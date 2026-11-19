import base64
import binascii
import hmac
import logging
import typing
import urllib.parse

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.extension import Extension
from mcp.shared.auth import InvalidScopeError, OAuthToken
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..routes import AuthRoutes
from .client import McpPreRegisteredClient, McpPreRegisteredClientDirectory
from .token_issuer import McpTokenAttachment, McpTokenIssuer


logger = logging.getLogger(__name__)


# Where a client obtains a token, and the authorize endpoint the SDK's metadata model requires though no user signs in.
TOKEN_PATH = '/token'  # noqa: S105
AUTHORIZE_PATH = '/authorize'

# The MCP extension this grant implements, advertised under the server's capabilities.
CLIENT_CREDENTIALS_EXTENSION = 'io.modelcontextprotocol/oauth-client-credentials'

# The ways a client may present its secret at the token endpoint, in the order RFC 6749 §2.3.1 prefers them.
TOKEN_ENDPOINT_AUTH_METHODS = ('client_secret_basic', 'client_secret_post')

# A token response carries no cache headers, as RFC 6749 §5.1 requires.
_NO_STORE = {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}


class McpClientCredentialsError(Exception):
    """A token request refused with an RFC 6749 §5.2 error, which the route returns as JSON."""

    def __init__(self, error: str, description: str, status_code: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code


class McpClientCredentialsGrant:
    """The gateway's ``client_credentials`` grant, where a pre-registered agent obtains an MCP token as itself.

    No user stands behind the token, so it links no upstream credential, and no refresh token is issued,
    as RFC 6749 §4.4.3 advises, since the client can always ask again.
    A request with no ``scope`` is granted every scope the client may hold,
    since nothing would approve a narrower start and step-up would follow at once.
    """

    def __init__(self, tokens: McpTokenIssuer, clients: McpPreRegisteredClientDirectory) -> None:
        self.tokens = tokens
        self.clients = clients

    async def authenticate(self, client_id: str, client_secret: str) -> McpPreRegisteredClient:
        """The client ``client_id`` names, once ``client_secret`` proves it, compared in constant time."""
        client = await self.clients.get(client_id)
        expected = client.client_secret if client else None
        if client is None or not expected or not hmac.compare_digest(expected.encode(), client_secret.encode()):
            logger.warning('Client credentials rejected: reason=invalid_client client_id=%r', client_id)
            raise McpClientCredentialsError('invalid_client', 'Client authentication failed', status_code=401)
        return client

    async def exchange(self, client: McpPreRegisteredClient, requested_scope: str | None) -> OAuthToken:
        """Mint an MCP access token for ``client``, granting what it asks for within the scopes it may hold."""
        try:
            scopes = client.validate_scope(requested_scope)
        except InvalidScopeError as exc:
            raise McpClientCredentialsError('invalid_scope', exc.message) from exc
        token = await self.tokens.issue_access_token(client, scopes, McpTokenAttachment())
        logger.info('Client credentials token issued: client_id=%s scopes=%s', client.client_id, scopes)
        return token


class McpClientCredentialsRoutes(AuthRoutes):
    """The token endpoint of the gateway's ``client_credentials`` grant, under the server's own path.

    ``resource`` is the endpoint's canonical URI, the one RFC 8707 ``resource`` parameter must name when sent.
    ``/authorize`` answers ``unauthorized_client``, since the SDK's metadata model requires one,
    but no user signs in here.
    """

    def __init__(self, grant: McpClientCredentialsGrant, resource: str) -> None:
        self._grant = grant
        self._resource = resource

    def register(self, mcp: MCPServer) -> None:
        mcp.custom_route(TOKEN_PATH, methods=['POST'])(self.issue_token)
        mcp.custom_route(AUTHORIZE_PATH, methods=['GET'])(self.refuse_authorization)

    async def issue_token(self, request: Request) -> Response:
        """Answer a token request, as RFC 6749 §4.4.2 and §5 describe it."""
        form = await request.form()
        try:
            if form.get('grant_type') != 'client_credentials':
                raise McpClientCredentialsError('unsupported_grant_type', 'Only client_credentials is supported')
            resource = form.get('resource')
            if resource is not None and str(resource).rstrip('/') != self._resource:
                raise McpClientCredentialsError('invalid_target', f'Tokens here are for {self._resource}')
            client_id, client_secret = _read_client_credentials(request, form)
            client = await self._grant.authenticate(client_id, client_secret)
            scope = form.get('scope')
            token = await self._grant.exchange(client, str(scope) if scope is not None else None)
        except McpClientCredentialsError as exc:
            headers = {**_NO_STORE, 'WWW-Authenticate': 'Basic'} if exc.status_code == 401 else _NO_STORE
            return JSONResponse(
                {'error': exc.error, 'error_description': exc.description}, status_code=exc.status_code, headers=headers
            )
        return JSONResponse(token.model_dump(exclude_none=True), headers=_NO_STORE)

    async def refuse_authorization(self, request: Request) -> Response:
        """Refuse a browser sign-in, which this server has no user for."""
        return JSONResponse(
            {
                'error': 'unauthorized_client',
                'error_description': 'This server issues tokens through client_credentials only',
            },
            status_code=400,
        )


class McpGatewayTokenVerifier(TokenVerifier):
    """The SDK's ``TokenVerifier`` for the access tokens the gateway issued itself."""

    def __init__(self, tokens: McpTokenIssuer) -> None:
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self._tokens.load_access_token(token)


class McpClientCredentialsExtension(Extension):
    """Tells an MCP client it may obtain a token with its own credentials, rather than by a user signing in."""

    identifier = CLIENT_CREDENTIALS_EXTENSION


def _read_client_credentials(request: Request, form: typing.Any) -> tuple[str, str]:
    """The client id and secret a token request presents, by HTTP Basic or in the form body.

    RFC 6749 §2.3.1 lets a client use one method per request, so presenting both is refused.
    Basic credentials are percent-decoded as the SDK's own token endpoint does,
    so a raw ``+`` in a secret, as ``curl -u`` sends it, stays a ``+``.
    The scheme name is matched in any case, as RFC 9110 §11.1 requires.
    """
    scheme, _, credentials = request.headers.get('Authorization', '').partition(' ')
    form_secret = form.get('client_secret')
    if scheme.lower() == 'basic':
        if form_secret is not None:
            raise McpClientCredentialsError('invalid_request', 'Use one client authentication method, not two')
        try:
            decoded = base64.b64decode(credentials.strip(), validate=True).decode()
            client_id, client_secret = decoded.split(':', 1)
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise McpClientCredentialsError('invalid_client', 'Malformed Basic credentials', status_code=401) from exc
        form_client_id = form.get('client_id')
        client_id = urllib.parse.unquote(client_id)
        if form_client_id is not None and str(form_client_id) != client_id:
            raise McpClientCredentialsError('invalid_client', 'client_id does not match', status_code=401)
        return client_id, urllib.parse.unquote(client_secret)
    form_client_id = form.get('client_id')
    if form_client_id is None or form_secret is None:
        raise McpClientCredentialsError('invalid_client', 'Client authentication is required', status_code=401)
    return str(form_client_id), str(form_secret)
