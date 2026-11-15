import abc
import contextlib
import dataclasses
import hashlib
import pathlib
import random
import typing

from mcp import Client

from ...settings import ServerConfig
from ..target import call_tool, connect


SEARCH_TOOL = 'search_operations'


@dataclasses.dataclass(frozen=True)
class SpecCorpus:
    """One spec as every system sees it, with its name, a local copy of its document, and every operation it serves."""

    name: str
    spec_path: pathlib.Path
    operations: tuple[str, ...]


class Retriever(typing.Protocol):
    """Ranks operation names for a query, as deep as asked."""

    async def rank(self, query: str, depth: int) -> list[str]: ...


class SystemConfigError(ValueError):
    """A manifest names a system kind that does not exist, or configures one wrongly."""


class System(abc.ABC):
    """A named way of ranking operations, opened once per spec to rank every query of it.

    Each subclass declares its ``kind``, the key a manifest names it by, and registers itself on definition.
    A new kind of system, such as another framework's tool search, is a new subclass,
    and the runner, the report, and the CI gate stay as they are.
    """

    kind: typing.ClassVar[str]
    _kinds: typing.ClassVar[dict[str, type['System']]] = {}

    def __init__(self, name: str):
        self.name = name

    def __init_subclass__(cls, **kwargs: typing.Any):
        super().__init_subclass__(**kwargs)
        # Only a class that names its own kind is one a manifest can ask for.
        # ABCMeta works out the abstract methods after this hook runs, so they cannot be what decides.
        if 'kind' in cls.__dict__:
            if cls.kind in System._kinds:
                raise TypeError(f'System kind {cls.kind!r} is already taken by {System._kinds[cls.kind].__name__}')
            System._kinds[cls.kind] = cls

    @classmethod
    def from_spec(cls, name: str, kind: str, config: typing.Mapping[str, typing.Any]) -> 'System':
        """Build the system a manifest declares under ``name``, raising ``SystemConfigError`` for an unknown kind."""
        system_class = System._kinds.get(kind)
        if system_class is None:
            raise SystemConfigError(f'System {name!r} has unknown kind {kind!r}; choose from {sorted(System._kinds)}')
        return system_class.configure(name, config)

    @classmethod
    @abc.abstractmethod
    def configure(cls, name: str, config: typing.Mapping[str, typing.Any]) -> typing.Self:
        """Build this kind of system from its manifest entry."""

    @abc.abstractmethod
    def open(self, corpus: SpecCorpus) -> contextlib.AbstractAsyncContextManager[Retriever]:
        """Yield a retriever over ``corpus``, released when the spec's queries are done."""


class GatewaySystem(System):
    """The gateway's own ``search_operations``, called over MCP exactly as a model would call it.

    Its config is the part of a server config that differs between gateway systems, such as ``exposure``.
    The spec's ``name`` and ``spec`` are filled in for each spec.
    """

    kind = 'gateway'

    def __init__(self, name: str, server: typing.Mapping[str, typing.Any]):
        super().__init__(name)
        self._server = dict(server)

    @classmethod
    def configure(cls, name: str, config: typing.Mapping[str, typing.Any]) -> typing.Self:
        if {'name', 'spec'} & set(config):
            raise SystemConfigError(f'System {name!r} cannot set name or spec, which come from each spec')
        return cls(name, config)

    def server_config(self, corpus: SpecCorpus) -> ServerConfig:
        """The server config this system serves ``corpus`` with."""
        return ServerConfig.model_validate({**self._server, 'name': corpus.name, 'spec': str(corpus.spec_path)})

    @contextlib.asynccontextmanager
    async def open(self, corpus: SpecCorpus) -> typing.AsyncIterator[Retriever]:
        async with connect(self.server_config(corpus)) as client:
            yield GatewaySearch(client)


class RandomSystem(System):
    """A floor to compare against, ranking every operation in an order fixed by the query's text alone."""

    kind = 'random'

    @classmethod
    def configure(cls, name: str, config: typing.Mapping[str, typing.Any]) -> typing.Self:
        if config:
            raise SystemConfigError(f'System {name!r} of kind random takes no configuration')
        return cls(name)

    @contextlib.asynccontextmanager
    async def open(self, corpus: SpecCorpus) -> typing.AsyncIterator[Retriever]:
        yield RandomRanking(corpus.operations)


class GatewaySearch:
    """Ranks by calling ``search_operations``, following its pages until the ranking is deep enough."""

    def __init__(self, client: Client):
        self._client = client

    async def rank(self, query: str, depth: int) -> list[str]:
        names: list[str] = []
        arguments: dict[str, typing.Any] = {'query': query}
        while len(names) < depth:
            page = await call_tool(self._client, SEARCH_TOOL, arguments)
            names.extend(operation['name'] for operation in page['operations'])
            if 'nextCursor' not in page:
                break
            arguments = {'query': query, 'cursor': page['nextCursor']}
        return names[:depth]


class RandomRanking:
    """Every operation, shuffled by a seed taken from the query, so the floor is the same on every run."""

    def __init__(self, operations: typing.Sequence[str]):
        self._operations = list(operations)

    async def rank(self, query: str, depth: int) -> list[str]:
        shuffled = list(self._operations)
        random.Random(hashlib.sha256(query.encode()).digest()).shuffle(shuffled)
        return shuffled[:depth]


async def served_operations(server: ServerConfig) -> tuple[str, ...]:
    """Every operation name a gateway serving ``server`` lists through ``search_operations``, in its order."""
    names: list[str] = []
    arguments: dict[str, typing.Any] = {}
    async with connect(server) as client:
        while True:
            page = await call_tool(client, SEARCH_TOOL, arguments)
            names.extend(operation['name'] for operation in page['operations'])
            if 'nextCursor' not in page:
                return tuple(names)
            arguments = {'cursor': page['nextCursor']}
