import base64
import binascii
import dataclasses
import logging
import typing

import pydantic
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.inbound import MCP_NAME_HEADER, MCP_PARAM_HEADER_PREFIX, X_MCP_HEADER_KEY, decode_header_value
from mcp.types import CallToolResult, InputRequiredResult

from ..openapi import ExposedTool, OperationInfo, schema_description
from ..scopes import McpUnrestrictedScopePolicy, RequestTarget, ScopeRegistry, current_grant
from ._bm25 import BM25FIndex
from ._semantic import SemanticIndex, SemanticSearchOptions
from ._shaping import shape_operation
from ._shared import _get_override, build_input_schema, derive_description, derive_name
from ._upstream import UpstreamConnection, _build_success_result
from .tool import build_tool_function


logger = logging.getLogger(__name__)


# The meta-tool that invokes an operation, and the header its operation name travels in,
# so an HTTP layer can tell which operation a call reaches without reading the body.
CALL_OPERATION_TOOL = 'call_operation'
OPERATION_PARAM_HEADER = 'Operation'

# How much a matching term counts in each searchable field.
# A server's exposure.search.field_weights overrides them field by field.
DEFAULT_FIELD_WEIGHTS = {
    'name': 2.0,
    'summary': 3.0,
    'description': 0.5,
    'path': 0.5,
    'tags': 0.5,
    'parameters': 0.0,
}

# How strongly reciprocal rank fusion favours the top of each query's ranking, at the value most fusions use.
_FUSION_RANK_OFFSET = 60

# How many operations one search page carries.
_PAGE_SIZE = 20

# The longest description a search result shows, in characters.
# The full text stays one get_operation call away.
_BRIEF_DESCRIPTION_LIMIT = 200

# The most phrasings one search takes, since each costs a ranking pass, and an embedding when searching by meaning.
_QUERY_LIMIT = 10

# What each meta-tool tells the model about itself.
# search_operations says how to phrase a query by how the server matches, by shared words or by meaning.
_SEARCH_BY_WORDS_GUIDANCE = (
    "Search with words the API's own documentation would use, such as `merge pull request`, "
    'rather than the way the user put it. '
    'Matching is by shared words, so pass several phrasings in `query` to try synonyms in one call, '
    'such as `["close issue", "update issue state"]`, and their matches are merged. '
)
_SEARCH_BY_MEANING_GUIDANCE = (
    'Describe what to do in plain words, such as `close out ticket 318`. '
    "Matching is by meaning, so a request in the user's own words works too. "
    'To look for several things in one call, pass a list of requests in `query`, and their matches are merged. '
)
_SEARCH_OPERATIONS_RESULTS = (
    'Returns `{operations: [{name, description}, ...]}`, best match first, '
    'with `nextCursor` when more matches exist; pass it back as `cursor` for the next page. '
    '`tags` limits the result to operations carrying any of those tags, and an omitted argument filters nothing. '
    'With neither `query` nor `tags`, lists every operation, '
    'and the first page also carries `tags: [{name, operations}, ...]`, each tag with its operation count. '
    'Call `get_operation(name)` next to fetch the input schema for one specific operation.'
)
_GET_OPERATION_DESCRIPTION = (
    'Return the input schema for one operation. '
    'Returns `{name, description, input_schema}`. '
    'Use the JSON Schema to construct the `arguments` payload for `call_operation`.'
)
_CALL_OPERATION_DESCRIPTION = (
    'Invoke an operation by name with a JSON object of arguments. '
    'Argument keys must match the keys in `get_operation(name).input_schema.properties`.'
)


class DynamicOperationTarget(RequestTarget):
    """The operation a ``call_operation`` invokes, named in its ``Mcp-Param-Operation`` header.

    The other meta-tools reach no single operation, so they address no guarded name.
    """

    def resolve(self, headers: typing.Mapping[str, str]) -> str | None:
        if decode_header_value(headers.get(MCP_NAME_HEADER)) != CALL_OPERATION_TOOL:
            return None
        return decode_header_value(headers.get(f'{MCP_PARAM_HEADER_PREFIX}{OPERATION_PARAM_HEADER}'.lower()))


