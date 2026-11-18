import datetime
import hashlib
import importlib.metadata
import pathlib
import shutil
import typing

import pydantic
import yaml
from pydantic_ai import Agent, ModelRetry, capture_run_messages
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness import FileSystem, Shell, Skills

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig

from .dataset import SCENARIOS_DIRECTORY, VARIANTS_DIRECTORY, Benchmark, DatasetError, Scenario


# The record of how a generated variant was made, kept beside the scenario's other files, named after the variant.
PROVENANCE_SUFFIX = '.provenance.json'

# The config the generator edits in its workspace, and the exchange kept beside it for reading after the fact.
WORKSPACE_CONFIG_FILE = 'config.yaml'
MESSAGES_FILE = 'messages.json'

# The upstream the starter config names, never reached, since the benchmark serves each spec from a replay.
PLACEHOLDER_BASE_URL = 'https://upstream.invalid'

# The server fields the benchmark fills in itself, dropped from what the generator wrote.
BENCHMARK_SERVER_FIELDS = frozenset({'name', 'spec', 'base_url', 'auth', 'headers'})

# The only command the generator may run, which is how the skill verifies a config, through the one tool that runs it.
SHELL_COMMANDS = ('openapi-mcp-gateway',)
SHELL_TOOLS = ('run_command',)

# How often the generator may be sent back to fix a config that does not load, or to fix one tool call.
# A refused command counts against its tool, and a model often tries more than one before it settles.
OUTPUT_RETRIES = 3
TOOL_RETRIES = 5

# What sets a benchmark run apart from a person using the skill, and nothing about how to write the config.
GENERATOR_INSTRUCTIONS = f"""\
You are writing an openapi-mcp-gateway config for the user's request, with the generate-config skill.
No one can answer questions, so where the skill says to confirm with the user, decide as the request suggests.
The specs are already chosen and saved in your working directory, so skip finding one.
{WORKSPACE_CONFIG_FILE} in your working directory names each server with its spec, a placeholder base_url and no auth.
Keep those, and add what the request calls for.
Verify the config with `openapi-mcp-gateway --config {WORKSPACE_CONFIG_FILE} --dry-run`, which is installed.
When it validates, reply with one short paragraph on what you exposed and why.
"""


class GenerationRun(pydantic.BaseModel):
    """How one scenario's config was generated, by which model and skill, from which intent, when, and at what cost."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    scenario: str
    variant: str
    model: str
    skill: str
    skill_sha256: str
    # What the generator was asked, the scenario's intent and anything the variant adds after it.
    request: str
    finished_at: str
    gateway_version: str
    pydantic_ai_version: str
    input_tokens: int
    output_tokens: int
    model_requests: int
    summary: str


class ReadableDumper(yaml.SafeDumper):
    """Writes a multi-line string, such as a JSONata expression, as a literal block rather than an escaped line."""

    @staticmethod
    def represent_text(representer: yaml.representer.BaseRepresenter, value: typing.Any) -> yaml.Node:
        return representer.represent_scalar('tag:yaml.org,2002:str', value, style='|' if '\n' in value else None)

    yaml_representers = {**yaml.SafeDumper.yaml_representers, str: represent_text}


async def generate(
    benchmark: Benchmark,
    work_directory: pathlib.Path,
    *,
    scenarios: typing.Sequence[str] = (),
    variants: typing.Sequence[str] = (),
    model: typing.Any = None,
) -> list[GenerationRun]:
    """Write each scenario's generated variants with the manifest's config generator, from its intent alone.

    Each variant asks with the intent and what the manifest adds after it, never with the scenario's tasks.

    The generator runs the skill in a workspace holding the scenario's pinned specs and a starter config,
    and its config is sent back until it loads.
    The benchmark's own server fields are then dropped,
    and the rest becomes the scenario's generated variant, with the run's provenance beside it.
    Every raw exchange is kept in the work directory.
    """
    generator = benchmark.manifest.config_generator
    unknown = sorted(set(variants) - set(generator.variants))
    if unknown:
        raise DatasetError(f'The config generator writes no variants named {unknown}')
    runs = []
    for name in scenarios or list(benchmark.scenarios):
        scenario = benchmark.scenarios[name]
        scenario_directory = benchmark.root / SCENARIOS_DIRECTORY / name
        for variant in variants or list(generator.variants):
            request = ' '.join(part for part in (scenario.intent, generator.variants[variant]) if part)
            workspace = work_directory / name / variant
            spec_paths = _prepare_workspace(benchmark, scenario, workspace)
            run, config = await _run_generator(benchmark, scenario, variant, request, workspace, spec_paths, model)
            _write_variant(scenario_directory / VARIANTS_DIRECTORY / f'{variant}.yaml', config, run)
            provenance = scenario_directory / f'{variant}{PROVENANCE_SUFFIX}'
            provenance.write_text(run.model_dump_json(indent=2) + '\n', encoding='utf-8')
            runs.append(run)
    return runs


def server_fragments(config: dict[str, typing.Any]) -> dict[str, dict[str, typing.Any]]:
    """Each server of a generated config, without the fields the benchmark fills in itself."""
    return {
        server['name']: {key: value for key, value in server.items() if key not in BENCHMARK_SERVER_FIELDS}
        for server in config.get('servers', [])
    }


def _prepare_workspace(benchmark: Benchmark, scenario: Scenario, workspace: pathlib.Path) -> dict[str, pathlib.Path]:
    """A fresh workspace with each server's pinned spec and a starter config naming them, keyed by server."""
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    spec_paths = {}
    for server, spec in scenario.servers.items():
        cached = benchmark.manifest.specs[spec].fetch(benchmark.root)
        spec_paths[server] = workspace / f'{server}{cached.suffix}'
        shutil.copyfile(cached, spec_paths[server])
    starter = {
        'servers': [
            {'name': server, 'spec': path.name, 'base_url': PLACEHOLDER_BASE_URL, 'auth': {'type': 'none'}}
            for server, path in spec_paths.items()
        ]
    }
    (workspace / WORKSPACE_CONFIG_FILE).write_text(yaml.safe_dump(starter, sort_keys=False), encoding='utf-8')
    return spec_paths


