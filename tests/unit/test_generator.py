"""Tests for tool generation, focused on name sanitization."""

import inspect
import typing

import httpx
import pytest
from mcp.server.fastmcp import Context, FastMCP

from openapi_mcp_gateway.client import APIClient
from openapi_mcp_gateway.generator import ToolGenerator, _sanitize_name
from openapi_mcp_gateway.openapi import OperationInfo, ParameterInfo


class _StubContext:
    async def report_progress(self, *_args, **_kwargs):
        return None


def _stub_ctx() -> Context:
    return typing.cast(Context, _StubContext())


class TestSanitizeName:
    def test_slash_to_underscore(self):
        assert _sanitize_name('meta/root') == 'meta_root'

    def test_dash_to_underscore(self):
        assert _sanitize_name('enterprise-team') == 'enterprise_team'

    def test_dot_to_underscore(self):
        assert _sanitize_name('foo.bar') == 'foo_bar'

    def test_leading_digit_prefixed(self):
        assert _sanitize_name('1st_param') == '_1st_param'

    def test_already_valid_unchanged(self):
        assert _sanitize_name('valid_name') == 'valid_name'

    def test_python_keyword_suffixed(self):
        assert _sanitize_name('async') == 'async_'
        assert _sanitize_name('class') == 'class_'
        assert _sanitize_name('from') == 'from_'


class TestToolGeneration:
    def _generator(self) -> tuple[ToolGenerator, FastMCP]:
        mcp = FastMCP('test')
        return ToolGenerator(mcp=mcp, base_url='https://api.example.com'), mcp

    def test_tool_name_with_slash_sanitized(self):
        gen, mcp = self._generator()
        op = OperationInfo(
            operation_id='meta/root',
            method='get',
            path='/',
        )
        gen.register_operations([op])
        # Tool registered under sanitized name
        assert 'meta_root' in {t.name for t in mcp._tool_manager.list_tools()}

    def test_param_name_with_dash_sanitized(self):
        gen, mcp = self._generator()
        op = OperationInfo(
            operation_id='get_thing',
            method='get',
            path='/things',
            parameters=[
                ParameterInfo(name='enterprise-team', location='query', required=True, schema={'type': 'string'}),
            ],
        )
        gen.register_operations([op])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_thing')
        sig = inspect.signature(tool.fn)
        assert 'enterprise_team' in sig.parameters
        assert 'enterprise-team' not in sig.parameters

    @pytest.mark.asyncio
    async def test_dashed_param_sent_with_original_name(self, monkeypatch):
        """Sanitized python name maps back to original API name when calling upstream."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['url'] = str(request.url)
            return httpx.Response(200, json={'ok': True})

        original_init = APIClient.__init__

        def patched_init(self, base_url, headers=None, timeout=90):
            original_init(self, base_url=base_url, headers=headers, timeout=timeout)
            self._client = httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))

        monkeypatch.setattr(APIClient, '__init__', patched_init)

        gen, mcp = self._generator()
        op = OperationInfo(
            operation_id='get_thing',
            method='get',
            path='/things',
            parameters=[
                ParameterInfo(name='enterprise-team', location='query', required=True, schema={'type': 'string'}),
            ],
        )
        gen.register_operations([op])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_thing')
        await tool.run({'enterprise_team': 'foo'}, context=_stub_ctx())
        assert 'enterprise-team=foo' in captured['url']

    @pytest.mark.asyncio
    async def test_path_param_with_dash_substituted(self, monkeypatch):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['path'] = request.url.path
            return httpx.Response(200, json={'ok': True})

        original_init = APIClient.__init__

        def patched_init(self, base_url, headers=None, timeout=90):
            original_init(self, base_url=base_url, headers=headers, timeout=timeout)
            self._client = httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))

        monkeypatch.setattr(APIClient, '__init__', patched_init)

        gen, mcp = self._generator()
        op = OperationInfo(
            operation_id='get_team',
            method='get',
            path='/teams/{enterprise-team}',
            parameters=[
                ParameterInfo(name='enterprise-team', location='path', required=True, schema={'type': 'string'}),
            ],
        )
        gen.register_operations([op])
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'get_team')
        await tool.run({'enterprise_team': 'acme'}, context=_stub_ctx())
        assert captured['path'] == '/teams/acme'