class MetaToolGenerator:
    """Register the ``search_operations`` / ``get_operation`` / ``call_operation`` meta-tools, for dynamic exposure.

    The LLM searches, then gets, then calls, to discover and invoke a specific operation.
    Per-operation callables come from the same upstream closure the static path uses,
    so auth, path substitution, and request shape stay identical across modes.
    Each operation's scope requirement is recorded in ``scope_registry`` under its name,
    and search and get show only the operations the caller's token can use.
    """

    def __init__(
        self,
        mcp: MCPServer,
        connection: UpstreamConnection,
        field_weight_overrides: typing.Mapping[str, float] | None = None,
        scope_registry: ScopeRegistry | None = None,
        semantic_search: SemanticSearchOptions | None = None,
    ):
        self.mcp = mcp
        self.connection = connection
        self.scope_registry = scope_registry or ScopeRegistry(McpUnrestrictedScopePolicy())
        # Overrides replace the default weight of the fields they name and leave the rest as they are.
        self.field_weights = {**DEFAULT_FIELD_WEIGHTS, **(field_weight_overrides or {})}
        self.semantic_search = semantic_search

    def register(self, operations: list[OperationInfo]) -> list[ExposedTool]:
        """Build the operation registry from ``operations`` and bind the three meta-tools, returning them."""
        registered: list[_RegisteredOperation] = []
        for operation in operations:
            # Same shaping as the static path, so a dynamic operation exposes the identical surface.
            # Meta-tools carry no per-operation annotations, so annotation overrides do not apply here.
            shaped_operation = shape_operation(operation)
            # The input schema is the operation's contract, built once and both shown by get_operation and enforced.
            input_schema = build_input_schema(shaped_operation)
            tool_override = _get_override(shaped_operation, 'tool')
            name = derive_name(shaped_operation, tool_override.name if tool_override else None)
            self.scope_registry.register(name, operation)
            registered.append(
                _RegisteredOperation(
                    name=name,
                    summary=shaped_operation.summary,
                    description=derive_description(
                        shaped_operation, tool_override.description if tool_override else None
                    ),
                    path=shaped_operation.path,
                    tags=shaped_operation.tags,
                    input_schema=input_schema,
                    callable_=build_tool_function(
                        shaped_operation, self.connection, input_schema=input_schema, attach_signature=False
                    ),
                )
            )
            logger.debug(
                'Dynamic operation indexed: %s ← %s %s',
                registered[-1].name,
                shaped_operation.method.upper(),
                shaped_operation.path,
            )
        registry = _OperationRegistry(registered, self.field_weights, self.semantic_search)
        self._bind_meta_tools(registry)
        logger.info(
            'Registered %d operation(s) behind dynamic meta-tools on server "%s"',
            len(registry),
            self.mcp.name,
        )
        return [
            ExposedTool('search_operations', '-', '-'),
            ExposedTool('get_operation', '-', '-'),
            ExposedTool('call_operation', '-', '-'),
        ]

    def _bind_meta_tools(self, registry: '_OperationRegistry') -> None:
        guidance = _SEARCH_BY_MEANING_GUIDANCE if self.semantic_search else _SEARCH_BY_WORDS_GUIDANCE
        description = f'Search the operations available on this server. {guidance}{_SEARCH_OPERATIONS_RESULTS}'

        @self.mcp.tool(name='search_operations', description=description)
        async def search_operations(
            ctx: Context,
            query: typing.Annotated[
                str | typing.Annotated[list[str], pydantic.Field(max_length=_QUERY_LIMIT)] | None,
                pydantic.Field(description='What to do, or a list of phrasings whose matches are merged.'),
            ] = None,
            tags: typing.Annotated[
                list[str] | None, pydantic.Field(description='Only operations carrying any of these tags.')
            ] = None,
            cursor: typing.Annotated[
                str | None, pydantic.Field(description='The `nextCursor` of the previous page.')
            ] = None,
        ) -> CallToolResult:
            return _build_success_result(await registry.search(query, tags, cursor, self._visibility()))

        @self.mcp.tool(name='get_operation', description=_GET_OPERATION_DESCRIPTION)
        async def get_operation(name: str, ctx: Context) -> CallToolResult:
            operation = registry.get(name)
            if operation is None or not self._visibility()(name):
                raise _unknown_operation_error(name)
            return _build_success_result(
                {'name': name, 'description': operation.description, 'input_schema': operation.input_schema}
            )

        @self.mcp.tool(name=CALL_OPERATION_TOOL, description=_CALL_OPERATION_DESCRIPTION)
        async def call_operation(
            name: typing.Annotated[str, pydantic.Field(json_schema_extra={X_MCP_HEADER_KEY: OPERATION_PARAM_HEADER})],
            arguments: dict[str, typing.Any],
            ctx: Context,
        ) -> CallToolResult | InputRequiredResult:
            operation = registry.get(name)
            if operation is None:
                raise _unknown_operation_error(name)
            # A 2026-07-28 client lacking MCP scopes was already refused over HTTP with a scope challenge.
            # This refusal covers older clients, which send no Mcp-Param-Operation header, and a short upstream grant.
            refusal = self.scope_registry.refusal(name, current_grant())
            if refusal is not None:
                raise ToolError(refusal)
            return await operation.callable_(**arguments, ctx=ctx)

    def _visibility(self) -> typing.Callable[[str], bool]:
        """Whether each operation, by name, shows to the current token.

        Operations the token can step up for show as well.
        """
        grant = current_grant()
        return lambda name: self.scope_registry.lists(name, grant)


