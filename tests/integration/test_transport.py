import contextlib
import pathlib
import sys
import typing

import anyio
import pytest
from fastapi import FastAPI
from mcp import Client, StdioServerParameters
from mcp.client.sse import sse_client
from starlette.testclient import TestClient

from openapi_mcp_gateway.app import _transport_security
from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import (
    DNSRebindingProtectionConfig,
    GatewayConfig,
    ServerConfig,
)
from tests.constants import (
    ATTACKER_HOST,
    ATTACKER_URL,
    BROWSER_ORIGIN,
    GATEWAY_HOST,
)
from tests.serving import serve


class TestTransports:
    """Every transport the CLI accepts starts and answers MCP, rather than starting far enough to log and then dying."""

    @pytest.mark.parametrize('transport', ['streamable-http', 'sse', 'stdio'])
    async def test_answers_initialize_and_lists_tools(self, gateway, petstore_json_path, transport):
        """A client of each transport completes ``initialize`` and lists the petstore tools."""
        with anyio.fail_after(10):
            async with _connect(gateway, petstore_json_path, transport) as mcp_client:
                server_info = mcp_client.server_info
                tools = await mcp_client.list_tools()

        assert server_info is not None
        assert server_info.name.startswith('petstore')
        assert tools.tools

    def test_stdio_refuses_a_server_that_checks_mcp_tokens(self, petstore_json_path):
        """A stdio client presents no token, so such a server would refuse or hide everything without a word."""
        server = {
            'name': 'petstore',
            'spec': str(petstore_json_path),
            'auth': {
                'upstream': {'type': 'bearer', 'token': 't'},
                'mcp': {'clients': {'agent': {'client_secret': 's'}}},
            },
        }
        gateway = Gateway.from_config(GatewayConfig.model_validate({'servers': [server]}))

        with pytest.raises(ValueError, match='never presents'):
            gateway.run(transport='stdio')

    def test_sse_transport_emits_deprecation_warning(self, gateway):
        """Selecting the deprecated ``sse`` transport warns the caller."""
        with pytest.warns(DeprecationWarning, match='sse'):
            gateway.mount(FastAPI(), transport='sse')

    def test_streamable_http_transport_does_not_warn(self, gateway, recwarn):
        """The recommended ``streamable-http`` transport mounts without an SSE deprecation warning."""
        gateway.mount(FastAPI(), transport='streamable-http')
        sse_warnings = [
            warning
            for warning in recwarn
            if issubclass(warning.category, DeprecationWarning) and 'sse' in str(warning.message)
        ]
        assert not sse_warnings


class TestDNSRebindingProtection:
    """``Host`` / ``Origin`` checks on the MCP endpoints, which is how a DNS rebinding attack is refused."""

    @pytest.fixture
    def listed_gateway(self, petstore_json_path):
        """Petstore gateway that lists the one host and origin it is reached by."""
        config = GatewayConfig(
            servers=[ServerConfig(name='petstore', spec=str(petstore_json_path))],
            dns_rebinding_protection=DNSRebindingProtectionConfig(
                allowed_hosts=[GATEWAY_HOST], allowed_origins=[BROWSER_ORIGIN]
            ),
        )
        return Gateway.from_config(config)

    def test_loopback_bind_refuses_a_foreign_host(self, gateway):
        """A page re-pointing its own domain at 127.0.0.1 still sends that domain as ``Host``, so it is refused."""
        with TestClient(gateway._build_app(transport='streamable-http', host='127.0.0.1')) as test_client:
            assert _initialize_status(test_client, ATTACKER_HOST) == 421
            assert _initialize_status(test_client, 'localhost:8000') == 200

    def test_any_other_bind_accepts_any_host(self, gateway):
        """With nothing listed, a bind behind an unknown proxy keeps accepting whatever ``Host`` arrives."""
        with TestClient(gateway._build_app(transport='streamable-http', host='0.0.0.0')) as test_client:
            assert _initialize_status(test_client, ATTACKER_HOST) == 200

    def test_listed_hosts_and_origins_are_enforced_on_any_bind(self, listed_gateway):
        """Listing hosts turns the check on even for a ``0.0.0.0`` bind, and accepts only what is listed."""
        with TestClient(listed_gateway._build_app(transport='streamable-http', host='0.0.0.0')) as test_client:
            assert _initialize_status(test_client, GATEWAY_HOST) == 200
            assert _initialize_status(test_client, ATTACKER_HOST) == 421
            assert _initialize_status(test_client, GATEWAY_HOST, origin=BROWSER_ORIGIN) == 200
            assert _initialize_status(test_client, GATEWAY_HOST, origin=ATTACKER_URL) == 403

    def test_mounted_gateway_checks_only_when_hosts_are_listed(self):
        """A mounted gateway has no bind of its own to judge by, so only a listed host turns the check on."""
        unlisted = _transport_security(DNSRebindingProtectionConfig(), host=None)
        assert unlisted is not None
        assert not unlisted.enable_dns_rebinding_protection
        listed = _transport_security(DNSRebindingProtectionConfig(allowed_hosts=[GATEWAY_HOST]), host=None)
        assert listed is not None
        assert listed.enable_dns_rebinding_protection
        assert listed.allowed_hosts == [GATEWAY_HOST]

    def test_origins_without_hosts_are_refused(self):
        """Origins alone would turn on a check that refuses every ``Host``, so the config is refused instead."""
        with pytest.raises(ValueError, match='allowed_hosts'):
            DNSRebindingProtectionConfig(allowed_origins=[BROWSER_ORIGIN])


def _initialize_status(test_client: TestClient, host: str, origin: str | None = None) -> int:
    """Status of an MCP ``initialize`` POST to the petstore endpoint, sent with ``host`` and ``origin``."""
    headers = {'Host': host, 'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
    if origin is not None:
        headers['Origin'] = origin
    body = {
        'jsonrpc': '2.0',
        'id': 1,
        'method': 'initialize',
        'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}},
    }
    return test_client.post('/petstore/mcp', headers=headers, json=body).status_code


@contextlib.asynccontextmanager
async def _connect(gateway: Gateway, spec_path: pathlib.Path, transport: str) -> typing.AsyncIterator[Client]:
    """Connect an MCP client over ``transport``, through the CLI for stdio and ``gateway``'s app for HTTP."""
    if transport == 'stdio':
        cli_args = ['--spec', str(spec_path), '--name', 'petstore', '--transport', 'stdio']
        async with Client(
            StdioServerParameters(command=sys.executable, args=['-m', 'openapi_mcp_gateway.cli', *cli_args])
        ) as mcp_client:
            yield mcp_client
        return
    async with serve(gateway._build_app(transport=transport)) as base_url:
        target = (
            f'{base_url}/petstore/mcp' if transport == 'streamable-http' else sse_client(f'{base_url}/petstore/sse')
        )
        async with Client(target) as mcp_client:
            yield mcp_client
