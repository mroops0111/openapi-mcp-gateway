import typing
from unittest.mock import AsyncMock, MagicMock

import httpx2
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import APIKeyHeader, HTTPBasic, HTTPBasicCredentials, OAuth2AuthorizationCodeBearer
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from openapi_mcp_gateway import Gateway, mcp_tool
from openapi_mcp_gateway.auth.upstream import client_credentials as client_credentials_module
from openapi_mcp_gateway.settings import AuthConfig, UpstreamAuthConfig
from tests.constants import AUTHORIZE_URL, TOKEN_URL
from tests.serving import serve


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
        result = await _call(gateway, {'user_id': '42'}, tool='lookup_user')
        assert result.is_error is False
        assert result.structured_content == {'user_id': '42', 'echo': 'ok'}

    async def test_passthrough_forwards_authorization_header(self):
        """Passthrough mode delivers the MCP client's Authorization header to the FastAPI route."""
        oauth = OAuth2AuthorizationCodeBearer(
            authorizationUrl=AUTHORIZE_URL,
            tokenUrl=TOKEN_URL,
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

        result = await _call(gateway, {}, headers={'authorization': 'Bearer client-token'})
        assert result.structured_content == {'authorization': 'Bearer client-token'}

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

        result = await _call(gateway, {}, headers={'authorization': 'Basic Zm9vOmJhcg=='})
        assert result.structured_content == {'username': 'foo'}

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

        result = await _call(gateway, {}, headers={'x-api-key': 'top-secret'})
        assert result.structured_content == {'api_key': 'top-secret'}

    async def test_resolver_authorization_wins_over_passthrough(self, monkeypatch):
        """In client_credentials mode the gateway-minted Authorization is not overwritten by the client's."""
        token_response = MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {'access_token': 'gateway-token', 'expires_in': 3600}
        token_response.text = ''
        monkeypatch.setattr(
            client_credentials_module.httpx.AsyncClient,
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
                            'tokenUrl': TOKEN_URL,
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
            auth=AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec')),
        )

        # MCP client tries to send its own Authorization, but gateway must use gateway-token instead.
        result = await _call(gateway, {}, headers={'authorization': 'Bearer client-token'})
        assert result.structured_content == {'authorization': 'Bearer gateway-token'}

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

        result = await _call(gateway, {}, headers={'x-tenant': 'acme-co'})
        assert result.structured_content == {'tenant': 'acme-co'}


async def _call(
    gateway: Gateway, arguments: dict[str, typing.Any], tool: str | None = None, headers: dict[str, str] | None = None
) -> CallToolResult:
    """Call ``tool``, or the one tool ``gateway`` serves, from an MCP client over HTTP that sends ``headers``."""
    async with (
        serve(gateway.build_app()) as base_url,
        httpx2.AsyncClient(headers=headers) as http_client,
        Client(streamable_http_client(f'{base_url}/svc/mcp', http_client=http_client)) as client,
    ):
        name = tool or (await client.list_tools()).tools[0].name
        return await client.call_tool(name, arguments)