@dataclasses.dataclass(frozen=True)
class _RegisteredOperation:
    """One operation behind the meta-tools, carrying what search, get, and call need."""

    name: str
    summary: str
    description: str
    path: str
    tags: list[str]
    input_schema: dict[str, typing.Any]
    callable_: typing.Callable[..., typing.Awaitable[CallToolResult | InputRequiredResult]]

    def search_fields(self) -> dict[str, str]:
        """Return the text search ranks this operation by, keyed by field name."""
        return {
            'name': self.name,
            'summary': self.summary,
            'description': self.description,
            'path': self.path,
            'tags': ' '.join(self.tags),
            'parameters': _parameter_text(self.input_schema),
        }

    def embedding_text(self) -> str:
        """Return the text an embedding model reads for this operation, what it does before how it is reached.

        The description falls back to the summary, or to the method and path, so a repeated part is read once.
        """
        parts = (self.name, self.summary, self.description, self.path)
        return '\n'.join(part for part in dict.fromkeys(parts) if part)


class _OperationRegistry:
    """The operations of one dynamically exposed server, looked up by name or searched by query and tag.

    Everything derived from the operations, such as the search index and tag counts, is built once here,
    since the operations never change after registration.
    Tags match without regard to case, and each is shown in the spelling it first appears in.
    """

    def __init__(
        self,
        operations: typing.Iterable[_RegisteredOperation],
        field_weights: typing.Mapping[str, float],
        semantic_search: SemanticSearchOptions | None = None,
    ):
        self._operations = {operation.name: operation for operation in operations}
        self._index = BM25FIndex(
            {name: operation.search_fields() for name, operation in self._operations.items()}, field_weights
        )
        self._semantic_index: SemanticIndex | None = None
        self._merge_with_words = False
        if semantic_search:
            self._semantic_index = SemanticIndex(
                {name: operation.embedding_text() for name, operation in self._operations.items()}, semantic_search
            )
            self._merge_with_words = semantic_search.retrieval == 'hybrid'

        # Keyed by casefolded tag. The inner dict is an ordered set of operation names.
        self._names_by_tag: dict[str, dict[str, None]] = {}
        self._tag_spellings: dict[str, str] = {}
        for name, operation in self._operations.items():
            for tag in operation.tags:
                key = tag.casefold()
                self._tag_spellings.setdefault(key, tag)
                self._names_by_tag.setdefault(key, {})[name] = None

    def __len__(self) -> int:
        return len(self._operations)

    def get(self, name: str) -> _RegisteredOperation | None:
        return self._operations.get(name)

    async def search(
        self,
        query: str | list[str] | None,
        tags: list[str] | None,
        cursor: str | None,
        visible: typing.Callable[[str], bool],
    ) -> dict[str, typing.Any]:
        """Return one page of the ``visible`` operations matching ``query`` and carrying any of ``tags``.

        An omitted or blank ``query``, and omitted or empty ``tags``, filter nothing.
        A list of queries ranks each one, and merges the rankings, see ``_fuse``.
        Listing everything, the first page also carries the tag counts,
        so the model can narrow down rather than page through all of them.
        Raises ``ValueError`` for a cursor that is malformed or past the end.
        """
        queries = [text.strip() for text in ([query] if isinstance(query, str) else query or [])]
        queries = [text for text in dict.fromkeys(queries) if text]
        tags = tags or []
        names = await self._matching(queries, tags, visible)
        offset = _decode_cursor(cursor, len(names)) if cursor else 0
        page = names[offset : offset + _PAGE_SIZE]
        payload: dict[str, typing.Any] = {
            'operations': [{'name': name, 'description': _brief(self._operations[name].description)} for name in page]
        }
        if offset + _PAGE_SIZE < len(names):
            payload['nextCursor'] = _encode_cursor(offset + _PAGE_SIZE)
        if not queries and not tags and offset == 0:
            tag_counts = self._tag_counts(set(names))
            if tag_counts:
                payload['tags'] = tag_counts
        return payload

    async def _rankings(self, queries: list[str], candidates: set[str]) -> list[list[str]]:
        """Rank ``candidates`` against each query, by words, by meaning, or by both.

        When the embedding model cannot be reached, the queries are ranked by words alone, so search keeps working.
        """
        if self._semantic_index is not None:
            try:
                by_meaning = await self._semantic_index.search(queries, candidates)
            except Exception as error:
                logger.warning('Searching by meaning failed, so this search ranks by words alone: %s', error)
            else:
                if not self._merge_with_words:
                    return by_meaning
                return [
                    _fuse([self._index.search(query, candidates), ranking])
                    for query, ranking in zip(queries, by_meaning, strict=True)
                ]
        return [self._index.search(query, candidates) for query in queries]

    def _tag_counts(self, names: set[str]) -> list[dict[str, typing.Any]]:
        """Each tag carried by any of ``names``, with how many of them carry it, in tag order."""
        counts = {key: len(names.intersection(tagged)) for key, tagged in self._names_by_tag.items()}
        return [
            {'name': self._tag_spellings[key], 'operations': counts[key]}
            for key in sorted(counts, key=lambda key: self._tag_spellings[key])
            if counts[key]
        ]

    async def _matching(self, queries: list[str], tags: list[str], visible: typing.Callable[[str], bool]) -> list[str]:
        """The ``visible`` operations carrying any of ``tags`` and matching ``queries``, best first.

        Hidden operations are dropped before ranking, since a ranking by meaning keeps only its top names.
        """
        tagged = {name for tag in tags for name in self._names_by_tag.get(tag.casefold(), {})} if tags else None
        candidates = {name for name in self._operations if (tagged is None or name in tagged) and visible(name)}
        if queries:
            return _fuse(await self._rankings(queries, candidates))
        return [name for name in self._operations if name in candidates]


