import json
import typing
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import APIKeyHeader, HTTPBasic, HTTPBasicCredentials, OAuth2AuthorizationCodeBearer
from mcp.server.fastmcp import Context

from openapi_mcp_gateway import Gateway, mcp_tool
from openapi_mcp_gateway.auth import token_source as token_source_module
from openapi_mcp_gateway.settings import AuthConfig


class _StubRequestContext:
    """Mimic FastMCP's request_context with a Starlette-like ``request.headers``."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.request = type('_Req', (), {'headers': headers})()


class _StubContext:
    """No-op MCP context exposing ``report_progress`` and ``request_context``."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.request_context = _StubRequestContext(headers or {})

    async def report_progress(self, *_args, **_kwargs):
        """Match the ``Context`` protocol; do nothing."""
        return None


def _stub_context(headers: dict[str, str] | None = None) -> Context:
    """Return a ``Context``-typed stub carrying optional incoming headers."""
    return typing.cast(Context, _StubContext(headers))


class TestGatewayFromFastapiAssembly:
    """``Gateway.from_fastapi`` wires decorators, transport, and auto-detected auth."""

    def test_no_auth_no_provider(self):
        """A FastAPI app without security schemes registers a server without an auth provider."""
        app = FastAPI()

        @app.get('/ping')
        @mcp_tool()
        def ping():
            return {'pong': True}

        gateway = Gateway.from_fastapi(app, name='svc')
        bundle = gateway._servers[0]
        assert bundle.auth_provider is None
        assert bundle.auth_settings is None
        assert gateway._shutdown_hooks == []

    def test_authorization_code_without_creds_picks_passthrough(self):
        """No client credentials but an authorizationCode scheme falls back to passthrough."""
        oauth = OAuth2AuthorizationCodeBearer(
            authorizationUrl='https://auth.example.com/authorize',
            tokenUrl='https://auth.example.com/token',
        )
        app = FastAPI()

        @app.get('/me')
        @mcp_tool()
        def me(_token: str = Depends(oauth)):
            return {'me': True}

        gateway = Gateway.from_fastapi(app, name='svc')
        bundle = gateway._servers[0]
        # Passthrough setup carries no provider, no settings, no shutdown hook.
        assert bundle.auth_provider is None
        assert bundle.auth_settings is None
        assert gateway._shutdown_hooks == []

    def test_client_credentials_uses_token_source(self):
        """A clientCredentials scheme + creds registers a service-token shutdown hook."""
        app = FastAPI()

        @app.get('/me')
        @mcp_tool()
        def me():
            return {'me': True}

        original_openapi = app.openapi

        def _openapi_with_cc():
            schema = original_openapi()
            schema.setdefault('components', {})['securitySchemes'] = {
                'oauth2': {
                    'type': 'oauth2',
                    'flows': {
                        'clientCredentials': {
                            'tokenUrl': 'https://auth.example.com/token',
                            'scopes': {'api': 'API access'},
                        },
                    },
                },
            }
            return schema

        app.openapi = _openapi_with_cc  # type: ignore[method-assign]

        gateway = Gateway.from_fastapi(
            app,
            name='svc',
            auth=AuthConfig(type='oauth2', client_id='cid', client_secret='sec'),
        )
        # client_credentials registers a shutdown hook that closes the token source's HTTP client.
        assert len(gateway._shutdown_hooks) == 1

    def test_no_marked_routes_raises(self):
        """A FastAPI app without any ``@mcp_tool`` route is a configuration error."""
        app = FastAPI()

        @app.get('/ping')
        def ping():
            return {'pong': True}

        with pytest.raises(ValueError, match='no routes are decorated with @mcp_tool'):
            Gateway.from_fastapi(app, name='svc')

    def test_unsupported_oauth_only_rejected(self):
        """A spec declaring only password/implicit flows is rejected with a clear error."""
        app = FastAPI()

        @app.get('/ping')
        @mcp_tool()
        def ping():
            return {'pong': True}

        original_openapi = app.openapi

        def _openapi_with_password():
            schema = original_openapi()
            schema.setdefault('components', {})['securitySchemes'] = {
                'oauth2': {
                    'type': 'oauth2',
                    'flows': {
                        'password': {
                            'tokenUrl': 'https://auth.example.com/token',
                            'scopes': {},
                        },
                    },
                },
            }
            return schema

        app.openapi = _openapi_with_password  # type: ignore[method-assign]

        with pytest.raises(ValueError, match='unsupported OAuth2 flows'):
            Gateway.from_fastapi(app, name='svc')


