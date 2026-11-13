import re
import secrets

import pydantic
from starlette.requests import Request
from starlette.responses import Response

from ...stores.base import TokenStore


# How long a sign-in may wait, at the consent page or at the upstream, before it has to start again.
PENDING_AUTHORIZATION_TTL = 900

# How long a browser's approval of a client lasts before the consent page asks again.
CONSENT_TTL = 30 * 24 * 3600

# The cookie naming the browser a sign-in runs in, and the form its value takes.
# The ``__Host-`` prefix needs HTTPS, so a gateway served over plain HTTP, as in development, drops it.
_BROWSER_COOKIE = 'mcp_gateway_browser'
_SECURE_BROWSER_COOKIE = f'__Host-{_BROWSER_COOKIE}'
_BROWSER_ID_PATTERN = re.compile(r'[0-9a-f]{64}')


class PendingAuthorization(pydantic.BaseModel):
    """One sign-in between the MCP client's ``/authorize`` and the upstream's callback.

    It is stored under ``request_id`` until the browser leaves for the upstream,
    then under the fresh ``state`` the gateway sends the upstream, never the client's own ``state``.
    ``browser_id`` is the browser that opened the consent page, the only one that may finish the sign-in.
    ``csrf_token`` is what the consent form must send back for its decision to count.
    """

    request_id: str
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: list[str]
    client_state: str | None
    csrf_token: str
    issuer: str
    upstream_issuer: str | None
    upstream_authorization_server: str
    browser_id: str | None = None

    @property
    def bound_browser_id(self) -> str:
        """The browser this sign-in is bound to, which every step after the consent page has."""
        if self.browser_id is None:
            raise ValueError(f'Sign-in {self.request_id} has not been opened in a browser')
        return self.browser_id


class ConsentRegistry:
    """The clients each browser has approved on this server's consent page, kept in ``store`` for ``CONSENT_TTL``.

    The gateway does not know the user before the upstream sign-in, so an approval belongs to the browser.
    """

    def __init__(self, store: TokenStore, issuer: str) -> None:
        self._store = store
        self._issuer = issuer

    async def is_approved(self, browser_id: str, client_id: str) -> bool:
        return await self._store.get('mcp_consent', self._key(browser_id, client_id)) is not None

    async def approve(self, browser_id: str, client_id: str) -> None:
        await self._store.set('mcp_consent', self._key(browser_id, client_id), {'approved': True}, ttl=CONSENT_TTL)

    def _key(self, browser_id: str, client_id: str) -> str:
        return f'{self._issuer} {browser_id} {client_id}'


class BrowserCookie:
    """The cookie that tells one browser from another, holding a random id and nothing else.

    It is ``HttpOnly`` and ``SameSite=Lax``, so no script reads it and no cross-site form sends it,
    and ``Secure`` with the ``__Host-`` prefix when the gateway is served over HTTPS.
    """

    def __init__(self, secure: bool) -> None:
        self._secure = secure
        self._name = _SECURE_BROWSER_COOKIE if secure else _BROWSER_COOKIE

    def read(self, request: Request) -> str | None:
        """The browser id ``request`` carries, or ``None`` when it carries none or a malformed one."""
        browser_id = request.cookies.get(self._name)
        return browser_id if browser_id and _BROWSER_ID_PATTERN.fullmatch(browser_id) else None

    def read_or_issue(self, request: Request) -> str:
        """The browser id ``request`` carries, or a new one for a browser seen for the first time."""
        return self.read(request) or secrets.token_hex(32)

    def attach(self, response: Response, browser_id: str) -> None:
        """Set ``browser_id`` on ``response``, renewing it for as long as an approval lasts."""
        response.set_cookie(
            self._name,
            browser_id,
            max_age=CONSENT_TTL,
            path='/',
            secure=self._secure,
            httponly=True,
            samesite='lax',
        )
