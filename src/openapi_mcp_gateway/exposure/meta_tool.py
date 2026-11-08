import base64
import binascii
import dataclasses
import logging
import typing

import pydantic
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult

from ..openapi import ExposedTool, OperationInfo
from ._bm25 import BM25FIndex
from ._shaping import shape_operation
from ._shared import _get_override, build_input_schema, derive_description, derive_name
from ._upstream import UpstreamBinding, _build_success_result
from .tool import build_tool_function


logger = logging.getLogger(__name__)


# How much a matching term counts in each searchable field.
# A server's exposure.search.field_weights overrides them field by field.
DEFAULT_FIELD_WEIGHTS = {'name': 2.0, 'summary': 3.0, 'description': 0.5, 'path': 0.5, 'tags': 0.5}

# How many operations one search page carries.
_PAGE_SIZE = 20

# The longest description a search result shows, in characters.
# The full text stays one get_operation call away.
_BRIEF_DESCRIPTION_LIMIT = 200

# What each meta-tool tells the model about itself.
_SEARCH_OPERATIONS_DESCRIPTION = (
    'Search the operations available on this server. '
    "Search with the words of the user's request first, such as `merge a pull request`. "
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


class MetaToolGenerator:
    """Register the ``search_operations`` / ``get_operation`` / ``call_operation`` meta-tools, for dynamic exposure.

    The LLM searches, then gets, then calls, to discover and invoke a specific operation.
    Per-operation callables come from the same upstream closure the static path uses,
    so auth, path substitution, and request shape stay identical across modes.
    """

    def __init__(
        self,
        mcp: MCPServer,
        binding: UpstreamBinding,
        field_weight_overrides: typing.Mapping[str, float] | None = None,
    ):
        self.mcp = mcp
        self.binding = binding
        # Overrides replace the default weight of the fields they name and leave the rest as they are.
        self.field_weights = {**DEFAULT_FIELD_WEIGHTS, **(field_weight_overrides or {})}

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
            registered.append(
                _RegisteredOperation(
                    name=derive_name(shaped_operation, tool_override.name if tool_override else None),
                    summary=shaped_operation.summary,
                    description=derive_description(
                        shaped_operation, tool_override.description if tool_override else None
                    ),
                    path=shaped_operation.path,
                    tags=shaped_operation.tags,
                    input_schema=input_schema,
                    callable_=build_tool_function(
                        shaped_operation, self.binding, input_schema=input_schema, attach_signature=False
                    ),
                )
            )
            logger.debug(
                'Dynamic operation indexed: %s ← %s %s',
                registered[-1].name,
                shaped_operation.method.upper(),
                shaped_operation.path,
            )
        registry = _OperationRegistry(registered, self.field_weights)
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
        @self.mcp.tool(name='search_operations', description=_SEARCH_OPERATIONS_DESCRIPTION)
        async def search_operations(
            ctx: Context,
            query: typing.Annotated[
                str | None, pydantic.Field(description="Free text describing what to do, in the user's words.")
            ] = None,
            tags: typing.Annotated[
                list[str] | None, pydantic.Field(description='Only operations carrying any of these tags.')
            ] = None,
            cursor: typing.Annotated[
                str | None, pydantic.Field(description='The `nextCursor` of the previous page.')
            ] = None,
        ) -> CallToolResult:
            return _build_success_result(registry.search(query, tags, cursor))

        @self.mcp.tool(name='get_operation', description=_GET_OPERATION_DESCRIPTION)
        async def get_operation(name: str, ctx: Context) -> CallToolResult:
            operation = registry.get(name)
            if operation is None:
                raise _unknown_operation_error(name)
            return _build_success_result(
                {'name': name, 'description': operation.description, 'input_schema': operation.input_schema}
            )

        @self.mcp.tool(name='call_operation', description=_CALL_OPERATION_DESCRIPTION)
        async def call_operation(name: str, arguments: dict[str, typing.Any], ctx: Context) -> CallToolResult:
            operation = registry.get(name)
            if operation is None:
                raise _unknown_operation_error(name)
            return await operation.callable_(**arguments, ctx=ctx)


@dataclasses.dataclass(frozen=True)
class _RegisteredOperation:
    """One operation behind the meta-tools, carrying what search, get, and call need."""

    name: str
    summary: str
    description: str
    path: str
    tags: list[str]
    input_schema: dict[str, typing.Any]
    callable_: typing.Callable[..., typing.Awaitable[CallToolResult]]

    def search_fields(self) -> dict[str, str]:
        """Return the text search ranks this operation by, keyed by field name."""
        return {
            'name': self.name,
            'summary': self.summary,
            'description': self.description,
            'path': self.path,
            'tags': ' '.join(self.tags),
        }


class _OperationRegistry:
    """The operations of one dynamically exposed server, looked up by name or searched by query and tag.

    Everything derived from the operations, such as the search index and tag counts, is built once here,
    since the operations never change after registration.
    Tags match without regard to case, and each is shown in the spelling it first appears in.
    """

    def __init__(self, operations: typing.Iterable[_RegisteredOperation], field_weights: typing.Mapping[str, float]):
        self._operations = {operation.name: operation for operation in operations}
        self._index = BM25FIndex(
            {name: operation.search_fields() for name, operation in self._operations.items()}, field_weights
        )
        # Keyed by casefolded tag. The inner dict is an ordered set of operation names.
        self._names_by_tag: dict[str, dict[str, None]] = {}
        self._tag_spellings: dict[str, str] = {}
        for name, operation in self._operations.items():
            for tag in operation.tags:
                key = tag.casefold()
                self._tag_spellings.setdefault(key, tag)
                self._names_by_tag.setdefault(key, {})[name] = None
        self._tag_counts = [
            {'name': self._tag_spellings[key], 'operations': len(self._names_by_tag[key])}
            for key in sorted(self._names_by_tag, key=lambda key: self._tag_spellings[key])
        ]

    def __len__(self) -> int:
        return len(self._operations)

    def get(self, name: str) -> _RegisteredOperation | None:
        return self._operations.get(name)

    def search(self, query: str | None, tags: list[str] | None, cursor: str | None) -> dict[str, typing.Any]:
        """Return one page of the operations matching ``query`` and carrying any of ``tags``.

        An omitted or blank ``query``, and omitted or empty ``tags``, filter nothing.
        Listing everything, the first page also carries the tag counts,
        so the model can narrow down rather than page through all of them.
        Raises ``ValueError`` for a cursor that is malformed or past the end.
        """
        query = (query or '').strip()
        tags = tags or []
        names = self._matching(query, tags)
        offset = _decode_cursor(cursor, len(names)) if cursor else 0
        page = names[offset : offset + _PAGE_SIZE]
        payload: dict[str, typing.Any] = {
            'operations': [{'name': name, 'description': _brief(self._operations[name].description)} for name in page]
        }
        if offset + _PAGE_SIZE < len(names):
            payload['nextCursor'] = _encode_cursor(offset + _PAGE_SIZE)
        if not query and not tags and offset == 0 and self._tag_counts:
            payload['tags'] = self._tag_counts
        return payload

    def _matching(self, query: str, tags: list[str]) -> list[str]:
        candidates = {name for tag in tags for name in self._names_by_tag.get(tag.casefold(), {})} if tags else None
        if query:
            return self._index.search(query, candidates)
        if candidates is None:
            return list(self._operations)
        return [name for name in self._operations if name in candidates]


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
