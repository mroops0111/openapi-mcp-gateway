import asyncio
import json
import typing

import httpx
import pytest
from pydantic import AnyUrl

from openapi_mcp_gateway.auth.mcp import client_metadata
from openapi_mcp_gateway.auth.mcp.client_metadata import (
    MAX_CACHE_SECONDS,
    MAX_DOCUMENT_BYTES,
    MIN_CACHE_SECONDS,
    ClientMetadataError,
    McpClientMetadataResolver,
    is_client_metadata_url,
    is_loopback_redirect,
    parse_client_metadata_document,
)
from tests.client_metadata import CLIENT_METADATA_URL, PUBLIC_ADDRESS, client_metadata_document, resolve_to


class TestClientMetadataUrl:
    """Only a URL the CIMD draft §3 allows is read as a metadata document's address."""

    @pytest.mark.parametrize(
        'client_id',
        [CLIENT_METADATA_URL, 'https://app.example.com:8443/client.json', 'https://app.example.com/c?v=1'],
    )
    def test_an_https_url_with_a_path_is_one(self, client_id):
        assert is_client_metadata_url(client_id)

    @pytest.mark.parametrize(
        'client_id',
        [
            'agent',
            'http://app.example.com/client.json',
            'https://app.example.com',
            'https://app.example.com/',
            'https://app.example.com/client.json#frag',
            'https://user@app.example.com/client.json',
            'https://app.example.com/a/../client.json',
            'https://app.example.com/./client.json',
            'https://app.example.com:bad/client.json',
        ],
    )
    def test_anything_else_is_not(self, client_id):
        assert not is_client_metadata_url(client_id)


class TestDocumentParsing:
    """A document is accepted only when it names itself and describes a public client the gateway can serve."""

    def test_a_valid_document_gives_its_name_and_redirects(self):
        document = parse_client_metadata_document(CLIENT_METADATA_URL, _body(client_metadata_document()))
        assert document.client_name == 'Example Agent'
        assert [str(uri) for uri in document.redirect_uris] == ['http://127.0.0.1/callback']

    @pytest.mark.parametrize(('body', 'reason'), [(b'{not json', 'is not JSON'), (b'["a"]', 'is not a JSON object')])
    def test_a_body_that_is_not_a_json_object_is_refused(self, body, reason):
        with pytest.raises(ClientMetadataError, match=reason):
            parse_client_metadata_document(CLIENT_METADATA_URL, body)

    @pytest.mark.parametrize(
        ('changes', 'reason'),
        [
            ({'client_id': 'https://evil.example.com/client.json'}, 'rather than its own URL'),
            ({'client_secret': 'shh'}, 'carries a client_secret'),
            ({'client_secret_expires_at': 0}, 'carries a client_secret'),
            ({'token_endpoint_auth_method': 'private_key_jwt'}, "authenticates with 'private_key_jwt'"),
            ({'token_endpoint_auth_method': 'client_secret_basic'}, 'only public clients'),
            ({'grant_types': ['client_credentials']}, 'authorization_code grant'),
            ({'response_types': ['token']}, 'code response type'),
            ({'client_name': None}, 'invalid client_name'),
            ({'redirect_uris': []}, 'invalid redirect_uris'),
            ({'redirect_uris': ['http://app.example.com/callback']}, 'neither https nor loopback'),
        ],
    )
    def test_a_document_the_gateway_cannot_serve_is_refused(self, changes, reason):
        document = {key: value for key, value in {**client_metadata_document(), **changes}.items() if value is not None}
        with pytest.raises(ClientMetadataError, match=reason):
            parse_client_metadata_document(CLIENT_METADATA_URL, _body(document))

    def test_a_document_naming_no_method_or_grants_is_a_public_client(self):
        """RFC 7591 defaults would make it confidential, but the CIMD draft forbids the secret that would need."""
        document = client_metadata_document()
        for field in ('token_endpoint_auth_method', 'grant_types', 'response_types'):
            document.pop(field, None)
        assert parse_client_metadata_document(CLIENT_METADATA_URL, _body(document)).client_id == CLIENT_METADATA_URL


