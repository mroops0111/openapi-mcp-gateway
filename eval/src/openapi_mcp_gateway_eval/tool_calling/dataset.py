import collections
import pathlib
import typing

import pydantic
import yaml

from openapi_mcp_gateway.settings import ServerConfig

from ..errors import BenchmarkDataError
from ..spec_pins import SpecPin
from .replay import ResponseOverride


MANIFEST_FILE = 'benchmark.yaml'
SCENARIOS_DIRECTORY = 'scenarios'
SCENARIO_FILE = 'scenario.yaml'
TASKS_FILE = 'tasks.yaml'
VARIANTS_DIRECTORY = 'variants'
# The instructions given to the models that write the benchmark's data, kept with it.
PROMPTS_DIRECTORY = 'prompts'

# The variant every other is compared with, one tool per operation and nothing configured.
BASELINE_VARIANT = 'baseline'

# What a model is told when the run sets nothing else, the benchmark's own system prompt.
DEFAULT_SYSTEM_PROMPT = (
    "You act for a user through the tools of their APIs. Carry out the user's request by calling the tools. "
    'Do not ask the user anything; when something is ambiguous, make the most reasonable choice and go on. '
    'When the request is done, reply with one short sentence saying what you did.'
)

# Stand-ins for the fields the harness fills in, so a variant's server fragment can be checked on its own.
PLACEHOLDER_SERVER_FIELDS = {'name': 'placeholder', 'spec': 'placeholder.json', 'base_url': 'http://placeholder'}

# How a manifest names an operation, by method and path template, such as ``POST /v1/databases/{id}/query``.
OPERATION_PATTERN = r'^[A-Z]+ /\S*$'

Split = typing.Literal['dev', 'test']
SPLITS: tuple[Split, ...] = typing.get_args(Split)

ModelType = typing.TypeVar('ModelType', bound=pydantic.BaseModel)


class DatasetError(BenchmarkDataError):
    """The tool calling benchmark's files are inconsistent."""


class ModelSpec(pydantic.BaseModel):
    """One model the benchmark runs, named as Pydantic AI names it, such as ``anthropic:claude-sonnet-5-5``."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    model: str
    # Pydantic AI model settings, such as max_tokens, passed through unchanged.
    settings: dict[str, typing.Any] = pydantic.Field(default_factory=dict)


class ConfigGenerator(pydantic.BaseModel):
    """The model and skill that write a scenario's generated config from its intent alone."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    model: ModelSpec
    # The skill library holding the skill, relative to the benchmark directory.
    skill_library: pathlib.Path
    skill: str
    # Each variant the generator writes, by name, to what the user adds after the intent, empty for the intent alone.
    variants: dict[str, str] = pydantic.Field(min_length=1)


class RunSettings(pydantic.BaseModel):
    """What every run holds fixed, the specs, the models, the system prompt, and how each task runs."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    specs: dict[str, SpecPin] = pydantic.Field(min_length=1)
    # The models a run picks from, none for a check, which calls no model.
    models: dict[str, ModelSpec] = pydantic.Field(default_factory=dict)
    # How many times each task runs under each variant and model.
    repeat: int = pydantic.Field(default=3, ge=1)
    # The most model requests one task may make before it counts as failed.
    request_limit: int = pydantic.Field(default=20, ge=1)
    # How often a model may retry one tool after the gateway refuses its arguments, before the run fails.
    tool_retries: int = pydantic.Field(default=3, ge=0)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Each spec's operations that only read though their method writes, such as a query sent as a POST.
    # The grader counts a request to one as a read, so it never shows up as an extra write.
    read_only_operations: dict[
        str, list[typing.Annotated[str, pydantic.StringConstraints(pattern=OPERATION_PATTERN)]]
    ] = pydantic.Field(default_factory=dict)

    @pydantic.model_validator(mode='after')
    def _validate_read_only_operations(self) -> typing.Self:
        unknown = sorted(set(self.read_only_operations) - set(self.specs))
        if unknown:
            raise ValueError(f'read_only_operations names specs {unknown} the manifest does not pin')
        return self


class Manifest(RunSettings):
    """The manifest in ``benchmark.yaml``, adding the benchmark's version, scenarios, and config generator."""

    version: int = pydantic.Field(ge=1)
    models: dict[str, ModelSpec] = pydantic.Field(min_length=1)
    scenarios: list[str] = pydantic.Field(min_length=1)
    config_generator: ConfigGenerator


