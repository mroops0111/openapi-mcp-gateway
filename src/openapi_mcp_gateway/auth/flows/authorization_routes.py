import logging
import secrets

from mcp.server import MCPServer
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ...scopes import McpScopePolicy
from .authorization_code import CALLBACK_PATH, CONSENT_PATH, AuthorizationCodeProvider
from .consent import BrowserCookie, PendingAuthorization
from .consent_page import PAGE_HEADERS, consent_page, error_page


logger = logging.getLogger(__name__)


# Why a consent page cannot go on, as the user reads it.
_UNKNOWN_SIGN_IN = 'This sign-in has expired, was already finished, or was opened in another browser.'
_TAMPERED_FORM = 'The approval form was not the one this gateway sent.'
_INCOMPLETE_CALLBACK = 'The sign-in service sent you back without an answer.'


class AuthorizationCodeRoutes:
    """The pages a browser visits during an ``authorization_code`` sign-in, mounted under each server's path.

    ``CONSENT_PATH`` shows the consent page and takes its decision,
    and ``CALLBACK_PATH`` is where the upstream sends the browser back.
    Each step reads the browser cookie, which ties the whole sign-in to the browser that approved it.
    ``mcp_scope_policy`` tells the consent page what each MCP scope allows, and which tools need none.
    """

    def __init__(self, provider: AuthorizationCodeProvider, mcp_scope_policy: McpScopePolicy) -> None:
        self._provider = provider
        self._mcp_scope_policy = mcp_scope_policy
        self._cookie = BrowserCookie(secure=provider.issuer.startswith('https://'))

    def register(self, mcp: MCPServer) -> None:
        mcp.custom_route(CONSENT_PATH, methods=['GET'])(self.show_consent)
        mcp.custom_route(CONSENT_PATH, methods=['POST'])(self.submit_consent)
        mcp.custom_route(CALLBACK_PATH, methods=['GET'])(self.finish_upstream)

    async def show_consent(self, request: Request) -> Response:
        """Bind the sign-in to this browser, then ask for consent or go straight on to the upstream.

        The page is skipped for a client the operator exempts, and for one this browser already approved.
        """
        browser_id = self._cookie.read_or_issue(request)
        pending = await self._provider.claim_authorization(request.query_params.get('request', ''), browser_id)
        if pending is None:
            response: Response = error_page(_UNKNOWN_SIGN_IN, 400)
        elif await self._provider.requires_consent(pending):
            response = consent_page(await self._provider.consent_prompt(pending, self._mcp_scope_policy))
        else:
            response = _redirect(await self._provider.continue_to_upstream(pending), 302)
        self._cookie.attach(response, browser_id)
        return response

    async def submit_consent(self, request: Request) -> Response:
        """Act on the decision the consent page posts, which counts only from the browser the page was shown to."""
        pending = await self._shown_authorization(request)
        if pending is None:
            return error_page(_UNKNOWN_SIGN_IN, 400)
        form = await request.form()
        if not secrets.compare_digest(str(form.get('csrf_token', '')), pending.csrf_token):
            logger.warning('OAuth consent rejected: reason=csrf_mismatch client_id=%s', pending.client_id)
            return error_page(_TAMPERED_FORM, 400)
        match form.get('decision'):
            case 'approve':
                redirect_uri = await self._provider.approve(pending)
            case 'deny':
                redirect_uri = await self._provider.deny(pending)
            case _:
                return error_page(_TAMPERED_FORM, 400)
        return _redirect(redirect_uri, 303)

    async def finish_upstream(self, request: Request) -> Response:
        """Take the upstream's redirect, and send the browser on to the MCP client with its code or the refusal.

        A failure the client cannot be told about is shown on the gateway's error page.
        """
        query = request.query_params
        state = query.get('state')
        browser_id = self._cookie.read(request)
        try:
            if state and (error := query.get('error')):
                redirect_uri = await self._provider.handle_upstream_error(
                    error, state, browser_id, query.get('error_description')
                )
            elif state and (code := query.get('code')):
                redirect_uri = await self._provider.handle_upstream_callback(code, state, browser_id, query.get('iss'))
            else:
                return error_page(_INCOMPLETE_CALLBACK, 400)
        except HTTPException as exc:
            return error_page(exc.detail, exc.status_code)
        return _redirect(redirect_uri, 302)

    async def _shown_authorization(self, request: Request) -> PendingAuthorization | None:
        """The sign-in whose consent page this browser was shown, or ``None`` for any other browser."""
        browser_id = self._cookie.read(request)
        if browser_id is None:
            return None
        return await self._provider.shown_authorization(request.query_params.get('request', ''), browser_id)


def _redirect(url: str, status_code: int) -> RedirectResponse:
    """A redirect no cache keeps, since each one carries a single-use ``state`` or code."""
    return RedirectResponse(url, status_code=status_code, headers={'Cache-Control': PAGE_HEADERS['Cache-Control']})
