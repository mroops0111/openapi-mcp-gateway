import asyncio
import collections
import collections.abc
import ipaddress
import json
import logging
import socket
import time
import urllib.parse

import httpx
import pydantic
from pydantic import AnyUrl


logger = logging.getLogger(__name__)


# The most a client's metadata document may weigh, the bound the CIMD draft §6.6 recommends.
MAX_DOCUMENT_BYTES = 5 * 1024

# How long fetching a document may take, name resolution included.
FETCH_TIMEOUT_SECONDS = 5.0

# How long a fetched document is used, which its Cache-Control sets within these bounds.
MIN_CACHE_SECONDS = 5 * 60
MAX_CACHE_SECONDS = 24 * 60 * 60

# How many documents one process keeps, so a stream of new client IDs cannot grow it without bound.
MAX_CACHED_DOCUMENTS = 1000


# Resolve a host and port to the addresses a connection to them may reach.
HostResolver = collections.abc.Callable[[str, int], collections.abc.Awaitable[list[str]]]


# The NAT64 prefix RFC 6052 §2.1 reserves, which carries an IPv4 address in its last 32 bits.
_NAT64_NETWORK = ipaddress.IPv6Network('64:ff9b::/96')


class ClientMetadataError(Exception):
    """Why a client's metadata document cannot be used, worded to follow "its metadata document"."""


class ClientMetadataDocument(pydantic.BaseModel):
    """The fields the gateway reads from a client's metadata document, named as RFC 7591 §2 names them."""

    client_id: str
    client_name: str = pydantic.Field(min_length=1)
    redirect_uris: list[AnyUrl] = pydantic.Field(min_length=1)


class McpClientMetadataResolver:
    """Fetches, checks and caches the metadata documents that Client ID Metadata Document clients publish.

    It stands in for the server side the MCP SDK does not have yet (python-sdk#1801), to be swapped for the SDK's.
    A document is fetched over ``https`` only, from a public address the connection is pinned to,
    without following redirects, and within ``MAX_DOCUMENT_BYTES`` and ``FETCH_TIMEOUT_SECONDS``.
    It is used for its Cache-Control lifetime held between ``MIN_CACHE_SECONDS`` and ``MAX_CACHE_SECONDS``,
    and a failure is never cached.
    ``transport`` and ``resolve_host`` replace the network, which tests need since a local server is refused.
    """

    def __init__(
        self, transport: httpx.AsyncBaseTransport | None = None, resolve_host: HostResolver | None = None
    ) -> None:
        self._transport = transport
        self._resolve_host = resolve_host or _resolve_host
        self._cache: collections.OrderedDict[str, tuple[float, ClientMetadataDocument]] = collections.OrderedDict()

    async def resolve(self, client_id: str) -> ClientMetadataDocument:
        """The document ``client_id`` points to, checked against the CIMD draft, or ``ClientMetadataError``."""
        cached = self._cache.get(client_id)
        if cached is not None and cached[0] > time.monotonic():
            self._cache.move_to_end(client_id)
            return cached[1]
        try:
            async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
                body, lifetime = await self._fetch(client_id)
        except TimeoutError as error:
            raise ClientMetadataError(f'took longer than {FETCH_TIMEOUT_SECONDS:g} seconds to fetch') from error
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise ClientMetadataError(f'could not be fetched ({type(error).__name__})') from error
        document = parse_client_metadata_document(client_id, body)
        self._remember(client_id, document, lifetime)
        return document

    async def _fetch(self, client_id: str) -> tuple[bytes, int]:
        """The body at ``client_id`` and how long it may be used, fetched from the public address its host names."""
        if not is_client_metadata_url(client_id):
            raise ClientMetadataError('is not at an https URL with a path')
        url = urllib.parse.urlsplit(client_id)
        host = url.hostname or ''
        address = await self._pick_public_address(host, url.port or 443)
        # A proxy from the environment would reach the address rather than the host, and so fail its certificate.
        async with httpx.AsyncClient(transport=self._transport, follow_redirects=False, trust_env=False) as http:
            request = http.build_request(
                'GET',
                urllib.parse.urlunsplit(url._replace(netloc=_netloc_at(address, url.port))),
                # A compressed body would be unpacked whole before its size is checked, so none is asked for.
                headers={'Host': url.netloc, 'Accept': 'application/json', 'Accept-Encoding': 'identity'},
                # The certificate is checked against the host, while the connection goes to the address checked here.
                extensions={'sni_hostname': host},
            )
            response = await http.send(request, stream=True)
            try:
                if response.status_code != 200:
                    raise ClientMetadataError(f'answered HTTP {response.status_code}')
                encoding = response.headers.get('content-encoding', 'identity').strip().lower()
                if encoding != 'identity':
                    raise ClientMetadataError(f'came {encoding}-encoded, while only an unencoded document is read')
                body = await _read_within_limit(response)
            finally:
                await response.aclose()
        return body, _cache_lifetime(response.headers.get('cache-control'))

    async def _pick_public_address(self, host: str, port: int) -> str:
        """An address ``host`` resolves to, refusing a host that resolves to any private or local one.

        Every address must be public, since a later resolution may pick another of them.
        """
        try:
            addresses = [host] if _is_ip_literal(host) else await self._resolve_host(host, port)
        except OSError as error:
            raise ClientMetadataError(f'is at {host}, which does not resolve') from error
        if not addresses:
            raise ClientMetadataError(f'is at {host}, which does not resolve')
        for address in addresses:
            if not _is_public_address(address):
                raise ClientMetadataError(f'is at {host}, which resolves to {address}, a private or local address')
        return addresses[0]

    def _remember(self, client_id: str, document: ClientMetadataDocument, lifetime: int) -> None:
        self._cache[client_id] = (time.monotonic() + lifetime, document)
        self._cache.move_to_end(client_id)
        while len(self._cache) > MAX_CACHED_DOCUMENTS:
            self._cache.popitem(last=False)