class TestGatewayFromFastapiToolCall:
    """End-to-end: tool invocation reaches the same-process FastAPI app via ASGI."""

    async def test_tool_invocation_returns_route_response(self):
        """Calling the generated MCP tool reaches the FastAPI route in-process."""
        app = FastAPI()

        @app.get('/users/{user_id}')
        @mcp_tool(name='lookup_user')
        def get_user(user_id: str):
            return {'user_id': user_id, 'echo': 'ok'}

        gateway = Gateway.from_fastapi(app, name='svc')
        mcp = gateway._servers[0].mcp
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == 'lookup_user')

        result = await tool.run({'user_id': '42'}, context=_stub_context())
        assert result.isError is False
        assert result.structuredContent == {'user_id': '42', 'echo': 'ok'}

    async def test_passthrough_forwards_authorization_header(self):
        """Passthrough mode delivers the MCP client's Authorization header to the FastAPI route."""
        oauth = OAuth2AuthorizationCodeBearer(
            authorizationUrl='https://auth.example.com/authorize',
            tokenUrl='https://auth.example.com/token',
            auto_error=False,
        )
        app = FastAPI()

        @app.get('/me')
        @mcp_tool()
        def me(authorization: str | None = Header(default=None), _token: str | None = Depends(oauth)):
            if not authorization:
                raise HTTPException(401, 'missing token')
            return {'authorization': authorization}

        gateway = Gateway.from_fastapi(app, name='svc')
        mcp = gateway._servers[0].mcp
        tool = next(iter(mcp._tool_manager.list_tools()))

        ctx = _stub_context({'authorization': 'Bearer client-token'})
        result = await tool.run({}, context=ctx)
        assert result.structuredContent == {'authorization': 'Bearer client-token'}

    async def test_authorization_forwarded_under_null_auth(self):
        """HTTPBasic-style routes (no OpenAPI securityScheme) still receive Authorization."""
        scheme = HTTPBasic(auto_error=False)
        app = FastAPI()

        @app.get('/me')
        @mcp_tool()
        def me(creds: HTTPBasicCredentials | None = Depends(scheme)):
            if creds is None:
                raise HTTPException(401, 'missing creds')
            return {'username': creds.username}

        gateway = Gateway.from_fastapi(app, name='svc')
        mcp = gateway._servers[0].mcp
        tool = next(iter(mcp._tool_manager.list_tools()))

        ctx = _stub_context({'authorization': 'Basic Zm9vOmJhcg=='})
        result = await tool.run({}, context=ctx)
        assert result.structuredContent == {'username': 'foo'}

    async def test_x_api_key_forwarded_by_default(self):
        """``X-API-Key`` arrives at the FastAPI route without explicit configuration."""
        api_key_scheme = APIKeyHeader(name='X-API-Key', auto_error=False)
        app = FastAPI()

        @app.get('/secured')
        @mcp_tool()
        def secured(api_key: str | None = Depends(api_key_scheme)):
            if not api_key:
                raise HTTPException(401, 'missing key')
            return {'api_key': api_key}

        gateway = Gateway.from_fastapi(app, name='svc')
        mcp = gateway._servers[0].mcp
        tool = next(iter(mcp._tool_manager.list_tools()))

        ctx = _stub_context({'x-api-key': 'top-secret'})
        result = await tool.run({}, context=ctx)
        assert result.structuredContent == {'api_key': 'top-secret'}

    async def test_resolver_authorization_wins_over_passthrough(self, monkeypatch):
        """In client_credentials mode the gateway-minted Authorization is not overwritten by the client's."""
        token_response = MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {'access_token': 'gateway-token', 'expires_in': 3600}
        token_response.text = ''
        monkeypatch.setattr(
            token_source_module.httpx.AsyncClient,
            'post',
            AsyncMock(return_value=token_response),
            raising=False,
        )

        app = FastAPI()
        original_openapi = app.openapi

        def _openapi_with_cc():
            schema = original_openapi()
            schema.setdefault('components', {})['securitySchemes'] = {
                'oauth2': {
                    'type': 'oauth2',
                    'flows': {
                        'clientCredentials': {
                            'tokenUrl': 'https://auth.example.com/token',
                            'scopes': {'api': 'API access'},
                        },
                    },
                },
            }
            return schema

        app.openapi = _openapi_with_cc  # type: ignore[method-assign]

        @app.get('/echo')
        @mcp_tool()
        def echo(authorization: str | None = Header(default=None)):
            return {'authorization': authorization}

        gateway = Gateway.from_fastapi(
            app,
            name='svc',
            auth=AuthConfig(type='oauth2', client_id='cid', client_secret='sec'),
        )
        mcp = gateway._servers[0].mcp
        tool = next(iter(mcp._tool_manager.list_tools()))

        # MCP client tries to send its own Authorization; gateway must use gateway-token instead.
        ctx = _stub_context({'authorization': 'Bearer client-token'})
        result = await tool.run({}, context=ctx)
        assert result.structuredContent == {'authorization': 'Bearer gateway-token'}

    async def test_extra_passthrough_header_via_kwarg(self):
        """Custom header names listed in ``passthrough_headers`` reach the upstream call."""
        app = FastAPI()

        @app.get('/whoami')
        @mcp_tool()
        def whoami(x_tenant: str | None = Header(default=None, alias='X-Tenant')):
            return {'tenant': x_tenant}

        gateway = Gateway.from_fastapi(
            app,
            name='svc',
            passthrough_headers=('X-API-Key', 'X-Tenant'),
        )
        mcp = gateway._servers[0].mcp
        tool = next(iter(mcp._tool_manager.list_tools()))

        ctx = _stub_context({'x-tenant': 'acme-co'})
        result = await tool.run({}, context=ctx)
        assert result.structuredContent == {'tenant': 'acme-co'}

    async def test_mixed_security_emits_warning(self, caplog):
        """Mixed security schemes across marked routes log a single startup warning."""
        oauth = OAuth2AuthorizationCodeBearer(
            authorizationUrl='https://auth.example.com/authorize',
            tokenUrl='https://auth.example.com/token',
            auto_error=False,
        )
        api_key = APIKeyHeader(name='X-API-Key', auto_error=False)
        app = FastAPI()

        @app.get('/oauth')
        @mcp_tool()
        def oauth_route(_token: str | None = Depends(oauth)):
            return {'kind': 'oauth'}

        @app.get('/apikey')
        @mcp_tool()
        def apikey_route(_key: str | None = Depends(api_key)):
            return {'kind': 'apikey'}

        with caplog.at_level('WARNING', logger='openapi_mcp_gateway.gateway'):
            Gateway.from_fastapi(app, name='svc')

        warnings = [record for record in caplog.records if 'distinct security schemes' in record.message]
        assert len(warnings) == 1
