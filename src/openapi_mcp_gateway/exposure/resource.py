import inspect
import logging
import typing

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, TextContent

from ..openapi import OperationInfo, ParameterInfo
from ._shared import (
    UpstreamBinding,
    _get_override,
    _iter_unique_sanitised_parameters,
    _sanitize_name,
    _schema_to_python_type,
    _split_by_location,
    derive_description,
    derive_name,
)
from ._upstream import _build_upstream_closure


logger = logging.getLogger(__name__)


def derive_resource_mime_type(operation: OperationInfo) -> str:
    """Return the MIME type for ``operation`` exposed as a resource.

    Honors ``x-mcp-integration.expose.resource.mime_type`` when set,
    otherwise defaults to ``application/json``.
    """
    override = _get_override(operation, 'resource')
    if override and override.mime_type:
        return override.mime_type
    return 'application/json'


def derive_resource_uri(server_name: str, operation: OperationInfo) -> str:
    """Return the URI template for ``operation`` exposed as an MCP resource.

    Honors ``x-mcp-integration.expose.resource.uri_template`` verbatim when set.
    Otherwise builds ``{server_name}://{path}``,
    rewriting OpenAPI path placeholders into sanitised Python identifiers,
    so FastMCP's ``{(\\w+)}`` template regex matches.
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
    """Build the signature FastMCP introspects to decide concrete vs template registration.

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

    Reuses the upstream closure from the tool path,
    so auth, path substitution, and error handling stay identical.
    Signature is conditional on whether the operation has path parameters,
    which dictates how FastMCP registers the resource:

    - **Has path parameters**: signature is ``(p1, p2, ..., ctx: Context)``.
      FastMCP injects the real ``Context`` per call and registers as a **template**.
      Future progress notifications, passthrough auth, and per-call logging all have a real ``ctx`` to work with.
    - **No path parameters**: empty signature.
      FastMCP registers as a **concrete resource**, and a :class:`_NullContext` is supplied internally.
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
        if result.isError:
            raise RuntimeError(text or 'Upstream error reading resource')
        return text

    signature, annotations = _build_resource_signature(path_parameters)
    read_function.__signature__ = signature
    read_function.__annotations__ = annotations
    return read_function


class _NullContext:
    """Stand-in ``Context`` for **concrete** resource reads only.

    Concrete vs template registration in FastMCP turns on the read function's parameter list (``server.py:594-597``).
    Any parameter including ``ctx`` forces template registration.
    A no-path-param GET that wants to surface under ``resources/list`` therefore cannot declare ``ctx``,
    so the shared upstream closure runs against this shim instead.
    Template resources keep the real injected ``Context`` (see :func:`build_resource_read_function`).

    ``report_progress`` is a no-op and ``request_context`` is ``None``.
    ``PassthroughAuthResolver`` is the only ctx-aware resolver,
    and it gracefully returns ``{}`` against the null shim.
    Contextvar-based resolvers (``authorization_code``, ``client_credentials``) read identity off the surrounding ASGI scope,
    not ``ctx``,
    so they are unaffected either way.
    """

    request_context = None

    async def report_progress(self, *_args: typing.Any, **_kwargs: typing.Any) -> None:
        return None


class ResourceGenerator:
    """Static exposure: register one MCP resource per eligible GET operation.

    Eligibility is validated upstream by ``_partition_resource_operations`` (gateway side),
    so this generator assumes every input operation is GET-only with no required non-path parameters.
    The URI template's placeholders are sanitised Python identifiers matching the read function's signature,
    so FastMCP's regex match succeeds.
    """

    def __init__(self, mcp: FastMCP, binding: UpstreamBinding, server_name: str):
        self.mcp = mcp
        self.binding = binding
        self.server_name = server_name

    def register(self, operations: list[OperationInfo]) -> None:
        """Declare one MCP resource per ``OperationInfo`` in ``operations``."""
        for operation in operations:
            override = _get_override(operation, 'resource')
            uri = derive_resource_uri(self.server_name, operation)
            read_function = build_resource_read_function(operation, self.binding)
            self.mcp.resource(
                uri,
                name=derive_name(operation, override.name if override else None),
                description=derive_description(operation, override.description if override else None),
                mime_type=derive_resource_mime_type(operation),
            )(read_function)
            logger.debug('Resource registered: %s ← %s %s', uri, operation.method.upper(), operation.path)
        logger.info('Registered %d MCP resource(s) on server "%s"', len(operations), self.mcp.name)
