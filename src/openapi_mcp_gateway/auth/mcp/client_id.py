import ipaddress
import urllib.parse

from pydantic import AnyUrl


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


def client_metadata_host(client_id: str) -> str | None:
    """The host a Client ID Metadata Document client's document sits on, or ``None`` for any other ``client_id``."""
    return urllib.parse.urlsplit(client_id).hostname if is_client_metadata_url(client_id) else None


def is_loopback_redirect(uri: AnyUrl) -> bool:
    """Whether ``uri`` is an ``http`` redirect to this machine, as RFC 8252 §7.3 defines a loopback redirect.

    ``localhost`` counts as well as the IP literals, since clients such as Claude Code register it.
    """
    if uri.scheme != 'http' or uri.host is None:
        return False
    host = uri.host.strip('[]')
    return host == 'localhost' or _is_loopback_address(host)


def _is_loopback_address(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
