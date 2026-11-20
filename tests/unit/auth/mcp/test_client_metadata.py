import asyncio
import json
import typing

import httpx
import pytest

from openapi_mcp_gateway.auth.mcp import client_metadata
from openapi_mcp_gateway.auth.mcp.client_metadata import (
    MAX_CACHE_SECONDS,
    MAX_DOCUMENT_BYTES,
    MIN_CACHE_SECONDS,
    ClientMetadataError,
    McpClientMetadataResolver,
    parse_client_metadata_document,
)
from tests.client_metadata import (
    CLIENT_METADATA_HOST,
    CLIENT_METADATA_URL,
    PUBLIC_ADDRESS,
    client_metadata_document,
    requested_url,
    resolve_to,
    resolver_over,
)


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
            ({'grant_types': ['client_credentials']}, 'does not list authorization_code among its grant_types'),
            ({'grant_types': 5}, 'has a grant_types that is not a list'),
            ({'response_types': ['token']}, 'does not list code among its response_types'),
            ({'response_types': 'code'}, 'has a response_types that is not a list'),
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


class TestFetching:
    """A document is fetched from a public address the connection is pinned to, and nothing else."""

    async def test_the_connection_goes_to_the_checked_address_under_the_host_name(self):
        seen: list[httpx.Request] = []
        resolver = resolver_over(_serve(client_metadata_document(), seen=seen))
        assert (await resolver.resolve(CLIENT_METADATA_URL)).client_name == 'Example Agent'
        [request] = seen
        assert request.url.host == PUBLIC_ADDRESS
        assert request.headers['host'] == CLIENT_METADATA_HOST
        assert request.extensions['sni_hostname'] == CLIENT_METADATA_HOST

    @pytest.mark.parametrize(
        'addresses',
        [
            ['10.0.0.5'],
            ['127.0.0.1'],
            ['169.254.169.254'],
            ['::1'],
            ['fe80::1%en0'],
            [PUBLIC_ADDRESS, '10.0.0.5'],
            [],
            ['64:ff9b::a9fe:a9fe'],
            ['::ffff:10.0.0.5'],
            ['2002:a00:5::1'],
        ],
        ids=[
            'private',
            'loopback',
            'link local',
            'IPv6 loopback',
            'zoned link local',
            'one of two private',
            'none',
            'NAT64 of link local',
            'IPv4 mapped',
            '6to4 of private',
        ],
    )
    async def test_a_host_resolving_to_any_private_or_local_address_is_refused(self, addresses):
        """One public address is not enough, since a later resolution may pick another of them."""
        resolver = McpClientMetadataResolver(_serve(client_metadata_document()), resolve_host=resolve_to(*addresses))
        with pytest.raises(ClientMetadataError, match=r'is at app\.example\.com'):
            await resolver.resolve(CLIENT_METADATA_URL)

    async def test_a_client_id_that_is_no_metadata_url_is_never_fetched(self):
        with pytest.raises(ClientMetadataError, match='is not at an https URL with a path'):
            await resolver_over(_serve(client_metadata_document())).resolve('http://app.example.com/client.json')

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
            await resolver_over(transport).resolve(CLIENT_METADATA_URL)
        assert len(seen) == 1

    async def test_no_compressed_body_is_asked_for_or_read(self):
        """A compressed body would be unpacked whole before the size limit could refuse it."""
        seen: list[httpx.Request] = []

        def serve(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b'\x1f\x8b', headers={'content-encoding': 'gzip'})

        with pytest.raises(ClientMetadataError, match='came gzip-encoded'):
            await resolver_over(httpx.MockTransport(serve)).resolve(CLIENT_METADATA_URL)
        assert seen[0].headers['accept-encoding'] == 'identity'

    async def test_a_document_over_the_size_limit_is_refused(self):
        oversized = {**client_metadata_document(), 'padding': 'x' * MAX_DOCUMENT_BYTES}
        with pytest.raises(ClientMetadataError, match='larger than'):
            await resolver_over(_serve(oversized)).resolve(CLIENT_METADATA_URL)

    async def test_a_slow_server_is_cut_off(self, monkeypatch):
        monkeypatch.setattr(client_metadata, 'FETCH_TIMEOUT_SECONDS', 0.01)

        async def hang(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(1)
            return httpx.Response(200)

        with pytest.raises(ClientMetadataError, match='took longer than'):
            await resolver_over(httpx.MockTransport(hang)).resolve(CLIENT_METADATA_URL)

    async def test_a_connection_failure_is_refused(self):
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError('refused', request=request)

        with pytest.raises(ClientMetadataError, match='could not be fetched'):
            await resolver_over(httpx.MockTransport(refuse)).resolve(CLIENT_METADATA_URL)


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
        resolver = resolver_over(_serve(client_metadata_document(), seen=seen, cache_control=cache_control))
        await resolver.resolve(CLIENT_METADATA_URL)
        clock.now += lifetime - 1
        await resolver.resolve(CLIENT_METADATA_URL)
        assert len(seen) == 1
        clock.now += 2
        await resolver.resolve(CLIENT_METADATA_URL)
        assert len(seen) == 2

    async def test_a_failure_is_fetched_again(self):
        responses = iter([httpx.Response(503), httpx.Response(200, json=client_metadata_document())])
        resolver = resolver_over(httpx.MockTransport(lambda request: next(responses)))
        with pytest.raises(ClientMetadataError):
            await resolver.resolve(CLIENT_METADATA_URL)
        assert (await resolver.resolve(CLIENT_METADATA_URL)).client_id == CLIENT_METADATA_URL

    async def test_the_least_recently_used_document_leaves_a_full_cache(self, monkeypatch):
        monkeypatch.setattr(client_metadata, 'MAX_CACHED_DOCUMENTS', 2)
        seen: list[httpx.Request] = []

        def serve(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={**client_metadata_document(), 'client_id': requested_url(request)})

        resolver = resolver_over(httpx.MockTransport(serve))
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
