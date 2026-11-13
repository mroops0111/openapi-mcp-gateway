import pathlib
import re
from unittest.mock import AsyncMock, patch

import httpx2
import pytest
from starlette.testclient import TestClient
from starlette.types import ASGIApp

from tests.constants import AUTHORIZE_URL, GATEWAY_URL
from tests.oauth import (
    CLIENT_REDIRECT_URI,
    CLIENT_STATE,
    authorization_code_gateway,
    query_value,
    upstream_token_response,
)


# The pre-registered clients, two the user has to approve and one the operator exempts.
AGENT = 'agent'
ASSISTANT = 'assistant'
TRUSTED = 'trusted'
CLIENTS = {AGENT: {}, ASSISTANT: {}, TRUSTED: {'consent': False}}

# The token form of a ``code_challenge``, which the SDK checks only for shape.
CODE_CHALLENGE = 'c' * 43


@pytest.fixture
def app(petstore_json_path: pathlib.Path) -> ASGIApp:
    """The gateway's app, its petstore server signing in through ``authorization_code``."""
    return authorization_code_gateway(petstore_json_path, CLIENTS)._build_app(transport='streamable-http')


@pytest.fixture
def browser(app: ASGIApp) -> TestClient:
    """The browser the user signs in with, keeping its cookies across requests."""
    return _browser(app)


@pytest.fixture
def other_browser(app: ASGIApp) -> TestClient:
    """Another browser, such as an attacker's, that never saw the user's consent page."""
    return _browser(app)


class TestConsentPage:
    """A client the browser has not approved is shown the consent page before the upstream sign-in."""

    def test_the_page_names_the_client_and_where_the_code_goes(self, browser):
        page = _open_consent(browser)
        assert page.status_code == 200
        assert AGENT in page.text
        assert 'http://<b>localhost:3000</b>/callback' in page.text

    def test_the_page_cannot_be_framed(self, browser):
        """A framed page could be clicked through by a page laid over it."""
        page = _open_consent(browser)
        assert page.headers['x-frame-options'] == 'DENY'
        assert "frame-ancestors 'none'" in page.headers['content-security-policy']

    def test_the_browser_cookie_is_bound_to_the_gateway_host(self, browser):
        cookie = _open_consent(browser).headers['set-cookie']
        assert cookie.startswith('__Host-mcp_gateway_browser=')
        assert {'Secure', 'HttpOnly', 'SameSite=lax', 'Path=/'} <= {part.strip() for part in cookie.split(';')}

    def test_an_exempt_client_goes_straight_to_the_upstream(self, browser):
        response = _open_consent(browser, TRUSTED)
        assert response.status_code == 302
        assert response.headers['location'].startswith(f'{AUTHORIZE_URL}?')


class TestScopeDescriptions:
    """The page leads each scope with what it allows, as the spec and ``auth.operation_scopes`` describe it."""

    def test_both_kinds_of_scope_are_described(self, petstore_json_path):
        gateway = authorization_code_gateway(
            petstore_json_path,
            {AGENT: {'scopes': ['pets:read']}},
            operation_scopes={'auto': 'tags'},
            upstream={'scopes': ['read:pets']},
        )
        page = _open_consent(_browser(gateway._build_app(transport='streamable-http'))).text
        assert 'Read pets<code>pets:read</code>' in page
        assert 'Read pets<code>read:pets</code>' in page