class ExpectedCall(pydantic.BaseModel):
    """A request the upstream must receive for a task to pass.

    ``path`` is a spec template, such as ``/repos/{owner}/{repo}/pulls/{pull_number}/merge``, or a concrete path,
    and ``path_parameters``, ``query``, and ``body`` name values the request must carry, leaving the rest free.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    server: str
    method: str
    path: str
    path_parameters: dict[str, str] = pydantic.Field(default_factory=dict)
    query: dict[str, str] = pydantic.Field(default_factory=dict)
    body: dict[str, typing.Any] = pydantic.Field(default_factory=dict)

    @property
    def alternatives(self) -> list[list['ExpectedCall']]:
        return [[self]]

    @pydantic.field_validator('method')
    @classmethod
    def _validate_method(cls, method: str) -> str:
        return method.upper()


class AllOf(pydantic.BaseModel):
    """Calls that together do one step of a task, each needing a request of its own.

    A model may split one change across requests, as Notion appends a heading and a paragraph in one request or two.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    all_of: list[ExpectedCall] = pydantic.Field(min_length=2)


class AnyOf(pydantic.BaseModel):
    """Ways to do one step of a task, any one of which counts, each a call or a group of calls.

    Some APIs do one thing by more than one endpoint, as Stripe refunds a charge by its own endpoint or a new refund.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    any_of: list[ExpectedCall | AllOf] = pydantic.Field(min_length=2)

    @property
    def alternatives(self) -> list[list[ExpectedCall]]:
        return [[way] if isinstance(way, ExpectedCall) else list(way.all_of) for way in self.any_of]


class SolutionCall(pydantic.BaseModel):
    """One call of a task's reference solution, by the operation's name as search lists it and its raw arguments."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    server: str
    operation: str
    arguments: dict[str, typing.Any] = pydantic.Field(default_factory=dict)


class Task(pydantic.BaseModel):
    """One request a model is asked to carry out, how it is graded, and a solution that is known to pass."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    id: str
    prompt: str = pydantic.Field(min_length=1)
    # The benchmark's split, which a task of the user's own goes without.
    split: Split | None = None
    expected_calls: list[ExpectedCall | AnyOf] = pydantic.Field(min_length=1)
    # Requests that fail the task when made, for what the request says to leave alone.
    forbidden_calls: list[ExpectedCall] = pydantic.Field(default_factory=list)
    # Calls known to pass, which only the check runs, so a task of the user's own may go without.
    solution: list[SolutionCall] = pydantic.Field(default_factory=list)
    # What the replay answers a read with, where the spec's own sample would not carry what the task needs.
    upstream_responses: list[ResponseOverride] = pydantic.Field(default_factory=list)


class Variant(pydantic.BaseModel):
    """One way of exposing a scenario's servers, as a server config fragment per server.

    ``tool_search`` hides every tool the gateway lists behind Pydantic AI's own tool search,
    so the client, not the gateway, finds the tools a task needs.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    servers: dict[str, dict[str, typing.Any]] = pydantic.Field(default_factory=dict)
    tool_search: bool = False

    @pydantic.field_validator('servers')
    @classmethod
    def _validate_servers(cls, servers: dict[str, dict[str, typing.Any]]) -> dict[str, dict[str, typing.Any]]:
        """Refuse a fragment that sets what the harness fills in, or that no server config would accept."""
        for server, fragment in servers.items():
            taken = sorted(set(fragment) & set(PLACEHOLDER_SERVER_FIELDS))
            if taken:
                raise ValueError(f'{server} cannot set {taken}, which the harness fills in')
            try:
                ServerConfig.model_validate({**fragment, **PLACEHOLDER_SERVER_FIELDS})
            except pydantic.ValidationError as error:
                raise ValueError(f'{server} is not a valid server config fragment: {error}') from error
        return servers

    def server_fragment(self, server: str) -> dict[str, typing.Any]:
        return dict(self.servers.get(server, {}))