def is_client_metadata_url(client_id: str) -> bool:
    """Whether ``client_id`` is a URL the CIMD draft §3 accepts as a client identifier.

    It is ``https`` with a host and a path, without a fragment, userinfo, or a ``.`` or ``..`` path segment.
    Only printable ASCII is accepted, so a host or path the HTTP request cannot carry is refused here.
    """
    if not all('!' <= character <= '~' for character in client_id):
        return False
    try:
        url = urllib.parse.urlsplit(client_id)
        _ = url.port
    except ValueError:
        return False
    segments = url.path.split('/')
    return (
        url.scheme == 'https'
        and bool(url.hostname)
        and url.path not in ('', '/')
        and '#' not in client_id
        and '@' not in url.netloc
        and '.' not in segments
        and '..' not in segments
    )


def parse_client_metadata_document(client_id: str, body: bytes) -> ClientMetadataDocument:
    """The document in ``body``, fetched from ``client_id``, or ``ClientMetadataError`` saying what is wrong.

    It must name ``client_id`` as its own, with a name and redirect URIs that are ``https`` or loopback.
    The CIMD draft §4.1 forbids a shared secret, and the SDK verifies no ``private_key_jwt``,
    so only a public client is accepted, which is what a document that names no method is.
    """
    try:
        data = json.loads(body)
    except ValueError as error:
        raise ClientMetadataError('is not JSON') from error
    if not isinstance(data, dict):
        raise ClientMetadataError('is not a JSON object')
    if data.get('client_id') != client_id:
        raise ClientMetadataError(f'names client_id {data.get("client_id")!r} rather than its own URL')
    if 'client_secret' in data or 'client_secret_expires_at' in data:
        raise ClientMetadataError('carries a client_secret, which the CIMD draft forbids')
    method = data.get('token_endpoint_auth_method', 'none')
    if method != 'none':
        raise ClientMetadataError(f'authenticates with {method!r}, while only public clients are accepted')
    for field, required in (('grant_types', 'authorization_code'), ('response_types', 'code')):
        listed = data.get(field, [required])
        if not isinstance(listed, list):
            raise ClientMetadataError(f'has a {field} that is not a list')
        if required not in listed:
            raise ClientMetadataError(f'does not list {required} among its {field}')
    try:
        document = ClientMetadataDocument.model_validate(data)
    except pydantic.ValidationError as error:
        fields = ', '.join(sorted({str(detail['loc'][0]) for detail in error.errors()}))
        raise ClientMetadataError(f'has a missing or invalid {fields}') from error
    for redirect_uri in document.redirect_uris:
        if redirect_uri.scheme != 'https' and not is_loopback_redirect(redirect_uri):
            raise ClientMetadataError(f'lists redirect URI {redirect_uri}, which is neither https nor loopback')
    return document


def is_loopback_redirect(uri: AnyUrl) -> bool:
    """Whether ``uri`` is an ``http`` redirect to this machine, as RFC 8252 §7.3 defines a loopback redirect.

    ``localhost`` counts as well as the IP literals, since clients such as Claude Code register it.
    """
    if uri.scheme != 'http' or uri.host is None:
        return False
    host = uri.host.strip('[]')
    return host == 'localhost' or (_is_ip_literal(host) and ipaddress.ip_address(host).is_loopback)


async def _resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _is_public_address(address: str) -> bool:
    """Whether ``address`` reaches the public internet, judged by the IPv4 address it carries when it carries one.

    ``is_global`` passes a NAT64, 6to4 or Teredo address whose IPv4 address is private,
    and a gateway on such a network forwards to it.
    """
    # A link-local IPv6 address may carry its zone, which ``ipaddress`` does not parse.
    ip = ipaddress.ip_address(address.partition('%')[0])
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is None and ip in _NAT64_NETWORK:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and not embedded.is_global:
            return False
    return ip.is_global


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _netloc_at(address: str, port: int | None) -> str:
    """The URL authority that reaches ``address`` on ``port``, bracketing an IPv6 address."""
    host = f'[{address}]' if ipaddress.ip_address(address).version == 6 else address
    return f'{host}:{port}' if port else host


async def _read_within_limit(response: httpx.Response) -> bytes:
    """The body of ``response``, refused once it passes ``MAX_DOCUMENT_BYTES`` without reading the rest.

    It counts the bytes as sent, since a response with any ``Content-Encoding`` was refused before.
    """
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_DOCUMENT_BYTES:
            raise ClientMetadataError(f'is larger than {MAX_DOCUMENT_BYTES} bytes')
    return bytes(body)


def _cache_lifetime(cache_control: str | None) -> int:
    """How many seconds a document may be used, its ``max-age`` held within the cache bounds.

    A document that says nothing, or asks not to be stored, is used for the shortest bound.
    The gateway needs it on every sign-in and refresh, so refetching it on each would only add failures.
    """
    directives = {
        name.strip().lower(): value.strip()
        for name, _, value in (part.partition('=') for part in (cache_control or '').split(','))
    }
    if 'no-store' in directives or 'no-cache' in directives:
        return MIN_CACHE_SECONDS
    try:
        max_age = int(directives.get('max-age', ''))
    except ValueError:
        return MIN_CACHE_SECONDS
    return max(MIN_CACHE_SECONDS, min(max_age, MAX_CACHE_SECONDS))