class TestConsentDecision:
    """What the user decides on the consent page, and how long an approval is remembered."""

    def test_approval_continues_to_the_upstream(self, browser):
        response = _decide(browser, _open_consent(browser), 'approve')
        assert response.status_code == 303
        assert response.headers['location'].startswith(f'{AUTHORIZE_URL}?')

    def test_an_approved_client_is_not_asked_again_in_the_same_browser(self, browser):
        _decide(browser, _open_consent(browser), 'approve')
        response = _open_consent(browser)
        assert response.status_code == 302
        assert response.headers['location'].startswith(f'{AUTHORIZE_URL}?')

    def test_an_approval_covers_only_its_own_client(self, browser):
        _decide(browser, _open_consent(browser), 'approve')
        assert _open_consent(browser, ASSISTANT).status_code == 200

    def test_an_approval_covers_only_its_own_browser(self, browser, other_browser):
        _decide(browser, _open_consent(browser), 'approve')
        assert _open_consent(other_browser).status_code == 200

    def test_denial_tells_the_client_with_its_own_state(self, browser):
        """RFC 6749 §4.1.2.1 sends the refusal to the client's redirect URI as ``access_denied``."""
        response = _decide(browser, _open_consent(browser), 'deny')
        location = response.headers['location']
        assert response.status_code == 303
        assert location.startswith(CLIENT_REDIRECT_URI)
        assert query_value(location, 'error') == 'access_denied'
        assert query_value(location, 'state') == CLIENT_STATE

    def test_a_denied_sign_in_cannot_be_approved_afterwards(self, browser):
        page = _open_consent(browser)
        _decide(browser, page, 'deny')
        assert _decide(browser, page, 'approve').status_code == 400


class TestForgedDecision:
    """Only the consent form the gateway sent, from the browser it was sent to, decides a sign-in."""

    def test_a_form_without_the_csrf_token_is_refused(self, browser):
        response = _decide(browser, _open_consent(browser), 'approve', csrf_token='forged')
        assert response.status_code == 400

    def test_another_browser_cannot_decide(self, browser, other_browser):
        """A cross-site form carries no ``SameSite=Lax`` cookie, which is what this browser lacks too."""
        page = _open_consent(browser)
        assert _decide(other_browser, page, 'approve').status_code == 400

    def test_another_browser_cannot_open_a_page_already_shown(self, browser, other_browser):
        """A consent link passed on after the user opened it is worthless to whoever receives it."""
        page = _open_consent(browser)
        assert other_browser.get(str(page.url)).status_code == 400


class TestBrowserBoundState:
    """The upstream's callback finishes a sign-in only in the browser that approved it."""

    def test_the_approving_browser_finishes_with_the_clients_state(self, browser):
        location = self._callback(browser, browser).headers['location']
        assert location.startswith(CLIENT_REDIRECT_URI)
        assert query_value(location, 'code').startswith('mcp_')
        assert query_value(location, 'state') == CLIENT_STATE

    def test_another_browser_cannot_finish(self, browser, other_browser):
        """A code delivered to another browser is the login CSRF the browser-bound ``state`` prevents."""
        assert self._callback(browser, other_browser).status_code == 400

    @staticmethod
    def _callback(approving: TestClient, finishing: TestClient) -> httpx2.Response:
        """Approve a sign-in in ``approving``, then deliver the upstream's redirect for it to ``finishing``."""
        upstream_url = _decide(approving, _open_consent(approving), 'approve').headers['location']
        with patch('httpx.AsyncClient.post', AsyncMock(return_value=upstream_token_response())):
            return finishing.get(
                '/petstore/auth/callback', params={'code': 'upstream-code', 'state': query_value(upstream_url, 'state')}
            )


def _browser(app: ASGIApp) -> TestClient:
    """A browser on the gateway's public HTTPS address, which ``__Host-`` cookies need, that stops at each redirect."""
    return TestClient(app, base_url=GATEWAY_URL, follow_redirects=False)


def _open_consent(browser: TestClient, client_id: str = AGENT) -> httpx2.Response:
    """Start a sign-in for ``client_id`` at the SDK's ``/authorize``, and follow it to the consent page."""
    authorize = browser.get(
        '/petstore/authorize',
        params={
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': CLIENT_REDIRECT_URI,
            'code_challenge': CODE_CHALLENGE,
            'code_challenge_method': 'S256',
            'state': CLIENT_STATE,
        },
    )
    assert authorize.status_code == 302
    return browser.get(authorize.headers['location'])


def _decide(
    browser: TestClient, page: httpx2.Response, decision: str, csrf_token: str | None = None
) -> httpx2.Response:
    """Post ``decision`` from ``page``, with the CSRF token it carries unless ``csrf_token`` replaces it."""
    sent = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert sent is not None
    return browser.post(str(page.url), data={'csrf_token': csrf_token or sent.group(1), 'decision': decision})
