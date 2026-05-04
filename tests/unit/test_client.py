import httpx
import pytest

from openapi_mcp_gateway.client import APIClient


def _client_with_handler(handler):
    """Build an ``APIClient`` whose transport is a ``MockTransport`` over ``handler``."""
    client = APIClient(base_url='https://example.com')
    client._client = httpx.AsyncClient(
        base_url='https://example.com',
        transport=httpx.MockTransport(handler),
    )
    return client


async def test_json_response_parsed():
    """``application/json`` responses are returned as the decoded dict."""

    def handler(_request):
        return httpx.Response(200, json={'id': 1, 'name': 'fido'})

    async with _client_with_handler(handler) as client:
        result = await client.request('GET', '/pet/1')

    assert result == {'id': 1, 'name': 'fido'}


async def test_204_no_content():
    """``204 No Content`` returns a synthetic ``{'status': 204}`` instead of parsing."""

    def handler(_request):
        return httpx.Response(204)

    async with _client_with_handler(handler) as client:
        result = await client.request('DELETE', '/pet/1')

    assert result == {'status': 204}


async def test_non_json_body_wrapped():
    """Non-JSON content types are wrapped as ``{'data': text}``."""

    def handler(_request):
        return httpx.Response(200, text='hello', headers={'content-type': 'text/plain'})

    async with _client_with_handler(handler) as client:
        result = await client.request('GET', '/text')

    assert result == {'data': 'hello'}


async def test_invalid_json_with_json_content_type_falls_back_to_text():
    """Bodies that claim ``application/json`` but fail to parse fall back to ``{'data': text}``."""

    def handler(_request):
        return httpx.Response(200, content=b'Pet deleted', headers={'content-type': 'application/json'})

    async with _client_with_handler(handler) as client:
        result = await client.request('DELETE', '/pet/1')

    assert result == {'data': 'Pet deleted'}


@pytest.mark.parametrize('status_code', [400, 401, 404, 500, 503])
async def test_http_error_raises_status_error(status_code):
    """4xx and 5xx responses raise ``httpx.HTTPStatusError`` carrying the response."""

    def handler(_request):
        return httpx.Response(status_code, json={'error': 'boom'})

    async with _client_with_handler(handler) as client:
        with pytest.raises(httpx.HTTPStatusError) as exception_info:
            await client.request('GET', '/pet/missing')

    assert exception_info.value.response.status_code == status_code
