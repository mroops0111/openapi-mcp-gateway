import json
import os
import pathlib
import re

import httpx
import pytest
from mcp import Client
from mcp.types import TextContent

from openapi_mcp_gateway import Gateway, GatewayConfig
from tests.gateway import server_mcp


pytestmark = pytest.mark.smoke


_EXAMPLES_DIR = pathlib.Path(__file__).resolve().parents[2] / 'examples'

# Every shipped example, so one added to examples/ is covered without a list to keep in step.
_EXAMPLES = sorted(path.name for path in _EXAMPLES_DIR.glob('*.yml'))

# The ${VAR} references an example reads its credentials from.
_ENV_REFERENCE = re.compile(r'\$\{(\w+)(?::-[^}]*)?\}')


@pytest.fixture
def example_gateway(monkeypatch):
    """Build the gateway an example describes, with bogus values for every variable it references.

    No example is ever called against its real API, so any value lets ``${VAR}`` resolve.
    """

    def build(config_name: str) -> Gateway:
        path = _EXAMPLES_DIR / config_name
        for name in _ENV_REFERENCE.findall(path.read_text()):
            if name not in os.environ:
                monkeypatch.setenv(name, f'smoke-test-{name.lower()}')
        return Gateway.from_config(GatewayConfig.from_yaml(path))

    return build


@pytest.mark.parametrize('config_name', _EXAMPLES)
async def test_example_serves_something(config_name: str, example_gateway):
    """Each example downloads its specs, builds a gateway, and serves a tool or a resource on every server.

    The specs are fetched over HTTP, so this needs the network, and no upstream API is called.
    """
    gateway = example_gateway(config_name)

    assert gateway.describe_servers(), f'config "{config_name}" registered no servers'
    for bundle in gateway.describe_servers():
        async with Client(bundle.mcp) as session:
            served = [
                *(await session.list_tools()).tools,
                *(await session.list_resources()).resources,
                *(await session.list_resource_templates()).resource_templates,
            ]
            assert served, f'config "{config_name}" server "{bundle.name}" serves nothing'


async def test_github_dynamic_search_finds_merge_on_first_page(example_gateway):
    """Against GitHub's 1,200-operation spec, the first result stays small and a plain query finds its operation.

    The first ``search_operations`` call returns one page of operations and the tag counts,
    so it fits in a few kilobytes.
    """
    gateway = example_gateway('github-dynamic.yml')

    async with Client(server_mcp(gateway)) as session:
        overview = await session.call_tool('search_operations', {})
        text = overview.content[0]
        assert isinstance(text, TextContent)
        assert len(text.text) < 8 * 1024
        found = await session.call_tool('search_operations', {'query': 'merge a pull request'})
        assert 'pulls_merge' in {entry['name'] for entry in found.structured_content['operations']}


class TestGithubShapingExample:
    """The examples/github-shaping.yml tools reach GitHub as documented, against a stand-in upstream."""

    async def test_find_issues_builds_the_search_query_and_trims_the_results(self, example_gateway, mock_upstream):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            issue = {
                'number': 7,
                'title': 'Crash on start',
                'state': 'open',
                'user': {'login': 'octocat'},
                'labels': [{'name': 'bug'}],
                'html_url': 'https://github.com/octo/repo/issues/7',
                'body': 'Stack trace ...',
                'reactions': {'total_count': 3},
            }
            return httpx.Response(200, json={'total_count': 1, 'items': [issue]})

        mock_upstream(handler)
        gateway = example_gateway('github-shaping.yml')

        async with Client(server_mcp(gateway)) as client:
            tool = next(tool for tool in (await client.list_tools()).tools if tool.name == 'find_issues')
            assert set(tool.input_schema['properties']) == {'repository', 'state', 'label', 'limit'}
            result = await client.call_tool('find_issues', {'repository': 'octo/repo', 'label': 'bug'})

        params = requests[0].url.params
        assert requests[0].url.path == '/search/issues'
        assert params['q'] == 'repo:octo/repo is:issue is:open label:"bug"'
        assert params['sort'] == 'updated'
        assert params['per_page'] == '10'
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert json.loads(content.text) == [
            {
                'number': 7,
                'title': 'Crash on start',
                'state': 'open',
                'author': 'octocat',
                'labels': ['bug'],
                'url': 'https://github.com/octo/repo/issues/7',
            }
        ]

    async def test_read_issue_splits_the_repository_into_the_path(self, example_gateway, mock_upstream):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            issue = {
                'number': 7,
                'title': 'Crash on start',
                'state': 'open',
                'user': {'login': 'octocat'},
                'body': 'Stack trace ...',
                'comments': 2,
                'node_id': 'I_kw',
            }
            return httpx.Response(200, json=issue)

        mock_upstream(handler)
        gateway = example_gateway('github-shaping.yml')

        async with Client(server_mcp(gateway)) as client:
            result = await client.call_tool('read_issue', {'repository': 'octo/repo', 'number': 7})

        assert requests[0].url.path == '/repos/octo/repo/issues/7'
        assert requests[0].headers['authorization'] == 'Bearer smoke-test-github_token'
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert json.loads(content.text) == {
            'number': 7,
            'title': 'Crash on start',
            'state': 'open',
            'author': 'octocat',
            'body': 'Stack trace ...',
            'comments': 2,
        }