class Scenario(pydantic.BaseModel):
    """One realistic use of one or more APIs, with its intent, servers, tasks, and the variants compared on it."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    name: str
    # One sentence of what the user wants the APIs for, which the scenario config was written from alone.
    intent: str
    # Server name to the name of its spec in the manifest.
    servers: dict[str, str] = pydantic.Field(min_length=1)
    tasks: list[Task] = pydantic.Field(min_length=1)
    variants: dict[str, Variant]

    @pydantic.model_validator(mode='after')
    def _validate_references(self) -> typing.Self:
        if BASELINE_VARIANT not in self.variants:
            raise ValueError(f'{self.name} has no {BASELINE_VARIANT} variant')
        task_ids = [task.id for task in self.tasks]
        duplicates = sorted({task_id for task_id in task_ids if task_ids.count(task_id) > 1})
        if duplicates:
            raise ValueError(f'{self.name} repeats task ids {duplicates}')
        for task in self.tasks:
            named = {
                call.server for step in task.expected_calls for alternative in step.alternatives for call in alternative
            }
            named |= {call.server for call in task.forbidden_calls}
            named |= {call.server for call in task.solution}
            named |= {response.server for response in task.upstream_responses}
            unknown = sorted(named - set(self.servers))
            if unknown:
                raise ValueError(f'{self.name} task {task.id} names servers {unknown} the scenario does not have')
        for variant_name, variant in self.variants.items():
            unknown = sorted(set(variant.servers) - set(self.servers))
            if unknown:
                raise ValueError(f'{self.name} variant {variant_name} configures servers {unknown} it does not have')
        return self


class Benchmark(pydantic.BaseModel):
    """A tool calling benchmark directory, or a user's own configs and tasks, loaded and validated."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    # The directory a spec pin's relative source resolves against.
    root: pathlib.Path
    manifest: RunSettings
    scenarios: dict[str, Scenario]
    # Variants whose reach is measured rather than required, such as a generated config or a user's own.
    measured_variants: frozenset[str] = frozenset()

    def spec_paths(self) -> dict[str, str]:
        """A local file holding each pinned spec, by the spec's name, downloading any not yet cached."""
        return {name: str(pin.fetch(self.root)) for name, pin in self.manifest.specs.items()}


def load(root: pathlib.Path) -> Benchmark:
    """Load and validate the tool calling benchmark in ``root``, raising ``DatasetError`` on any inconsistency.

    Each scenario is a directory of its intent and servers, its tasks, and one file per variant.
    """
    manifest = _validated(Manifest, _read_yaml(root / MANIFEST_FILE), root / MANIFEST_FILE)
    scenarios = {}
    for name in manifest.scenarios:
        directory = root / SCENARIOS_DIRECTORY / name
        scenario_file = _read_yaml(directory / SCENARIO_FILE)
        variants = {
            path.stem: _validated(Variant, _read_yaml(path) or {}, path)
            for path in sorted((directory / VARIANTS_DIRECTORY).glob('*.yaml'))
        }
        scenario = _validated(
            Scenario,
            {**scenario_file, 'name': name, 'tasks': _read_yaml(directory / TASKS_FILE), 'variants': variants},
            directory,
        )
        unknown_specs = sorted(set(scenario.servers.values()) - set(manifest.specs))
        if unknown_specs:
            raise DatasetError(f'{directory}: specs {unknown_specs} are not in the manifest')
        scenarios[name] = scenario
    _check_benchmark_tasks(scenarios)
    _check_splits(scenarios)
    return Benchmark(
        root=root,
        manifest=manifest,
        scenarios=scenarios,
        measured_variants=frozenset(manifest.config_generator.variants),
    )


def _check_splits(scenarios: dict[str, Scenario]) -> None:
    """Refuse scenarios whose tasks split differently, so each scenario weighs the same in a mean over them."""
    splits = {
        name: dict(sorted(collections.Counter(task.split for task in scenario.tasks).items()))
        for name, scenario in scenarios.items()
    }
    if len({tuple(counts.items()) for counts in splits.values()}) > 1:
        raise DatasetError(f'Every scenario must have as many tasks in each split, but they have {splits}')


def _check_benchmark_tasks(scenarios: dict[str, Scenario]) -> None:
    """Refuse a benchmark task without a split or a solution, which only a task of the user's own may leave out.

    A run reports the test split, and the check proves every task can pass.
    """
    tasks = [task for scenario in scenarios.values() for task in scenario.tasks]
    unsplit = sorted(task.id for task in tasks if task.split is None)
    if unsplit:
        raise DatasetError(f'Tasks {unsplit} have no split')
    unsolved = sorted(task.id for task in tasks if not task.solution)
    if unsolved:
        raise DatasetError(f'Tasks {unsolved} have no solution')


def _validated(model: type[ModelType], data: typing.Any, location: pathlib.Path) -> ModelType:
    try:
        return model.model_validate(data)
    except pydantic.ValidationError as error:
        raise DatasetError(f'{location}: {error}') from error


def _read_yaml(path: pathlib.Path) -> typing.Any:
    if not path.exists():
        raise DatasetError(f'{path} not found')
    try:
        return yaml.safe_load(path.read_text(encoding='utf-8'))
    except yaml.YAMLError as error:
        raise DatasetError(f'{path}: {error}') from error
