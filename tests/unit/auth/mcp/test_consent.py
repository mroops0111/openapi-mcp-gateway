import pytest
from starlette.requests import Request

from openapi_mcp_gateway.auth.mcp.consent import BrowserCookie
from tests.oauth import BROWSER_ID, PLAIN_COOKIE, SECURE_COOKIE


class TestBrowserCookie:
    """The browser cookie is read back only in the form the gateway issues it."""

    @pytest.mark.parametrize(('secure', 'name'), [(True, SECURE_COOKIE), (False, PLAIN_COOKIE)])
    def test_the_host_prefix_needs_https(self, secure, name):
        """Browsers refuse a ``__Host-`` cookie without ``Secure``, which plain HTTP cannot set."""
        assert BrowserCookie(secure).read(_request(f'{name}={BROWSER_ID}')) == BROWSER_ID

    @pytest.mark.parametrize('value', ['short', 'B0' * 32, 'b0' * 32 + 'extra'])
    def test_a_malformed_id_is_ignored(self, value):
        assert BrowserCookie(secure=True).read(_request(f'{SECURE_COOKIE}={value}')) is None

    def test_a_new_browser_is_issued_an_id(self):
        browser_id = BrowserCookie(secure=True).read_or_issue(_request(''))
        assert BrowserCookie(secure=True).read(_request(f'{SECURE_COOKIE}={browser_id}')) == browser_id


def _request(cookie: str) -> Request:
    """A request carrying ``cookie`` as its ``Cookie`` header."""
    return Request({'type': 'http', 'headers': [(b'cookie', cookie.encode())]})
