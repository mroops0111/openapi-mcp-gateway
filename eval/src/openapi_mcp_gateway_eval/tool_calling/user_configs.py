import pathlib
import typing

import pydantic
import yaml

from openapi_mcp_gateway.settings import GatewayConfig, yaml_layer

from ..spec_pins import pin_as_it_is
from .dataset import (
    DEFAULT_SYSTEM_PROMPT,
    OPERATION_PATTERN,
    Benchmark,
    DatasetError,
    ModelSpec,
    RunSettings,
    Scenario,
    Task,
    Variant,
)


# The server fields a variant keeps from a user's config, which shape what the gateway shows a model.
# The rest, such as the upstream, its credential, and the MCP sign-in, says nothing about the model and is left out.
MODEL_FACING_FIELDS = ('exposure', 'policy', 'operations', 'instructions')

# What a user's configs can be compared with, each over the same servers and specs.
Comparison = typing.Literal['baseline', 'dynamic', 'tool-search']
COMPARISONS: tuple[Comparison, ...] = typing.get_args(Comparison)

# The name of a config given without one.
DEFAULT_CONFIG_NAME = 'yours'

ModelType = typing.TypeVar('ModelType', bound=pydantic.BaseModel)


class TasksFile(pydantic.BaseModel):
    """A user's own tasks, with the system prompt and the read-only operations every run over them shares."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Each server's operations that only read though their method writes, by server name.
    read_only_operations: dict[
        str, list[typing.Annotated[str, pydantic.StringConstraints(pattern=OPERATION_PATTERN)]]
    ] = pydantic.Field(default_factory=dict)
    tasks: list[Task] = pydantic.Field(min_length=1)


def load(
    configs: typing.Mapping[str, pathlib.Path],
    tasks_path: pathlib.Path,
    *,
    model_names: typing.Sequence[str],
    repeat: int = 1,
) -> Benchmark:
    """A benchmark of one scenario, the tasks in ``tasks_path`` over the servers every config in ``configs`` names.

    Each config becomes a variant by its name, keeping only what shapes the tools a model sees,
    and each comparison is a variant too, though a run picks the ones it compares with.
    Every config must name the same servers with the same specs, since only what a model sees may differ.
    A spec is pinned to its content now, a relative path resolving against the working directory as ``serve`` does.
    """
    taken = sorted(set(configs) & set(COMPARISONS))
    if taken:
        raise DatasetError(f'A config cannot be named {taken}, which a comparison is named')
    written = {name: _read_servers(path) for name, path in configs.items()}
    servers = _shared_servers(written)
    tasks_file = _parse(TasksFile, _read_yaml(tasks_path), tasks_path)
    variants = {
        name: Variant(
            servers={
                server['name']: {key: server[key] for key in MODEL_FACING_FIELDS if key in server}
                for server in config_servers
            }
        )
        for name, config_servers in written.items()
    }
    variants |= {comparison: _comparison_variant(comparison, servers) for comparison in COMPARISONS}
    base_directory = pathlib.Path.cwd()
    settings = _parse(
        RunSettings,
        {
            'specs': {server: pin_as_it_is(spec, base_directory) for server, spec in servers.items()},
            'models': {model: ModelSpec(model=model) for model in model_names},
            'repeat': repeat,
            'system_prompt': tasks_file.system_prompt,
            'read_only_operations': tasks_file.read_only_operations,
        },
        tasks_path,
    )
    scenario = _parse(
        Scenario,
        {
            'name': tasks_path.stem,
            'intent': '',
            'servers': {server: server for server in servers},
            'tasks': tasks_file.tasks,
            'variants': variants,
        },
        tasks_path,
    )
    return Benchmark(
        root=base_directory,
        manifest=settings,
        scenarios={scenario.name: scenario},
        measured_variants=frozenset(configs),
    )


def _read_servers(path: pathlib.Path) -> list[dict[str, typing.Any]]:
    """The servers of the config at ``path`` as written, once the gateway has loaded the config without complaint."""
    try:
        GatewayConfig.from_yaml(path)
    except pydantic.ValidationError as error:
        raise DatasetError(f'{path}: {error}') from error
    return list(yaml_layer(path).get('servers') or [])


def _shared_servers(configs: typing.Mapping[str, list[dict[str, typing.Any]]]) -> dict[str, str]:
    """The servers every config names, each to its spec, refusing configs that differ in either."""
    by_config = {name: {server['name']: server['spec'] for server in servers} for name, servers in configs.items()}
    first, *rest = by_config.items()
    if not first[1]:
        raise DatasetError(f'{first[0]} names no servers')
    for name, servers in rest:
        if servers != first[1]:
            raise DatasetError(
                f'{name} serves {servers} and {first[0]} serves {first[1]}, '
                'but every config must name the same servers with the same specs'
            )
    return first[1]


def _comparison_variant(comparison: Comparison, servers: typing.Iterable[str]) -> Variant:
    """One tool per operation for ``baseline``, the gateway's dynamic exposure, or Pydantic AI's own tool search."""
    if comparison == 'dynamic':
        return Variant(servers={server: {'exposure': {'style': 'dynamic'}} for server in servers})
    return Variant(tool_search=comparison == 'tool-search')


def _parse(model: type[ModelType], data: typing.Any, location: pathlib.Path) -> ModelType:
    try:
        return model.model_validate(data)
    except pydantic.ValidationError as error:
        raise DatasetError(f'{location}: {error}') from error


def _read_yaml(path: pathlib.Path) -> typing.Any:
    try:
        return yaml.safe_load(path.read_text(encoding='utf-8'))
    except yaml.YAMLError as error:
        raise DatasetError(f'{path}: {error}') from error
