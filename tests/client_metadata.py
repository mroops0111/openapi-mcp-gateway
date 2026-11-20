import collections.abc
import contextlib
import typing
import urllib.parse
from unittest.mock import patch

import httpx

from openapi_mcp_gateway.auth.mcp.client_metadata import HostResolver, McpClientMetadataResolver
from tests.constants import BROWSER_ORIGIN


# Where the client in these tests publishes its metadata document, which is also its client_id, and that URL's host.
CLIENT_METADATA_URL = f'{BROWSER_ORIGIN}/oauth/client.json'
CLIENT_METADATA_HOST = urllib.parse.urlsplit(BROWSER_ORIGIN).hostname or ''

# A public address every host resolves to in these tests, so the SSRF guard lets the fetch through.
PUBLIC_ADDRESS = '93.184.216.34'


def client_metadata_document(
    client_id: str = CLIENT_METADATA_URL, redirect_uris: collections.abc.Sequence[str] = ('http://127.0.0.1/callback',)
) -> dict[str, typing.Any]:
    """A metadata document for a public client named ``Example Agent``, as Claude Code publishes one.

    Its loopback redirect names no port, so a sign-in from any port matches it.
    """
    return {
        'client_id': client_id,
        'client_name': 'Example Agent',
        'redirect_uris': list(redirect_uris),
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
        'token_endpoint_auth_method': 'none',
    }


def resolve_to(*addresses: str) -> HostResolver:
    """A name resolution that answers ``addresses`` for every host."""

    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


def resolver_over(transport: httpx.AsyncBaseTransport) -> McpClientMetadataResolver:
    """A resolver reaching ``transport`` for any host, as if every host resolved to ``PUBLIC_ADDRESS``."""
    return McpClientMetadataResolver(transport, resolve_host=resolve_to(PUBLIC_ADDRESS))


def requested_url(request: httpx.Request) -> str:
    """The URL a request was for, under the host its ``Host`` header names rather than the address it went to."""
    return f'https://{request.headers["host"]}{request.url.raw_path.decode()}'


def serving_resolver(documents: collections.abc.Mapping[str, dict[str, typing.Any]]) -> McpClientMetadataResolver:
    """A resolver whose network serves each of ``documents`` at its URL, and 404 anywhere else."""

    def serve(request: httpx.Request) -> httpx.Response:
        document = documents.get(requested_url(request))
        return httpx.Response(200, json=document) if document is not None else httpx.Response(404)

    return resolver_over(httpx.MockTransport(serve))


@contextlib.contextmanager
def serving_client_metadata(documents: collections.abc.Mapping[str, dict[str, typing.Any]]) -> typing.Iterator[None]:
    """Gateways built inside fetch client metadata documents from ``documents`` rather than the network."""
    with patch(
        'openapi_mcp_gateway.auth.flows.authorization_code.McpClientMetadataResolver',
        lambda: serving_resolver(documents),
    ):
        yield
