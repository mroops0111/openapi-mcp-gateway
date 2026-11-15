import pathlib
import typing

import pydantic
import yaml

from ...settings import ServerConfig
from ..errors import BenchmarkDataError
from ..spec_pins import SpecPin
from .replay import ResponseOverride


MANIFEST_FILE = 'benchmark.yaml'
SCENARIOS_DIRECTORY = 'scenarios'
SCENARIO_FILE = 'scenario.yaml'
TASKS_FILE = 'tasks.yaml'
VARIANTS_DIRECTORY = 'variants'

# The variant every other is compared with, one tool per operation and nothing configured.
BASELINE_VARIANT = 'baseline'

# Stand-ins for the fields the harness fills in, so a variant's server fragment can be checked on its own.
PLACEHOLDER_SERVER_FIELDS = {'name': 'placeholder', 'spec': 'placeholder.json', 'base_url': 'http://placeholder'}

Split = typing.Literal['practice', 'final']
SPLITS: tuple[Split, ...] = typing.get_args(Split)

ModelType = typing.TypeVar('ModelType', bound=pydantic.BaseModel)


class DatasetError(BenchmarkDataError):
    """The agent benchmark's files are inconsistent."""


class ModelSpec(pydantic.BaseModel):
    """One model the benchmark runs, named as Pydantic AI names it, such as ``anthropic:claude-sonnet-5-5``."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    model: str
    # Pydantic AI model settings, such as max_tokens, passed through unchanged.
    settings: dict[str, typing.Any] = pydantic.Field(default_factory=dict)


class Manifest(pydantic.BaseModel):
    """The manifest in ``benchmark.yaml``, naming the specs, the models, and how each task runs."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    version: int = pydantic.Field(ge=1)
    specs: dict[str, SpecPin] = pydantic.Field(min_length=1)
    models: dict[str, ModelSpec] = pydantic.Field(min_length=1)
    scenarios: list[str] = pydantic.Field(min_length=1)
    repetitions: int = pydantic.Field(default=3, ge=1)
    # The most model requests one task may make before it counts as failed.
    request_limit: int = pydantic.Field(default=20, ge=1)
    system_prompt: str


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

    @pydantic.field_validator('method')
    @classmethod
    def _validate_method(cls, method: str) -> str:
        return method.upper()


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
    split: Split
    # Whether the task is one the scenario's intent covers, so a config written for the intent should serve it.
    in_intent: bool
    expected_calls: list[ExpectedCall] = pydantic.Field(min_length=1)
    solution: list[SolutionCall] = pydantic.Field(min_length=1)
    responses: list[ResponseOverride] = pydantic.Field(default_factory=list)


class Variant(pydantic.BaseModel):
    """One way of exposing a scenario's servers, as a server config fragment per server."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    servers: dict[str, dict[str, typing.Any]] = pydantic.Field(default_factory=dict)

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
    """One realistic use of one or more APIs: its intent, its servers, its tasks, and the variants compared on it."""

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
            named = {call.server for call in task.expected_calls} | {call.server for call in task.solution}
            named |= {response.server for response in task.responses}
            unknown = sorted(named - set(self.servers))
            if unknown:
                raise ValueError(f'{self.name} task {task.id} names servers {unknown} the scenario does not have')
        for variant_name, variant in self.variants.items():
            unknown = sorted(set(variant.servers) - set(self.servers))
            if unknown:
                raise ValueError(f'{self.name} variant {variant_name} configures servers {unknown} it does not have')
        return self


class Benchmark(pydantic.BaseModel):
    """An agent benchmark directory, loaded and validated."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    root: pathlib.Path
    manifest: Manifest
    scenarios: dict[str, Scenario]


def load(root: pathlib.Path) -> Benchmark:
    """Load and validate the agent benchmark in ``root``, raising ``DatasetError`` on any inconsistency.

    Each scenario is a directory holding ``scenario.yaml``, ``tasks.yaml``, and one file per variant under ``variants/``.
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
    return Benchmark(root=root, manifest=manifest, scenarios=scenarios)


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
