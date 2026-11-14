import hashlib
import random
import typing

from mcp import Client

from ..target import call_tool


SEARCH_TOOL = 'search_operations'


class Retriever(typing.Protocol):
    """Something that ranks operation names for a query, as deep as asked."""

    async def rank(self, query: str, depth: int) -> list[str]: ...


class GatewaySearch:
    """Ranks by calling ``search_operations`` on a gateway, exactly as a model would, following pages to ``depth``."""

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
    """A floor to compare against: every operation in an order fixed by the query's text alone."""

    def __init__(self, operations: typing.Sequence[str]):
        self._operations = list(operations)

    async def rank(self, query: str, depth: int) -> list[str]:
        shuffled = list(self._operations)
        # Seeded by the query, so the floor is the same on every run and every machine.
        random.Random(hashlib.sha256(query.encode()).digest()).shuffle(shuffled)
        return shuffled[:depth]


async def list_operations(client: Client) -> list[str]:
    """Every operation name the gateway's ``search_operations`` lists, in its order."""
    names: list[str] = []
    arguments: dict[str, typing.Any] = {}
    while True:
        page = await call_tool(client, SEARCH_TOOL, arguments)
        names.extend(operation['name'] for operation in page['operations'])
        if 'nextCursor' not in page:
            return names
        arguments = {'cursor': page['nextCursor']}
