import importlib
import json
import typing

import mcp.types
import pytest
from fastapi import FastAPI
from mcp.shared.memory import create_connected_server_and_client_session

from openapi_mcp_gateway import Gateway
from openapi_mcp_gateway.fastapi import _TOOL_METADATA_ATTR, ToolMetadata


pytestmark = pytest.mark.smoke


def _mark_routes(app: FastAPI, paths: list[str]) -> None:
    """Attach ``ToolMetadata(expose=True)`` to each route whose path matches one in ``paths``."""
    metadata = ToolMetadata(expose=True)
    for route in app.routes:
        endpoint = getattr(route, 'endpoint', None)
        if endpoint is None:
            continue
        if getattr(route, 'path', None) in paths:
            setattr(endpoint, _TOOL_METADATA_ATTR, metadata)


def _build_gateway(module_path: str, paths: list[str]) -> Gateway:
    """Import ``module_path``, mark its routes, and build a gateway from the FastAPI app."""
    module = importlib.import_module(module_path)
    app = typing.cast(FastAPI, module.app)
    _mark_routes(app, paths)
    return Gateway.from_fastapi(app, name='smoke')


def _decode_text_content(content_blocks: typing.Sequence[mcp.types.ContentBlock]) -> typing.Any:
    """Return the JSON-decoded payload of the first text content block."""
    for block in content_blocks:
        if isinstance(block, mcp.types.TextContent):
            return json.loads(block.text)
    raise AssertionError('no text content in tool result')


_NO_AUTH_CASES: list[tuple[str, str, list[str], dict[str, typing.Any], typing.Any]] = [
    (
        'docs_src.path_params.tutorial001_py310',
        '/items/{item_id}',
        ['/items/{item_id}'],
        {'item_id': 'foo'},
        {'item_id': 'foo'},
    ),
    (
        'docs_src.body.tutorial001_py310',
        '/items/',
        ['/items/'],
        {'name': 'x', 'price': 1.5},
        {'name': 'x', 'description': None, 'price': 1.5, 'tax': None},
    ),
    (
        'docs_src.query_params_str_validations.tutorial001_py310',
        '/items/',
        ['/items/'],
        {'q': 'hello'},
        {'items': [{'item_id': 'Foo'}, {'item_id': 'Bar'}], 'q': 'hello'},
    ),
]


@pytest.mark.parametrize(('module_path', 'route_path', 'paths', 'tool_args', 'expected'), _NO_AUTH_CASES)
async def test_no_auth_tutorial_round_trip(
    fastapi_docs_src,
    module_path: str,
    route_path: str,
    paths: list[str],
    tool_args: dict[str, typing.Any],
    expected: typing.Any,
):
    """No-security tutorials register a tool, list it, and return the expected route response."""
    del fastapi_docs_src, route_path  # fixture only sets sys.path; route_path is in `paths`
    gateway = _build_gateway(module_path, paths)
    bundle = gateway._servers[0]

    async with create_connected_server_and_client_session(bundle.mcp) as session:
        listed = await session.list_tools()
        assert len(listed.tools) == 1
        tool_name = listed.tools[0].name

        result = await session.call_tool(tool_name, tool_args)
        assert result.isError is False
        assert _decode_text_content(result.content) == expected


async def test_password_flow_tutorial_is_rejected(fastapi_docs_src):
    """``OAuth2PasswordBearer`` (password flow) is rejected with a clear error."""
    del fastapi_docs_src
    module = importlib.import_module('docs_src.security.tutorial001_py310')
    _mark_routes(module.app, ['/items/'])
    with pytest.raises(ValueError, match='unsupported OAuth2 flows'):
        Gateway.from_fastapi(module.app, name='smoke')


_HTTP_BASIC_CASES = [
    'docs_src.security.tutorial006_py310',
    'docs_src.security.tutorial007_py310',
]


@pytest.mark.parametrize('module_path', _HTTP_BASIC_CASES)
async def test_http_basic_tutorial_lists_tool(fastapi_docs_src, module_path: str):
    """HTTPBasic tutorials assemble cleanly under NullAuth and the MCP client can list the tool.

    HTTPBasic exercises the ``Authorization`` passthrough path, but the
    in-memory MCP transport doesn't carry HTTP headers, so this smoke only
    verifies tool discovery. The header-passthrough behaviour itself is
    covered by ``tests/integration/test_fastapi.py``.
    """
    del fastapi_docs_src
    gateway = _build_gateway(module_path, ['/users/me'])
    bundle = gateway._servers[0]

    async with create_connected_server_and_client_session(bundle.mcp) as session:
        listed = await session.list_tools()
        assert len(listed.tools) == 1