def _fuse(rankings: list[list[str]]) -> list[str]:
    """Merge ``rankings`` by reciprocal rank fusion, best first.

    Each name scores the sum of ``1 / (offset + rank)`` over the rankings it appears in,
    so a name near the top of one ranking, or found by several, rises without comparing scores across queries.
    One ranking comes back as it was, and ties keep the order of first appearance.
    """
    if len(rankings) == 1:
        return rankings[0]
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, name in enumerate(ranking, start=1):
            scores[name] = scores.get(name, 0.0) + 1 / (_FUSION_RANK_OFFSET + rank)
    return sorted(scores, key=lambda name: -scores[name])


def _parameter_text(input_schema: dict[str, typing.Any]) -> str:
    """The name and description of each argument ``input_schema`` takes, as search reads them.

    Only the top-level arguments count,
    since a nested field says more about a payload's shape than about what the operation does.
    """
    defs = input_schema.get('$defs', {})
    return ' '.join(
        f'{name} {schema_description(schema, defs)}' for name, schema in input_schema.get('properties', {}).items()
    )


def _encode_cursor(offset: int) -> str:
    """Encode where the next page starts as an opaque cursor."""
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str, total: int) -> int:
    """Return the offset ``cursor`` encodes, rejecting one that is malformed or past the end of ``total`` results."""
    try:
        offset = int(base64.b64decode(cursor, altchars=b'-_', validate=True).decode())
    except (binascii.Error, UnicodeDecodeError, ValueError):
        offset = None
    if offset is None or not 0 < offset < total:
        raise ValueError(f'Invalid cursor: {cursor!r}. Pass the nextCursor of the previous page.')
    return offset


def _brief(description: str) -> str:
    """Cut ``description`` to its first paragraph on one line, at most ``_BRIEF_DESCRIPTION_LIMIT`` characters."""
    first_paragraph = ' '.join(description.strip().split('\n\n', 1)[0].split())
    if len(first_paragraph) <= _BRIEF_DESCRIPTION_LIMIT:
        return first_paragraph
    return first_paragraph[: _BRIEF_DESCRIPTION_LIMIT - 1].rstrip() + '…'


def _unknown_operation_error(name: str) -> ValueError:
    """Build the error ``get_operation`` and ``call_operation`` raise when ``name`` is not registered."""
    return ValueError(f'Unknown operation: {name!r}. Use search_operations to find available names.')
