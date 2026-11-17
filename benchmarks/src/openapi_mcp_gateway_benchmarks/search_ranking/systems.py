import abc
import contextlib
import hashlib
import pathlib
import random
import typing

import pydantic
from mcp import Client
from pydantic_ai import Agent, Tool, capture_run_messages
from pydantic_ai.capabilities import ToolSearch
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from openapi_mcp_gateway.settings import ServerConfig

from ..gateway_connection import OperationListing, call_tool, connect, list_operations


SEARCH_TOOL = 'search_operations'

# The tool Pydantic AI's tool search gives a model for finding deferred tools.
SEARCH_TOOLS_TOOL = 'search_tools'

# Stand-ins for the fields every spec fills in, so a gateway system's server fragment can be checked on its own.
PLACEHOLDER_SERVER_FIELDS = {'name': 'placeholder', 'spec': 'placeholder.json'}


class SpecCorpus(pydantic.BaseModel):
    """One spec as every system sees it, with its name, a local copy of its document, and every operation it serves."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    name: str
    spec_path: pathlib.Path
    operations: tuple[str, ...]


class Retriever(typing.Protocol):
    """Ranks operation names for a query, as deep as asked."""

    async def rank(self, query: str, depth: int) -> list[str]: ...


class System(pydantic.BaseModel, abc.ABC):
    """A way of ranking a spec's operations, opened once per spec to rank every query of it.

    A manifest declares each system as a mapping whose ``type`` picks the subclass,
    and the manifest validates its whole configuration on load.
    A new kind of system, such as another framework's tool search, is a new subclass added to ``Manifest.systems``,
    and the runner, the report, and the CI gate stay as they are.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    type: str

    @abc.abstractmethod
    def open(self, corpus: SpecCorpus) -> contextlib.AbstractAsyncContextManager[Retriever]:
        """Yield a retriever over ``corpus``, released when the spec's queries are done."""


class GatewaySystem(System):
    """The gateway's own ``search_operations``, called over MCP exactly as a model would call it.

    ``server`` is the part of a server config that differs between gateway systems, such as ``exposure``.
    The spec's ``name`` and ``spec`` are filled in for each spec.
    """

    type: typing.Literal['gateway'] = 'gateway'
    server: dict[str, typing.Any] = pydantic.Field(default_factory=dict)

    @pydantic.field_validator('server')
    @classmethod
    def _validate_server(cls, server: dict[str, typing.Any]) -> dict[str, typing.Any]:
        """Refuse a fragment that sets what each spec fills in, or that no server config would accept."""
        taken = sorted(set(server) & set(PLACEHOLDER_SERVER_FIELDS))
        if taken:
            raise ValueError(f'server cannot set {taken}, which come from each spec')
        try:
            ServerConfig.model_validate({**server, **PLACEHOLDER_SERVER_FIELDS})
        except pydantic.ValidationError as error:
            raise ValueError(f'server is not a valid server config fragment: {error}') from error
        return server

    def server_config(self, corpus: SpecCorpus) -> ServerConfig:
        """The server config this system serves ``corpus`` with."""
        return ServerConfig.model_validate({**self.server, 'name': corpus.name, 'spec': str(corpus.spec_path)})

    @contextlib.asynccontextmanager
    async def open(self, corpus: SpecCorpus) -> typing.AsyncIterator[Retriever]:
        async with connect(self.server_config(corpus)) as client:
            yield GatewaySearch(client)


class RandomSystem(System):
    """A floor to compare against, ranking every operation in an order fixed by the query's text alone."""

    type: typing.Literal['random'] = 'random'

    @contextlib.asynccontextmanager
    async def open(self, corpus: SpecCorpus) -> typing.AsyncIterator[Retriever]:
        yield RandomRanking(corpus.operations)


class PydanticAIKeywordSystem(System):
    """Pydantic AI's own tool search with its keyword strategy, a common agent framework's default to compare with.

    Every operation becomes a deferred Pydantic AI tool with its name and its spec text as the description,
    and a scripted model asks the framework's ``search_tools`` for the query, so the ranking is the framework's own.
    """

    type: typing.Literal['pydantic-ai-keywords'] = 'pydantic-ai-keywords'

    @contextlib.asynccontextmanager
    async def open(self, corpus: SpecCorpus) -> typing.AsyncIterator[Retriever]:
        server = ServerConfig.model_validate({'name': corpus.name, 'spec': str(corpus.spec_path)})
        yield PydanticAIKeywordSearch(list_operations(server))


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


class PydanticAIKeywordSearch:
    """Ranks by running Pydantic AI's ``search_tools`` once per query, through the framework's public API."""

    def __init__(self, listings: typing.Sequence[OperationListing]):
        self._tools = [
            Tool(
                _never_called,
                name=listing.name,
                description=' '.join(part for part in (listing.summary, listing.description) if part),
                defer_loading=True,
            )
            for listing in listings
        ]

    async def rank(self, query: str, depth: int) -> list[str]:
        def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            searched = any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts)
            if searched:
                return ModelResponse(parts=[TextPart('Done.')])
            return ModelResponse(parts=[ToolCallPart(tool_name=SEARCH_TOOLS_TOOL, args={'queries': [query]})])

        agent = Agent(
            FunctionModel(respond),
            tools=self._tools,
            capabilities=[ToolSearch(strategy='keywords', max_results=depth)],
        )
        with capture_run_messages() as messages:
            await agent.run(query)
        for message in messages:
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name == SEARCH_TOOLS_TOOL:
                    content = part.content if isinstance(part.content, dict) else {}
                    return [match['name'] for match in content.get('discovered_tools', [])][:depth]
        return []


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


def _never_called() -> None:
    """The function behind every deferred tool, which search finds but nothing calls."""
