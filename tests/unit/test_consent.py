import dataclasses

import pytest
from starlette.requests import Request

from openapi_mcp_gateway.auth.flows.consent import BrowserCookie
from openapi_mcp_gateway.auth.flows.consent_page import ConsentPrompt, DescribedScope, consent_page
from tests.oauth import BROWSER_ID


# A prompt for a client that registered itself under a name meant to break out of the page.
HOSTILE_PROMPT = ConsentPrompt(
    server_name='petstore',
    upstream_host='auth.example.com',
    client_id='client-1',
    client_name='<script>alert(1)</script>',
    redirect_uri='https://app.example.com/cb?next="><img src=x>',
    upstream_scopes=[DescribedScope('read', None)],
    mcp_scopes=[DescribedScope('pets:read', '<b>Read</b> pets')],
    opens_unscoped_tools=False,
    csrf_token='token',
)


class TestConsentPage:
    """Everything a client supplies reaches the page escaped."""

    def test_a_hostile_client_name_is_escaped(self):
        page = _page_text(HOSTILE_PROMPT)
        assert '<script>' not in page
        assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page

    def test_a_hostile_redirect_uri_is_escaped(self):
        page = _page_text(HOSTILE_PROMPT)
        assert '<img' not in page

    def test_a_client_without_a_name_is_shown_by_its_id(self):
        page = _page_text(dataclasses.replace(HOSTILE_PROMPT, client_name=None))
        assert 'Allow client-1 to use petstore?' in page

    def test_a_hostile_scope_description_is_escaped(self):
        assert '&lt;b&gt;Read&lt;/b&gt; pets<code>pets:read</code>' in _page_text(HOSTILE_PROMPT)

    def test_a_scope_without_a_description_is_shown_by_its_name(self):
        assert '<li><code>read</code></li>' in _page_text(HOSTILE_PROMPT)

    @pytest.mark.parametrize('mcp_scopes', [[], HOSTILE_PROMPT.mcp_scopes])
    def test_tools_open_to_every_client_are_named(self, mcp_scopes):
        """A list of scopes alone would understate what a server that opens unscoped tools grants."""
        prompt = dataclasses.replace(HOSTILE_PROMPT, mcp_scopes=mcp_scopes, opens_unscoped_tools=True)
        page = _page_text(prompt)
        assert 'Use the tools on petstore open to every client' in page
        assert 'No tools' not in page

    def test_a_client_with_nothing_open_to_it_is_told_so(self):
        assert 'No tools on petstore' in _page_text(dataclasses.replace(HOSTILE_PROMPT, mcp_scopes=[]))

    def test_no_upstream_scopes_leaves_their_list_out(self):
        page = _page_text(dataclasses.replace(HOSTILE_PROMPT, upstream_scopes=[]))
        assert 'will be asked for' not in page


class TestBrowserCookie:
    """The browser cookie is read back only in the form the gateway issues it."""

    @pytest.mark.parametrize(('secure', 'name'), [(True, '__Host-mcp_gateway_browser'), (False, 'mcp_gateway_browser')])
    def test_the_host_prefix_needs_https(self, secure, name):
        """Browsers refuse a ``__Host-`` cookie without ``Secure``, which plain HTTP cannot set."""
        assert BrowserCookie(secure).read(_request(f'{name}={BROWSER_ID}')) == BROWSER_ID

    @pytest.mark.parametrize('value', ['short', 'B0' * 32, 'b0' * 32 + 'extra'])
    def test_a_malformed_id_is_ignored(self, value):
        assert BrowserCookie(secure=True).read(_request(f'__Host-mcp_gateway_browser={value}')) is None

    def test_a_new_browser_is_issued_an_id(self):
        browser_id = BrowserCookie(secure=True).read_or_issue(_request(''))
        assert BrowserCookie(secure=True).read(_request(f'__Host-mcp_gateway_browser={browser_id}')) == browser_id


def _page_text(prompt: ConsentPrompt) -> str:
    """The HTML the consent page for ``prompt`` serves."""
    return bytes(consent_page(prompt).body).decode()


def _request(cookie: str) -> Request:
    """A request carrying ``cookie`` as its ``Cookie`` header."""
    return Request({'type': 'http', 'headers': [(b'cookie', cookie.encode())]})
