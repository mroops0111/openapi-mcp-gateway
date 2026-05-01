import inspect
import json
import keyword
import re
import typing

import inflection
import pydantic
from mcp.server.fastmcp import Context, FastMCP

from .auth.resolver import AuthResolver, NullAuthResolver
from .client import APIClient
from .openapi import OperationInfo, ParameterInfo


_INVALID_IDENT_CHARS = re.compile(r'[^A-Za-z0-9_]')


def _sanitize_name(name: str) -> str:
    """Replace characters not allowed in Python identifiers with underscores.

    Used for both tool names (e.g. ``meta/root`` → ``meta_root``) and parameter
    names (e.g. ``enterprise-team`` → ``enterprise_team``). Python keywords
    are suffixed with ``_`` (PEP 8 convention). The original name is preserved
    separately for path/query/header substitution.
    """
    sanitized = _INVALID_IDENT_CHARS.sub('_', name)
    if sanitized and sanitized[0].isdigit():
        sanitized = '_' + sanitized
    if keyword.iskeyword(sanitized):
        sanitized += '_'
    return sanitized


class ToolGenerator:
    """Generates MCP tools from a list of OpenAPI operations."""

    def __init__(
        self,
        mcp: FastMCP,
        base_url: str,
        auth_resolver: AuthResolver | None = None,
        timeout: float = 90,
    ):
        self.mcp = mcp
        self.base_url = base_url
        self.auth_resolver = auth_resolver or NullAuthResolver()
        self.timeout = timeout

    def register_operations(self, operations: list[OperationInfo]) -> None:
        for operation in operations:
            self._register_tool(operation)

    def _register_tool(self, operation: OperationInfo) -> None:
        # Separate parameters by location
        url_params = [p for p in operation.parameters if p.location in ('path', 'query', 'header')]
        body_params = [p for p in operation.parameters if p.location == 'body']

        tool_function = self._generate_tool_function(
            method=operation.method,
            path=operation.path,
            url_params=url_params,
            body_params=body_params,
        )
        tool_name = _sanitize_name(inflection.underscore(operation.operation_id))
        description = operation.description or operation.summary or f'{operation.method.upper()} {operation.path}'
        self.mcp.tool(name=tool_name, description=description)(tool_function)

    def _generate_tool_function(
        self,
        method: str,
        path: str,
        url_params: list[ParameterInfo],
        body_params: list[ParameterInfo],
    ) -> typing.Callable:
        """Build a tool function with a synthetic signature matching the operation's parameters."""
        base_url = self.base_url
        auth_resolver = self.auth_resolver
        timeout = self.timeout

        # Map sanitized python identifier → ParameterInfo (carries original API name)
        url_param_by_pname: dict[str, ParameterInfo] = {_sanitize_name(p.name): p for p in url_params}
        body_param_by_pname: dict[str, ParameterInfo] = {_sanitize_name(p.name): p for p in body_params}

        async def tool_function(**kwargs: typing.Any) -> str:
            ctx: Context = kwargs.pop('ctx')
            await ctx.report_progress(0, 1, f'Sending request to {method.upper()} {path} ...')

            # Resolve auth header dynamically
            auth_header = await auth_resolver.resolve(ctx)

            # Build path with path params substituted
            resolved_path = path
            query_params: dict[str, typing.Any] = {}
            extra_headers: dict[str, str] = {}

            for pname, param_info in url_param_by_pname.items():
                value = kwargs.get(pname)
                if value is None:
                    continue
                original = param_info.name
                if f'{{{original}}}' in resolved_path:
                    resolved_path = resolved_path.replace(f'{{{original}}}', str(value))
                elif param_info.location == 'header':
                    extra_headers[original] = str(value)
                else:
                    query_params[original] = value

            body = {
                param_info.name: kwargs[pname]
                for pname, param_info in body_param_by_pname.items()
                if kwargs.get(pname) is not None
            }

            headers: dict[str, str] = {}
            if auth_header:
                headers['Authorization'] = auth_header
            headers.update(extra_headers)

            async with APIClient(base_url=base_url, headers=headers, timeout=timeout) as client:
                result = await client.request(
                    method,
                    resolved_path,
                    params=query_params or None,
                    data=body or None,
                )

            await ctx.report_progress(1, 1, 'Request completed')
            return json.dumps(result, indent=2, ensure_ascii=False)

        # Build signature: required params → ctx → optional params
        # Deduplicate by sanitized name (first occurrence wins)
        seen_pnames: set[str] = set()
        ordered_params: list[tuple[str, ParameterInfo]] = []
        for p in url_params + body_params:
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
    """Convert an OpenAPI schema type to a Python type annotation."""
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
