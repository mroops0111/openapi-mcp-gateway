"""Tests for the APIClient response handling."""

import httpx
import pytest

from openapi_mcp_gateway.client import APIClient


def _client_with_handler(handler):
    client = APIClient(base_url='https://example.com')
    client._client = httpx.AsyncClient(
        base_url='https://example.com',
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_json_response_parsed():
    def handler(_request):
        return httpx.Response(200, json={'id': 1, 'name': 'fido'})

    async with _client_with_handler(handler) as client:
        result = await client.request('GET', '/pet/1')

    assert result == {'id': 1, 'name': 'fido'}


@pytest.mark.asyncio
async def test_204_no_content():
    def handler(_request):
        return httpx.Response(204)

    async with _client_with_handler(handler) as client:
        result = await client.request('DELETE', '/pet/1')

    assert result == {'status': 204}


@pytest.mark.asyncio
async def test_empty_body_with_200():
    def handler(_request):
        return httpx.Response(200, content=b'', headers={'content-type': 'application/json'})

    async with _client_with_handler(handler) as client:
        result = await client.request('POST', '/action')

    assert result == {'status': 200}


@pytest.mark.asyncio
async def test_non_json_body_wrapped():
    def handler(_request):
        return httpx.Response(200, text='hello', headers={'content-type': 'text/plain'})

    async with _client_with_handler(handler) as client:
        result = await client.request('GET', '/text')

    assert result == {'data': 'hello'}


@pytest.mark.asyncio
async def test_invalid_json_with_json_content_type_falls_back_to_text():
    """When content-type is application/json but body is not valid JSON, wrap as text."""

    def handler(_request):
        return httpx.Response(200, content=b'Pet deleted', headers={'content-type': 'application/json'})

    async with _client_with_handler(handler) as client:
        result = await client.request('DELETE', '/pet/1')

    assert result == {'data': 'Pet deleted'}
