import inspect
import logging
import typing

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent

from ..openapi import OperationInfo, ParameterInfo
from ..scopes import McpUnrestrictedScopePolicy, ScopeRegistry
from ._binary_content import binary_content_data
from ._shared import (
    SignatureTypes,
    derive_description,
    derive_name,
    get_override,
    iter_unique_sanitised_parameters,
    sanitize_name,
    schema_to_python_type,
    split_by_location,
)
from ._upstream_call import UpstreamConnection, UpstreamOperationCall


logger = logging.getLogger(__name__)


def derive_resource_mime_type(operation: OperationInfo) -> str:
    """Return the MIME type for ``operation`` exposed as a resource.

    Honors ``x-mcp-integration.expose.resource.mime_type`` when set, otherwise defaults to ``application/json``.
    """
    override = get_override(operation, 'resource')
    if override and override.mime_type:
        return override.mime_type
    return 'application/json'


def derive_resource_uri(server_name: str, operation: OperationInfo) -> str:
    """Return the URI template for ``operation`` exposed as an MCP resource.

    Honors ``x-mcp-integration.expose.resource.uri_template`` verbatim when set.
    Otherwise builds ``{server_name}://{path}``, rewriting OpenAPI path placeholders into sanitised Python identifiers,
    so MCPServer's ``{(\\w+)}`` template regex matches.
    """
    override = get_override(operation, 'resource')
    if override and override.uri_template:
        return override.uri_template
    path = operation.path.lstrip('/')
    for parameter in operation.parameters:
        if parameter.location == 'path':
            sanitized_name = sanitize_name(parameter.name)
            if sanitized_name != parameter.name:
                path = path.replace(f'{{{parameter.name}}}', f'{{{sanitized_name}}}')
    return f'{server_name}://{path}'


def build_resource_read_function(operation: OperationInfo, connection: UpstreamConnection) -> typing.Callable:
    """Build the async read function for ``operation`` exposed as an MCP resource.

    Reuses the tool path's ``UpstreamOperationCall``, so auth, path substitution, and error handling stay identical.
    Signature is conditional on whether the operation has path parameters,
    which dictates how MCPServer registers the resource:

    - **Has Path Parameters**: signature is ``(p1, p2, ..., ctx: Context)``.
      MCPServer injects the real ``Context`` per call and registers as a **template**.
      Future progress notifications, passthrough auth, and per-call logging all have a real ``ctx`` to work with.
    - **No Path Parameters**: empty signature.
      MCPServer registers as a **concrete resource**, and a :class:`_NullContext` is supplied internally.
      ``UpstreamPassthroughAuthResolver`` will return ``{}`` for these,
      which is acceptable because no FastAPI-resource integration exists today.

    Optional query / header / body parameters are dropped from the resource surface in both cases.
    Callers cannot pass them, and the upstream sees defaults.
    """
    upstream_call = UpstreamOperationCall(operation, connection)
    path_parameters, _, _, _ = split_by_location(operation.parameters)
    null_context = typing.cast(Context, _NullContext())

    async def read_function(**kwargs: typing.Any) -> str | bytes:
        context = kwargs.pop('ctx', null_context)
        # A resource read never offers a new sign-in, so the call always finishes in one round.
        result = typing.cast(CallToolResult, await upstream_call(ctx=context, **kwargs))
        body = _extract_body_from_result(result)
        if result.is_error:
            raise RuntimeError(body or 'Upstream error reading resource')
        return body

    signature, annotations = _build_resource_signature(path_parameters, operation.schema_defs)
    read_function.__signature__ = signature
    read_function.__annotations__ = annotations
    return read_function


class ResourceGenerator:
    """Register one MCP resource per ``OperationInfo`` in the input list.

    Inputs are assumed to be GET operations with no required non-path parameters.
    URI placeholders are sanitised to valid Python identifiers, so MCPServer's template regex matches.
    Each resource's scope requirement is recorded in ``scope_registry`` under its URI or URI template,
    which is what a ``resources/read`` and its ``Mcp-Name`` header carry.
    """

    def __init__(
        self,
        mcp: MCPServer,
        connection: UpstreamConnection,
        server_name: str,
        scope_registry: ScopeRegistry | None = None,
    ):
        self.mcp = mcp
        self.connection = connection
        self.server_name = server_name
        self.scope_registry = scope_registry or ScopeRegistry(McpUnrestrictedScopePolicy())

    def register(self, operations: list[OperationInfo]) -> list[str]:
        """Declare one MCP resource per ``OperationInfo`` in ``operations``, returning the resource names."""
        resource_names: list[str] = []
        for operation in operations:
            override = get_override(operation, 'resource')
            uri = derive_resource_uri(self.server_name, operation)
            read_function = build_resource_read_function(operation, self.connection)
            name = derive_name(operation, override.name if override else None)
            self.mcp.resource(
                uri,
                name=name,
                description=derive_description(operation, override.description if override else None),
                mime_type=derive_resource_mime_type(operation),
            )(read_function)
            self.scope_registry.register_resource(uri, operation)
            resource_names.append(name)
            logger.debug('Resource registered: %s ← %s %s', uri, operation.method.upper(), operation.path)
        logger.info('Registered %d MCP resource(s) on server "%s"', len(operations), self.mcp.name)
        return resource_names


def _extract_body_from_result(result: CallToolResult) -> str | bytes:
    """Pull the body off ``result``, as text from a text block and as bytes from a binary one.

    MCPServer serves returned bytes as a blob, so a binary body reaches the client as it left the upstream.
    """
    first_content = result.content[0] if result.content else None
    if first_content is None:
        return ''
    if isinstance(first_content, TextContent):
        return first_content.text
    data = binary_content_data(first_content)
    return data if data is not None else ''


def _build_resource_signature(
    path_parameters: list[ParameterInfo],
    schema_defs: dict[str, dict[str, typing.Any]],
) -> tuple[inspect.Signature, dict[str, typing.Any]]:
    """Build the signature MCPServer introspects to decide concrete vs template registration.

    With path parameters, the signature is ``(path_parameter_1, ..., ctx: Context) -> str | bytes``,
    so MCPServer registers a template.
    Without them, the signature is empty, so it registers a concrete resource.
    """
    annotations: dict[str, typing.Any] = {}
    signature_parameters: list[inspect.Parameter] = []
    signature_types = SignatureTypes(schema_defs, prefix='')
    for parameter_name, parameter in iter_unique_sanitised_parameters(path_parameters):
        python_type = schema_to_python_type(parameter.schema_, signature_types=signature_types)
        signature_parameters.append(
            inspect.Parameter(
                name=parameter_name,
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=python_type,
            )
        )
        annotations[parameter_name] = python_type
    if path_parameters:
        signature_parameters.append(
            inspect.Parameter(name='ctx', kind=inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context)
        )
        annotations['ctx'] = Context
    annotations['return'] = str | bytes
    return inspect.Signature(signature_parameters), annotations


class _NullContext:
    """Stand-in ``Context`` for concrete (no-path-param) resource reads.

    MCPServer registers a resource as concrete only when the read function takes no parameters,
    so concrete reads cannot declare ``ctx``.
    This shim feeds the shared ``UpstreamOperationCall`` a context-shaped object that no-ops on progress reporting,
    and returns no header data for auth resolution.
    """

    request_context = None

    async def report_progress(self, *_args: typing.Any, **_kwargs: typing.Any) -> None:
        return None