class TestLoopbackRedirect:
    @pytest.mark.parametrize(
        ('uri', 'loopback'),
        [
            ('http://localhost/callback', True),
            ('http://127.0.0.1:3000/', True),
            ('http://[::1]:9000/cb', True),
            ('https://localhost/callback', False),
            ('http://app.example.com/callback', False),
        ],
    )
    def test_only_http_to_this_machine_is_loopback(self, uri, loopback):
        assert is_loopback_redirect(AnyUrl(uri)) is loopback


class TestFetching:
    """A document is fetched from a public address the connection is pinned to, and nothing else."""

    async def test_the_connection_goes_to_the_checked_address_under_the_host_name(self):
        seen: list[httpx.Request] = []
        resolver = _resolver(_serve(client_metadata_document(), seen=seen))
        assert (await resolver.resolve(CLIENT_METADATA_URL)).client_name == 'Example Agent'
        [request] = seen
        assert request.url.host == PUBLIC_ADDRESS
        assert request.headers['host'] == 'app.example.com'
        assert request.extensions['sni_hostname'] == 'app.example.com'

    @pytest.mark.parametrize(
        'addresses',
        [['10.0.0.5'], ['127.0.0.1'], ['169.254.169.254'], ['::1'], ['fe80::1%en0'], [PUBLIC_ADDRESS, '10.0.0.5'], []],
    )
    async def test_a_host_resolving_to_any_private_or_local_address_is_refused(self, addresses):
        """One public address is not enough, since a later resolution may pick another of them."""
        resolver = McpClientMetadataResolver(_serve(client_metadata_document()), resolve_host=resolve_to(*addresses))
        with pytest.raises(ClientMetadataError, match=r'is at app\.example\.com'):
            await resolver.resolve(CLIENT_METADATA_URL)

    async def test_a_client_id_that_is_no_metadata_url_is_never_fetched(self):
        with pytest.raises(ClientMetadataError, match='is not at an https URL with a path'):
            await _resolver(_serve(client_metadata_document())).resolve('http://app.example.com/client.json')

    async def test_the_system_resolver_finds_localhost_local(self):
        """Name resolution itself is checked, beyond the stand-in the other tests use."""
        resolver = McpClientMetadataResolver(
            _serve({**client_metadata_document(), 'client_id': 'https://localhost/c.json'})
        )
        with pytest.raises(ClientMetadataError, match='a private or local address'):
            await resolver.resolve('https://localhost/c.json')

    async def test_a_private_ip_literal_is_refused_without_resolving(self):
        client_id = 'https://10.0.0.5/client.json'
        resolver = McpClientMetadataResolver(_serve({**client_metadata_document(), 'client_id': client_id}))
        with pytest.raises(ClientMetadataError, match='a private or local address'):
            await resolver.resolve(client_id)

    async def test_a_host_that_does_not_resolve_is_refused(self):
        async def fail(host: str, port: int) -> list[str]:
            raise OSError('no such host')

        resolver = McpClientMetadataResolver(_serve(client_metadata_document()), resolve_host=fail)
        with pytest.raises(ClientMetadataError, match='does not resolve'):
            await resolver.resolve(CLIENT_METADATA_URL)

    @pytest.mark.parametrize('status', [301, 302, 404, 500])
    async def test_anything_but_200_is_refused_and_a_redirect_is_not_followed(self, status):
        """Following a redirect would reach a host that was never checked."""
        seen: list[httpx.Request] = []
        transport = httpx.MockTransport(
            lambda request: seen.append(request) or httpx.Response(status, headers={'location': 'http://10.0.0.5/'})
        )
        with pytest.raises(ClientMetadataError, match=f'answered HTTP {status}'):
            await _resolver(transport).resolve(CLIENT_METADATA_URL)
        assert len(seen) == 1

    async def test_a_document_over_the_size_limit_is_refused(self):
        oversized = {**client_metadata_document(), 'padding': 'x' * MAX_DOCUMENT_BYTES}
        with pytest.raises(ClientMetadataError, match='larger than'):
            await _resolver(_serve(oversized)).resolve(CLIENT_METADATA_URL)

    async def test_a_slow_server_is_cut_off(self, monkeypatch):
        monkeypatch.setattr(client_metadata, 'FETCH_TIMEOUT_SECONDS', 0.01)

        async def hang(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(1)
            return httpx.Response(200)

        with pytest.raises(ClientMetadataError, match='took longer than'):
            await _resolver(httpx.MockTransport(hang)).resolve(CLIENT_METADATA_URL)

    async def test_a_connection_failure_is_refused(self):
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError('refused', request=request)

        with pytest.raises(ClientMetadataError, match='could not be fetched'):
            await _resolver(httpx.MockTransport(refuse)).resolve(CLIENT_METADATA_URL)


class TestCaching:
    """A document is reused for its Cache-Control lifetime within the bounds, and a failure is never kept."""

    @pytest.mark.parametrize(
        ('cache_control', 'lifetime'),
        [
            (None, MIN_CACHE_SECONDS),
            ('no-store', MIN_CACHE_SECONDS),
            ('max-age=60', MIN_CACHE_SECONDS),
            ('public, max-age=3600', 3600),
            ('max-age=9999999', MAX_CACHE_SECONDS),
            ('max-age=soon', MIN_CACHE_SECONDS),
        ],
    )
    async def test_a_document_is_reused_for_its_bounded_lifetime(self, monkeypatch, cache_control, lifetime):
        clock = _Clock(monkeypatch)
        seen: list[httpx.Request] = []
        resolver = _resolver(_serve(client_metadata_document(), seen=seen, cache_control=cache_control))
        await resolver.resolve(CLIENT_METADATA_URL)
        clock.now += lifetime - 1
        await resolver.resolve(CLIENT_METADATA_URL)
        assert len(seen) == 1
        clock.now += 2
        await resolver.resolve(CLIENT_METADATA_URL)
        assert len(seen) == 2

    async def test_a_failure_is_fetched_again(self):
        responses = iter([httpx.Response(503), httpx.Response(200, json=client_metadata_document())])
        resolver = _resolver(httpx.MockTransport(lambda request: next(responses)))
        with pytest.raises(ClientMetadataError):
            await resolver.resolve(CLIENT_METADATA_URL)
        assert (await resolver.resolve(CLIENT_METADATA_URL)).client_id == CLIENT_METADATA_URL

    async def test_the_least_recently_used_document_leaves_a_full_cache(self, monkeypatch):
        monkeypatch.setattr(client_metadata, 'MAX_CACHED_DOCUMENTS', 2)
        seen: list[httpx.Request] = []

        def serve(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={**client_metadata_document(), 'client_id': _url_of(request)})

        resolver = _resolver(httpx.MockTransport(serve))
        first, second, third = (f'https://app.example.com/{name}.json' for name in ('a', 'b', 'c'))
        for client_id in (first, second, first, third, first):
            await resolver.resolve(client_id)
        assert [request.url.path for request in seen] == ['/a.json', '/b.json', '/c.json']
        await resolver.resolve(second)
        assert len(seen) == 4


class _Clock:
    """``time.monotonic`` as the resolver reads it, moved forward by the test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        monkeypatch.setattr(client_metadata.time, 'monotonic', lambda: self.now)


def _body(document: dict[str, typing.Any]) -> bytes:
    return json.dumps(document).encode()


def _resolver(transport: httpx.AsyncBaseTransport) -> McpClientMetadataResolver:
    """A resolver reaching ``transport`` for any host, as if every host resolved to ``PUBLIC_ADDRESS``."""
    return McpClientMetadataResolver(transport, resolve_host=resolve_to(PUBLIC_ADDRESS))


def _serve(
    document: dict[str, typing.Any], seen: list[httpx.Request] | None = None, cache_control: str | None = None
) -> httpx.MockTransport:
    """A server answering every request with ``document``, recording each request in ``seen``."""

    def serve(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        headers = {'cache-control': cache_control} if cache_control else {}
        return httpx.Response(200, json=document, headers=headers)

    return httpx.MockTransport(serve)


def _url_of(request: httpx.Request) -> str:
    """The URL a request was for, under the host its ``Host`` header names rather than the address it went to."""
    return f'https://{request.headers["host"]}{request.url.raw_path.decode()}'
