import inspect
import json
import keyword
import logging
import re
import typing

import httpx
import inflection
import pydantic
from mcp.server.fastmcp import Context, FastMCP

from .auth.resolver import AuthResolver, NullAuthResolver
from .client import APIClient
from .openapi import OperationInfo, ParameterInfo


logger = logging.getLogger(__name__)


_INVALID_IDENT_CHARS = re.compile(r'[^A-Za-z0-9_]')


def _sanitize_name(name: str) -> str:
    """Return a valid Python identifier, prefixing digits and suffixing keywords."""
    sanitized = _INVALID_IDENT_CHARS.sub('_', name)
    if sanitized and sanitized[0].isdigit():
        sanitized = '_' + sanitized
    if keyword.iskeyword(sanitized):
        sanitized += '_'
    return sanitized


class ToolGenerator:
    """Bind OpenAPI operations onto a FastMCP server as callable tools."""

    def __init__(
        self,
        mcp: FastMCP,
        base_url: str,
        auth_resolver: AuthResolver | None = None,
        timeout: float = 90,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        """Store MCP server handle, upstream URL, auth resolver, timeout, and transport.

        ``transport`` is forwarded to every per-request ``APIClient`` so the
        gateway can route upstream calls in-process (``httpx.ASGITransport``)
        instead of over the network. The ``auth_resolver`` is the single
        source of upstream headers — compose ``CompositeAuthResolver`` if
        more than one source is needed (e.g. forwarding ``X-API-Key`` from
        the MCP client alongside a gateway-minted ``Authorization``).
        """
        self.mcp = mcp
        self.base_url = base_url
        self.auth_resolver = auth_resolver or NullAuthResolver()
        self.timeout = timeout
        self.transport = transport

    def register_operations(self, operations: list[OperationInfo]) -> None:
        """Declare one MCP tool per ``OperationInfo`` after policy filtering."""
        for operation in operations:
            self._register_tool(operation)
        logger.info('Registered %d MCP tool(s) on server "%s"', len(operations), self.mcp.name)

    def _register_tool(self, operation: OperationInfo) -> None:
        """Register a single FastMCP tool with generated signature and description."""
        path_params = [p for p in operation.parameters if p.location == 'path']
        query_params = [p for p in operation.parameters if p.location == 'query']
        header_params = [p for p in operation.parameters if p.location == 'header']
        body_params = [p for p in operation.parameters if p.location == 'body']

        tool_function = self._generate_tool_function(
            method=operation.method,
            path=operation.path,
            path_params=path_params,
            query_params=query_params,
            header_params=header_params,
            body_params=body_params,
        )
        tool_name = _sanitize_name(inflection.underscore(operation.operation_id))
        description = operation.description or operation.summary or f'{operation.method.upper()} {operation.path}'
        self.mcp.tool(name=tool_name, description=description)(tool_function)
        logger.debug('Tool registered: %s ← %s %s', tool_name, operation.method.upper(), operation.path)

    def _generate_tool_function(
        self,
        method: str,
        path: str,
        path_params: list[ParameterInfo],
        query_params: list[ParameterInfo],
        header_params: list[ParameterInfo],
        body_params: list[ParameterInfo],
    ) -> typing.Callable:
        """Return an async callable with ``inspect.Signature`` matching parameters."""
        base_url = self.base_url
        auth_resolver = self.auth_resolver
        timeout = self.timeout
        transport = self.transport

        # Map sanitized python identifier → ParameterInfo (carries original API name)
        path_param_by_pname: dict[str, ParameterInfo] = {_sanitize_name(param.name): param for param in path_params}
        query_param_by_pname: dict[str, ParameterInfo] = {_sanitize_name(param.name): param for param in query_params}
        header_param_by_pname: dict[str, ParameterInfo] = {_sanitize_name(param.name): param for param in header_params}
        body_param_by_pname: dict[str, ParameterInfo] = {_sanitize_name(param.name): param for param in body_params}

        async def tool_function(**kwargs: typing.Any) -> str:
            ctx: Context = kwargs.pop('ctx')
            await ctx.report_progress(0, 1, f'Sending request to {method.upper()} {path} ...')

            auth_headers = await auth_resolver.resolve(ctx)

            resolved_path = path
            for pname, param_info in path_param_by_pname.items():
                value = kwargs.get(pname)
                if value is None:
                    continue
                resolved_path = resolved_path.replace(f'{{{param_info.name}}}', str(value))

            query_args: dict[str, typing.Any] = {
                param_info.name: kwargs[pname]
                for pname, param_info in query_param_by_pname.items()
                if kwargs.get(pname) is not None
            }

            header_args: dict[str, str] = {
                param_info.name: str(kwargs[pname])
                for pname, param_info in header_param_by_pname.items()
                if kwargs.get(pname) is not None
            }

            body = {
                param_info.name: kwargs[pname]
                for pname, param_info in body_param_by_pname.items()
                if kwargs.get(pname) is not None
            }

            headers: dict[str, str] = {**auth_headers, **header_args}

            async with APIClient(
                base_url=base_url,
                headers=headers,
                timeout=timeout,
                transport=transport,
            ) as client:
                result = await client.request(
                    method,
                    resolved_path,
                    params=query_args or None,
                    data=body or None,
                )

            await ctx.report_progress(1, 1, 'Request completed')
            return json.dumps(result, indent=2, ensure_ascii=False)

        # Build signature: required params → ctx → optional params
        # Deduplicate by sanitized name (first occurrence wins)
        seen_pnames: set[str] = set()
        ordered_params: list[tuple[str, ParameterInfo]] = []
        for p in path_params + query_params + header_params + body_params:
            pname = _sanitize_name(p.name)
            if pname not in seen_pnames:
                seen_pnames.add(pname)
                ordered_params.append((pname, p))

        annotations: dict[str, typing.Any] = {}
        signature_params: list[inspect.Parameter] = []

        for pname, param in ordered_params:
            if param.required:
                py_type = _schema_to_python_type(param.schema_)
                ann = (
                    typing.Annotated[py_type, pydantic.Field(description=param.description)]
                    if param.description
                    else py_type
                )
                signature_params.append(
                    inspect.Parameter(name=pname, kind=inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=ann)
                )
                annotations[pname] = ann

        signature_params.append(
            inspect.Parameter(name='ctx', kind=inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context)
        )
        annotations['ctx'] = Context

        for pname, param in ordered_params:
            if not param.required:
                py_type = _schema_to_python_type(param.schema_)
                ann = (
                    typing.Annotated[py_type | None, pydantic.Field(description=param.description)]
                    if param.description
                    else py_type | None
                )
                signature_params.append(
                    inspect.Parameter(
                        name=pname,
                        kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        annotation=ann,
                        default=None,
                    )
                )
                annotations[pname] = ann

        tool_function.__signature__ = inspect.Signature(signature_params)
        tool_function.__annotations__ = annotations
        return tool_function


def _schema_to_python_type(schema: dict[str, typing.Any]) -> typing.Any:
    """Map JSON Schema ``type`` / ``items`` to typing-compatible annotations."""
    schema_type = schema.get('type', 'string')

    if schema_type == 'string':
        return str
    if schema_type == 'integer':
        return int
    if schema_type == 'number':
        return float
    if schema_type == 'boolean':
        return bool
    if schema_type == 'array':
        items = schema.get('items', {})
        item_type = _schema_to_python_type(items)
        return list[item_type]
    if schema_type == 'object':
        return dict[str, typing.Any]
    return typing.Any
