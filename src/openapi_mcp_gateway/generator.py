"""Dynamically generate MCP tools from OpenAPI operations."""

import inspect
import json
import typing

import inflection
import pydantic
from mcp.server.fastmcp import Context, FastMCP

from .auth.resolver import AuthResolver, NullAuthResolver
from .client import APIClient
from .openapi import OperationInfo, ParameterInfo


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
        tool_name = inflection.underscore(operation.operation_id)
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

        url_param_names = [p.name for p in url_params]
        body_param_names = [p.name for p in body_params]
        all_params = url_params + body_params

        async def tool_function(**kwargs: typing.Any) -> str:
            ctx: Context = kwargs.pop('ctx')
            await ctx.report_progress(0, 1, f'Sending request to {method.upper()} {path} ...')

            # Resolve auth header dynamically
            auth_header = await auth_resolver.resolve(ctx)

            # Build path with path params substituted
            resolved_path = path
            query_params: dict[str, typing.Any] = {}
            extra_headers: dict[str, str] = {}

            for name in url_param_names:
                value = kwargs.get(name)
                if value is None:
                    continue
                if f'{{{name}}}' in resolved_path:
                    resolved_path = resolved_path.replace(f'{{{name}}}', str(value))
                else:
                    param_info = next((p for p in url_params if p.name == name), None)
                    if param_info and param_info.location == 'header':
                        extra_headers[name] = str(value)
                    else:
                        query_params[name] = value

            body = {name: kwargs[name] for name in body_param_names if kwargs.get(name) is not None}

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
        # Deduplicate by name (first occurrence wins)
        seen_names: set[str] = set()
        unique_params: list[ParameterInfo] = []
        for p in all_params:
            if p.name not in seen_names:
                seen_names.add(p.name)
                unique_params.append(p)
        all_params = unique_params

        annotations: dict[str, typing.Any] = {}
        signature_params: list[inspect.Parameter] = []

        for param in all_params:
            if param.required:
                py_type = _schema_to_python_type(param.schema_)
                ann = (
                    typing.Annotated[py_type, pydantic.Field(description=param.description)]
                    if param.description
                    else py_type
                )
                signature_params.append(
                    inspect.Parameter(name=param.name, kind=inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=ann)
                )
                annotations[param.name] = ann

        signature_params.append(
            inspect.Parameter(name='ctx', kind=inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context)
        )
        annotations['ctx'] = Context

        for param in all_params:
            if not param.required:
                py_type = _schema_to_python_type(param.schema_)
                ann = (
                    typing.Annotated[py_type | None, pydantic.Field(description=param.description)]
                    if param.description
                    else py_type | None
                )
                signature_params.append(
                    inspect.Parameter(
                        name=param.name,
                        kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        annotation=ann,
                        default=None,
                    )
                )
                annotations[param.name] = ann

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
