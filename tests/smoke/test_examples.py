import pathlib

import pytest
from mcp import Client

from openapi_mcp_gateway import Gateway, GatewayConfig


pytestmark = pytest.mark.smoke


_EXAMPLES_DIR = pathlib.Path(__file__).resolve().parents[2] / 'examples'


@pytest.fixture(autouse=True)
def _smoke_credentials(monkeypatch):
    """Provide bogus credentials so OAuth/Bearer specs build without real env vars.

    The smoke only lists tools and never calls upstream APIs.
    Bogus values are sufficient for ``${ENV_VAR}`` substitution and OAuth flow assembly.
    """
    monkeypatch.setenv('ASANA_CLIENT_ID', 'smoke-test-client-id')
    monkeypatch.setenv('ASANA_CLIENT_SECRET', 'smoke-test-client-secret')
    monkeypatch.setenv('GITHUB_TOKEN', 'smoke-test-github-token')
    monkeypatch.setenv('AUTH0_CLIENT_ID', 'smoke-test-auth0-client-id')
    monkeypatch.setenv('AUTH0_CLIENT_SECRET', 'smoke-test-auth0-client-secret')


@pytest.mark.parametrize(
    'config_name',
    [
        'petstore.yml',
        'github.yml',
        'asana.yml',
        'multi-server.yml',
        'auth0-management.yml',
    ],
)
async def test_example_config_lists_tools(config_name: str):
    """Each example YAML loads, builds a gateway, and exposes at least one tool per server.

    The OpenAPI specs are fetched over HTTP (network required), and tool invocation is intentionally not exercised,
    since that would require real upstream credentials and defeat the point of a quick assembly smoke.
    The test only verifies the MCP client can ``list_tools`` against every server registered from the config.
    """
    config = GatewayConfig.from_yaml(_EXAMPLES_DIR / config_name)
    gateway = Gateway.from_config(config)

    assert gateway._servers, f'config "{config_name}" registered no servers'

    for bundle in gateway._servers:
        async with Client(bundle.mcp) as session:
            listed = await session.list_tools()
            assert listed.tools, f'config "{config_name}" server "{bundle.name}" registered no tools'


async def test_github_dynamic_search_finds_merge_on_first_page():
    """Against GitHub's 1,200-operation spec, the first result stays small and a plain query finds its operation.

    The first ``search_operations`` call returns one page of operations and the tag counts, so it fits in a few kilobytes.
    """
    config = GatewayConfig.from_yaml(_EXAMPLES_DIR / 'github-dynamic.yml')
    gateway = Gateway.from_config(config)

    async with Client(gateway._servers[0].mcp) as session:
        overview = await session.call_tool('search_operations', {})
        assert len(overview.content[0].text) < 8 * 1024
        found = await session.call_tool('search_operations', {'query': 'merge a pull request'})
        assert 'pulls_merge' in {entry['name'] for entry in found.structured_content['operations']}