async def _run_generator(
    benchmark: Benchmark,
    scenario: Scenario,
    variant: str,
    request: str,
    workspace: pathlib.Path,
    spec_paths: dict[str, pathlib.Path],
    model: typing.Any,
) -> tuple[GenerationRun, dict[str, typing.Any]]:
    generator = benchmark.manifest.config_generator
    skill_library = (benchmark.root / generator.skill_library).resolve()
    agent = Agent(
        model or generator.model.model,
        name='config_generator',
        instructions=GENERATOR_INSTRUCTIONS,
        model_settings=typing.cast(typing.Any, generator.model.settings),
        retries={'output': OUTPUT_RETRIES, 'tools': TOOL_RETRIES},
        capabilities=[
            Skills('.', include=[generator.skill], workspace=LocalWorkspaceBackend(skill_library)),
            LocalWorkspace(workspace),
            FileSystem(),
            Shell(allowed_commands=SHELL_COMMANDS, tools=SHELL_TOOLS),
        ],
    )

    @agent.output_validator
    def check_config(summary: str) -> str:
        _load_workspace_config(workspace, spec_paths)
        return summary

    with capture_run_messages() as messages:
        try:
            result = await agent.run(request)
        finally:
            (workspace / MESSAGES_FILE).write_bytes(ModelMessagesTypeAdapter.dump_json(messages, indent=2))
    usage = result.usage
    run = GenerationRun(
        scenario=scenario.name,
        variant=variant,
        model=generator.model.model,
        skill=generator.skill,
        skill_sha256=hashlib.sha256((skill_library / generator.skill / 'SKILL.md').read_bytes()).hexdigest(),
        request=request,
        finished_at=datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds'),
        gateway_version=importlib.metadata.version('openapi-mcp-gateway'),
        pydantic_ai_version=importlib.metadata.version('pydantic-ai-slim'),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        model_requests=usage.requests,
        summary=result.output,
    )
    return run, _load_workspace_config(workspace, spec_paths)


def _load_workspace_config(workspace: pathlib.Path, spec_paths: dict[str, pathlib.Path]) -> dict[str, typing.Any]:
    """The workspace config, after checking it loads as the dry run does, sending the generator back when it does not.

    Each spec is pointed at its file by absolute path first, since the generator names them relative to its workspace.
    """
    path = workspace / WORKSPACE_CONFIG_FILE
    try:
        config = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError) as error:
        raise ModelRetry(f'{WORKSPACE_CONFIG_FILE} could not be read: {error}') from error
    raw_servers = config.get('servers') if isinstance(config, dict) else None
    servers = [server for server in raw_servers or [] if isinstance(server, dict)]
    names = sorted(str(server.get('name')) for server in servers)
    if names != sorted(spec_paths):
        raise ModelRetry(f'{WORKSPACE_CONFIG_FILE} must keep exactly the servers {sorted(spec_paths)}, not {names}.')
    resolved = {
        **config,
        'servers': [{**server, 'spec': str(spec_paths[server['name']])} for server in servers],
    }
    try:
        Gateway.from_config(GatewayConfig.model_validate(resolved))
    except Exception as error:
        raise ModelRetry(f'{WORKSPACE_CONFIG_FILE} does not load: {error}') from error
    return config


def _write_variant(path: pathlib.Path, config: dict[str, typing.Any], run: GenerationRun) -> None:
    header = (
        f'# Generated by {run.model} with the {run.skill} skill, from the intent and never the tasks.\n'
        f'# Written on {run.finished_at}, and {run.variant}{PROVENANCE_SUFFIX} records the request and the run.\n'
        '# Generate it again rather than editing it.\n'
    )
    body = yaml.dump(
        {'servers': server_fragments(config)}, Dumper=ReadableDumper, sort_keys=False, allow_unicode=True, width=120
    )
    path.write_text(header + body, encoding='utf-8')
