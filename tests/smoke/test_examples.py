import pathlib

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from openapi_mcp_gateway import Gateway, GatewayConfig


pytestmark = pytest.mark.smoke


_EXAMPLES_DIR = pathlib.Path(__file__).resolve().parents[2] / 'examples'


@pytest.fixture(autouse=True)
def _smoke_credentials(monkeypatch):
    """Provide bogus credentials so OAuth/Bearer specs build without real env vars.

    The smoke only lists tools; it never calls upstream APIs. Bogus values
    are sufficient for ``${ENV_VAR}`` substitution and OAuth flow assembly.
    """
    monkeypatch.setenv('ASANA_CLIENT_ID', 'smoke-test-client-id')
    monkeypatch.setenv('ASANA_CLIENT_SECRET', 'smoke-test-client-secret')
    monkeypatch.setenv('GITHUB_TOKEN', 'smoke-test-github-token')


@pytest.mark.parametrize(
    'config_name',
    [
        'petstore.yml',
        'github.yml',
        'asana.yml',
        'multi-server.yml',
    ],
)
async def test_example_config_lists_tools(config_name: str):
    """Each example YAML loads, builds a gateway, and exposes at least one tool per server.

    The OpenAPI specs are fetched over HTTP (network required) and tool
    invocation is intentionally not exercised — that would require real
    upstream credentials and would defeat the point of a quick assembly
    smoke. We only verify the MCP client can ``list_tools`` against every
    server registered from the config.
    """
    config = GatewayConfig.from_yaml(_EXAMPLES_DIR / config_name)
    gateway = Gateway.from_config(config)

    assert gateway._servers, f'config "{config_name}" registered no servers'

    for bundle in gateway._servers:
        async with create_connected_server_and_client_session(bundle.mcp) as session:
            listed = await session.list_tools()
            assert listed.tools, f'config "{config_name}" server "{bundle.name}" registered no tools'
