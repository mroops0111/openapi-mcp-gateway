import json

import httpx
import pytest
from mcp.types import TextContent

from openapi_mcp_gateway.openapi import (
    ParameterInfo,
)
from tests.context import stub_context
from tests.operation import register_tool, shaped_operation


class TestRequestTransform:
    """A ``request`` JSONata expression builds the whole upstream request from the friendly arguments."""

    async def test_injects_constant_into_query(self, mock_upstream):
        """A literal in the expression is sent upstream though the LLM never supplied it."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['params'] = dict(request.url.params)
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        op = shaped_operation([], request='{"include_adult": false, "language": "en-US"}')
        await register_tool(op).run({}, context=stub_context())
        assert captured['params']['include_adult'] == 'false'
        assert captured['params']['language'] == 'en-US'

    async def test_renames_friendly_argument(self, mock_upstream):
        """The LLM supplies a friendly name, the expression sends it under the upstream name."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['params'] = dict(request.url.params)
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        op = shaped_operation(
            [ParameterInfo(name='query', location='query', required=True, schema={'type': 'string'})],
            request='{"v[any_searchable][]": query}',
        )
        await register_tool(op).run({'query': 'hello'}, context=stub_context())
        assert captured['params']['v[any_searchable][]'] == 'hello'
        assert 'query' not in captured['params']

    async def test_maps_value_with_lookup(self, mock_upstream):
        """A ``$lookup`` translates a friendly enum into the raw API value."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['params'] = dict(request.url.params)
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        op = shaped_operation(
            [
                ParameterInfo(
                    name='status',
                    location='query',
                    required=True,
                    schema={'type': 'string', 'enum': ['open', 'closed', 'any']},
                )
            ],
            request='{"op[status_id]": $lookup({"open": "o", "closed": "c", "any": "*"}, status)}',
        )
        await register_tool(op).run({'status': 'open'}, context=stub_context())
        assert captured['params']['op[status_id]'] == 'o'

    async def test_routes_key_to_path(self, mock_upstream):
        """An output key that names a path placeholder fills the URL path."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['path'] = request.url.path
            return httpx.Response(200, json={})

        mock_upstream(handler)
        op = shaped_operation(
            [ParameterInfo(name='movie_id', location='path', required=True, schema={'type': 'integer'})],
            request='{"movie_id": movie_id, "language": "en-US"}',
            path='/movie/{movie_id}',
        )
        await register_tool(op).run({'movie_id': 42}, context=stub_context())
        assert captured['path'] == '/movie/42'

    async def test_routes_to_body_for_post(self, mock_upstream):
        """For a body method, non-path keys become the JSON body."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['body'] = json.loads(request.content)
            return httpx.Response(200, json={})

        mock_upstream(handler)
        op = shaped_operation(
            [ParameterInfo(name='title', location='body', required=True, schema={'type': 'string'})],
            request='{"name": title, "source": "gateway"}',
            method='post',
            path='/docs',
        )
        await register_tool(op).run({'title': 'hello'}, context=stub_context())
        assert captured['body'] == {'name': 'hello', 'source': 'gateway'}

    async def test_null_value_dropped(self, mock_upstream):
        """A key the expression sets to null leaves no trace upstream."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['params'] = dict(request.url.params)
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        op = shaped_operation([], request='{"kept": "yes", "dropped": null}')
        await register_tool(op).run({}, context=stub_context())
        assert captured['params']['kept'] == 'yes'
        assert 'dropped' not in captured['params']

    async def test_fan_out_list_becomes_repeated_query(self, mock_upstream):
        """A list value is sent as repeated same-key query params, as filter DSLs require."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['multi'] = request.url.params.multi_items()
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        op = shaped_operation(
            [ParameterInfo(name='query', location='query', required=True, schema={'type': 'string'})],
            request='{"f[]": ["any_searchable", "status_id"], "v[any_searchable][]": query}',
        )
        await register_tool(op).run({'query': 'bug'}, context=stub_context())
        assert ('f[]', 'any_searchable') in captured['multi']
        assert ('f[]', 'status_id') in captured['multi']
        assert ('v[any_searchable][]', 'bug') in captured['multi']

    async def test_omitted_argument_leaves_no_trace(self, mock_upstream):
        """An omitted optional argument is absent from the expression input, so its key is dropped."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['params'] = dict(request.url.params)
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        op = shaped_operation(
            [ParameterInfo(name='page', location='query', required=False, schema={'type': 'integer'})],
            request='{"page": page, "language": "en-US"}',
        )
        await register_tool(op).run({}, context=stub_context())
        assert 'page' not in captured['params']
        assert captured['params']['language'] == 'en-US'

    async def test_default_prefilled_before_transform(self, mock_upstream):
        """An x-mcp default fills the argument before the expression runs, so the expression sees it."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured['params'] = dict(request.url.params)
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        op = shaped_operation(
            [
                ParameterInfo(
                    name='sort',
                    location='query',
                    required=False,
                    schema={'type': 'string', 'enum': ['popular', 'newest']},
                )
            ],
            params={'sort': {'default': 'popular'}},
            request='{"sort_by": $lookup({"popular": "popularity.desc", "newest": "primary_release_date.desc"}, sort)}',
        )
        await register_tool(op).run({}, context=stub_context())
        assert captured['params']['sort_by'] == 'popularity.desc'

    async def test_evaluation_error_returns_is_error(self, mock_upstream):
        """A failing request expression returns an ``isError`` result and never calls upstream."""
        called: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            called['hit'] = True
            return httpx.Response(200, json=[])

        mock_upstream(handler)
        op = shaped_operation(
            [ParameterInfo(name='name', location='query', required=True, schema={'type': 'string'})],
            request='{"n": name + 1}',
        )
        result = await register_tool(op).run({'name': 'x'}, context=stub_context())
        assert result.is_error is True
        message = result.content[0]
        assert isinstance(message, TextContent)
        assert 'request transform failed' in message.text
        assert 'hit' not in called

    def test_invalid_expression_fails_at_registration(self):
        """A syntactically broken expression is rejected when the tool is built, not on first call."""
        op = shaped_operation([], request='{ broken')
        with pytest.raises(ValueError, match='Invalid JSONata'):
            register_tool(op)


class TestResponseTransform:
    """A ``response`` JSONata expression reshapes a successful upstream body before it reaches the client."""

    async def test_reshapes_object(self, mock_upstream):
        """The expression rebuilds the object, renaming and lifting nested fields."""
        mock_upstream(
            lambda _request: httpx.Response(200, json={'data': {'items': [1, 2]}, 'pagination': {'total': 100}})
        )
        op = shaped_operation([], response='{"items": data.items, "count": pagination.total}')
        result = await register_tool(op).run({}, context=stub_context())
        assert result.structured_content == {'items': [1, 2], 'count': 100}

    async def test_unwrap_via_path(self, mock_upstream):
        """A bare path expression unwraps an envelope down to the inner object."""
        mock_upstream(lambda _request: httpx.Response(200, json={'issue': {'id': 1, 'subject': 's'}}))
        op = shaped_operation([], response='issue')
        result = await register_tool(op).run({}, context=stub_context())
        assert result.structured_content == {'id': 1, 'subject': 's'}

    async def test_array_projection(self, mock_upstream):
        """Mapping over an array keeps only the chosen fields of each item."""
        mock_upstream(
            lambda _request: httpx.Response(
                200,
                json={'results': [{'title': 'A', 'vote_average': 7, 'x': 1}, {'title': 'B', 'vote_average': 8}]},
            )
        )
        op = shaped_operation([], response='results.{"title": title, "rating": vote_average}')
        result = await register_tool(op).run({}, context=stub_context())
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert json.loads(content.text) == [{'title': 'A', 'rating': 7}, {'title': 'B', 'rating': 8}]

    async def test_evaluation_error_returns_is_error(self, mock_upstream):
        """A failing response expression returns an ``isError`` result."""
        mock_upstream(lambda _request: httpx.Response(200, json={'total': 'not-a-number'}))
        op = shaped_operation([], response='{"n": total + 1}')
        result = await register_tool(op).run({}, context=stub_context())
        assert result.is_error is True
        message = result.content[0]
        assert isinstance(message, TextContent)
        assert 'response transform failed' in message.text

    def test_invalid_expression_fails_at_registration(self):
        """A syntactically broken response expression is rejected at build time."""
        op = shaped_operation([], response='results.{')
        with pytest.raises(ValueError, match='Invalid JSONata'):
            register_tool(op)
