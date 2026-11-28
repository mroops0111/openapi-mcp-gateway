from unittest.mock import AsyncMock, patch

import pytest

from openapi_mcp_gateway.auth.mcp.token_issuer import MCP_DEFAULT_SCOPE
from tests.client_metadata import (
    CLIENT_METADATA_HOST,
    CLIENT_METADATA_URL,
    client_metadata_document,
    serving_resolver,
)
from tests.gateway import build_gateway
from tests.mcp_client import Browser, MemoryTokenStorage, authorization_code_auth, list_tool_names
from tests.oauth import upstream_token_response
from tests.serving import serve_gateway


class TestAuthorizationCode:
    """An MCP SDK client registers, sends the user through consent and the upstream, and lists the tools."""

    @pytest.mark.parametrize('path_prefix', ['shop', '/'])
    async def test_a_client_signs_a_user_in(self, shop_json_path, path_prefix):
        server = {
            'name': 'shop',
            'path_prefix': path_prefix,
            'spec': str(shop_json_path),
            'auth': {
                'upstream': {'type': 'oauth2', 'client_id': 'gateway', 'client_secret': 'secret', 'scopes': ['read']},
            },
        }
        mount = '' if path_prefix == '/' else f'/{path_prefix}'

        with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response())):
            async with serve_gateway(lambda url: build_gateway(url, [server])) as base_url:
                endpoint = f'{base_url}{mount}/mcp'
                auth = authorization_code_auth(endpoint, Browser(f'{base_url}{mount}'))
                assert await list_tool_names(endpoint, auth)


class TestClientIdMetadataDocument:
    """An MCP SDK client signs in with its metadata document's URL as its client_id, where the gateway accepts one.

    The document lists its loopback redirect without a port, and the client listens on one of its choosing.
    """

    @pytest.mark.parametrize(
        ('mcp', 'scope'),
        [
            ({}, MCP_DEFAULT_SCOPE),
            (
                {
                    'operation_scopes': {'auto': 'tags'},
                    'other_clients': {'trusted_hosts': [CLIENT_METADATA_HOST], 'scopes': ['products:read']},
                },
                'products:read',
            ),
            (
                {
                    'operation_scopes': {'auto': 'tags'},
                    'clients': {CLIENT_METADATA_URL: {'scopes': ['products:write']}},
                },
                'products:write',
            ),
        ],
        ids=['open by default', 'other clients', 'listed'],
    )
    async def test_a_client_signs_a_user_in_without_registering(self, shop_json_path, mcp, scope):
        server = {
            'name': 'shop',
            'spec': str(shop_json_path),
            'auth': {
                'upstream': {'type': 'oauth2', 'client_id': 'gateway', 'client_secret': 'secret', 'scopes': ['read']},
                'mcp': mcp,
            },
        }
        storage = MemoryTokenStorage()
        resolver = serving_resolver({CLIENT_METADATA_URL: client_metadata_document()})
        with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response())):
            async with serve_gateway(lambda url: build_gateway(url, [server], resolver)) as base_url:
                endpoint = f'{base_url}/shop/mcp'
                auth = authorization_code_auth(
                    endpoint, Browser(f'{base_url}/shop'), storage=storage, client_metadata_url=CLIENT_METADATA_URL
                )
                assert await list_tool_names(endpoint, auth)
        assert storage.client_info is not None
        assert storage.client_info.client_id == CLIENT_METADATA_URL
        assert storage.tokens is not None
        assert storage.tokens.scope == scope
