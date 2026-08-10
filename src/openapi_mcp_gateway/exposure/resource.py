import inspect
import logging
import typing

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent

from ..openapi import OperationInfo, ParameterInfo
from ._shared import (
    _get_override,
    _iter_unique_sanitised_parameters,
    _sanitize_name,
    _schema_to_python_type,
    _split_by_location,
    derive_description,
    derive_name,
)
from ._upstream import UpstreamBinding, _build_upstream_closure


logger = logging.getLogger(__name__)


def derive_resource_mime_type(operation: OperationInfo) -> str:
    """Return the MIME type for ``operation`` exposed as a resource.

    Honors ``x-mcp-integration.expose.resource.mime_type`` when set, otherwise defaults to ``application/json``.
    """
    override = _get_override(operation, 'resource')
    if override and override.mime_type:
        return override.mime_type
    return 'application/json'


def derive_resource_uri(server_name: str, operation: OperationInfo) -> str:
    """Return the URI template for ``operation`` exposed as an MCP resource.

    Honors ``x-mcp-integration.expose.resource.uri_template`` verbatim when set.
    Otherwise builds ``{server_name}://{path}``, rewriting OpenAPI path placeholders into sanitised Python identifiers,
    so MCPServer's ``{(\\w+)}`` template regex matches.
    """
    override = _get_override(operation, 'resource')
    if override and override.uri_template:
        return override.uri_template
    path = operation.path.lstrip('/')
    for parameter in operation.parameters:
        if parameter.location == 'path':
            sanitized_name = _sanitize_name(parameter.name)
            if sanitized_name != parameter.name:
                path = path.replace(f'{{{parameter.name}}}', f'{{{sanitized_name}}}')
    return f'{server_name}://{path}'


def _extract_text_from_result(result: CallToolResult) -> str:
    """Pull the text body off ``result``, narrowing ``content[0]`` to ``TextContent``."""
    first_block = result.content[0] if result.content else None
    return first_block.text if isinstance(first_block, TextContent) else ''


def _build_resource_signature(
    path_parameters: list[ParameterInfo],
) -> tuple[inspect.Signature, dict[str, typing.Any]]:
    """Build the signature MCPServer introspects to decide concrete vs template registration.

    With path parameters: ``(path_parameter_1, ..., ctx: Context) -> str`` (template).
    Without: empty signature (concrete resource).
    """
    annotations: dict[str, typing.Any] = {}
    signature_parameters: list[inspect.Parameter] = []
    for parameter_name, parameter in _iter_unique_sanitised_parameters(path_parameters):
        python_type = _schema_to_python_type(parameter.schema_)
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
    annotations['return'] = str
    return inspect.Signature(signature_parameters), annotations


def build_resource_read_function(operation: OperationInfo, binding: UpstreamBinding) -> typing.Callable:
    """Build the async read function for ``operation`` exposed as an MCP resource.

    Reuses the upstream closure from the tool path, so auth, path substitution, and error handling stay identical.
    Signature is conditional on whether the operation has path parameters,
    which dictates how MCPServer registers the resource:

    - **Has path parameters**: signature is ``(p1, p2, ..., ctx: Context)``.
      MCPServer injects the real ``Context`` per call and registers as a **template**.
      Future progress notifications, passthrough auth, and per-call logging all have a real ``ctx`` to work with.
    - **No path parameters**: empty signature.
      MCPServer registers as a **concrete resource**, and a :class:`_NullContext` is supplied internally.
      ``PassthroughAuthResolver`` will return ``{}`` for these,
      which is acceptable because no FastAPI-resource integration exists today.

    Optional query / header / body parameters are dropped from the resource surface in both cases.
    Callers cannot pass them, and the upstream sees defaults.
    """
    upstream_callable = _build_upstream_closure(operation, binding)
    path_parameters, _, _, _ = _split_by_location(operation.parameters)
    null_context = typing.cast(Context, _NullContext())

    async def read_function(**kwargs: typing.Any) -> str:
        context = kwargs.pop('ctx', null_context)
        result = await upstream_callable(ctx=context, **kwargs)
        text = _extract_text_from_result(result)
        if result.is_error:
            raise RuntimeError(text or 'Upstream error reading resource')
        return text

    signature, annotations = _build_resource_signature(path_parameters)
    read_function.__signature__ = signature
    read_function.__annotations__ = annotations
    return read_function


class _NullContext:
    """Stand-in ``Context`` for concrete (no-path-param) resource reads.

    MCPServer registers a resource as concrete only when the read function takes no parameters,
    so concrete reads cannot declare ``ctx``.
    This shim feeds the shared upstream closure a context-shaped object that no-ops on progress reporting,
    and returns no header data for auth resolution.
    """

    request_context = None

    async def report_progress(self, *_args: typing.Any, **_kwargs: typing.Any) -> None:
        return None


class ResourceGenerator:
    """Register one MCP resource per ``OperationInfo`` in the input list.

    Inputs are assumed to be GET operations with no required non-path parameters;
    URI placeholders are sanitised to valid Python identifiers so MCPServer's template regex matches.
    """

    def __init__(self, mcp: MCPServer, binding: UpstreamBinding, server_name: str):
        self.mcp = mcp
        self.binding = binding
        self.server_name = server_name

    def register(self, operations: list[OperationInfo]) -> list[str]:
        """Declare one MCP resource per ``OperationInfo`` in ``operations``, returning the resource names."""
        resource_names: list[str] = []
        for operation in operations:
            override = _get_override(operation, 'resource')
            uri = derive_resource_uri(self.server_name, operation)
            read_function = build_resource_read_function(operation, self.binding)
            name = derive_name(operation, override.name if override else None)
            self.mcp.resource(
                uri,
                name=name,
                description=derive_description(operation, override.description if override else None),
                mime_type=derive_resource_mime_type(operation),
            )(read_function)
            resource_names.append(name)
            logger.debug('Resource registered: %s ← %s %s', uri, operation.method.upper(), operation.path)
        logger.info('Registered %d MCP resource(s) on server "%s"', len(operations), self.mcp.name)
        return resource_names
