import base64
import contextlib
import threading
import time
import typing
import urllib.parse

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tests.serving import bind_loopback_socket, build_socket_url


# The RFC 8693 grant the gateway exchanges a caller's token with, and the token types it names.
TOKEN_EXCHANGE_GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:token-exchange'
ACCESS_TOKEN_TYPE = 'urn:ietf:params:oauth:token-type:access_token'

# The gateway's own credential at the issuer, which it exchanges tokens with.
GATEWAY_CLIENT_ID = 'gateway'
GATEWAY_CLIENT_SECRET = 'gateway-secret'

# The key id the issuer signs every token under.
_KEY_ID = 'fake-idp-key'


class FakeIdentityProvider:
    """An authorization server the gateway does not own, as an organisation's IdP would be.

    It mints MCP tokens to agents through ``client_credentials``, granting each agent at most ``grantable[agent]``,
    and exchanges them for upstream tokens through RFC 8693, granting at most ``upstream_grantable``.
    A request beyond what may be granted is refused with ``invalid_scope``, as an issuer declining a step-up does.
    Every exchange request is kept in ``exchanges`` for a test to read.
    """

    def __init__(self, signing_key: rsa.RSAPrivateKey, secrets: dict[str, str], grantable: dict[str, set[str]]):
        self.issuer = ''
        self.secrets = secrets
        self.grantable = grantable
        self.upstream_grantable: set[str] | None = None
        self.exchanges: list[dict[str, str]] = []
        self._signing_key = signing_key
        self._exchanged_count = 0

    @property
    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route('/.well-known/openid-configuration', self._metadata),
                Route('/.well-known/oauth-authorization-server', self._metadata),
                Route('/jwks', self._jwks),
                Route('/token', self._token, methods=['POST']),
            ]
        )

    async def _metadata(self, request: Request) -> JSONResponse:
        return JSONResponse(
            {
                'issuer': self.issuer,
                # The SDK's metadata model requires one, though no user ever signs in here.
                'authorization_endpoint': f'{self.issuer}/authorize',
                'jwks_uri': f'{self.issuer}/jwks',
                'token_endpoint': f'{self.issuer}/token',
                'grant_types_supported': ['client_credentials', TOKEN_EXCHANGE_GRANT_TYPE],
                'token_endpoint_auth_methods_supported': ['client_secret_basic', 'client_secret_post'],
                'response_types_supported': [],
            }
        )

    async def _jwks(self, request: Request) -> JSONResponse:
        jwk = RSAAlgorithm.to_jwk(self._signing_key.public_key(), as_dict=True)
        return JSONResponse({'keys': [{**jwk, 'kid': _KEY_ID, 'alg': 'RS256', 'use': 'sig'}]})

    async def _token(self, request: Request) -> JSONResponse:
        form = {key: str(value) for key, value in (await request.form()).items()}
        client_id, client_secret = _client_credentials_of(request, form)
        if self.secrets.get(client_id) != client_secret:
            return JSONResponse({'error': 'invalid_client'}, status_code=401)
        if form.get('grant_type') == 'client_credentials':
            return self._issue_mcp_token(client_id, form)
        if form.get('grant_type') == TOKEN_EXCHANGE_GRANT_TYPE:
            return self._exchange(form)
        return JSONResponse({'error': 'unsupported_grant_type'}, status_code=400)

    def _issue_mcp_token(self, client_id: str, form: dict[str, str]) -> JSONResponse:
        requested = set(form.get('scope', '').split())
        if not requested <= self.grantable.get(client_id, set()):
            return JSONResponse({'error': 'invalid_scope'}, status_code=400)
        claims = {
            'iss': self.issuer,
            'sub': client_id,
            'aud': form.get('resource', ''),
            'client_id': client_id,
            'scope': ' '.join(sorted(requested)),
            'exp': int(time.time()) + 3600,
        }
        token = jwt.encode(claims, self._signing_key, algorithm='RS256', headers={'kid': _KEY_ID})
        return JSONResponse(
            {'access_token': token, 'token_type': 'Bearer', 'expires_in': 3600, 'scope': claims['scope']}
        )

    def _exchange(self, form: dict[str, str]) -> JSONResponse:
        self.exchanges.append(form)
        requested = form.get('scope', '').split()
        granted = [scope for scope in requested if self.upstream_grantable is None or scope in self.upstream_grantable]
        self._exchanged_count += 1
        return JSONResponse(
            {
                'access_token': f'upstream-token-{self._exchanged_count}',
                'issued_token_type': ACCESS_TOKEN_TYPE,
                'token_type': 'Bearer',
                'expires_in': 3600,
                'scope': ' '.join(granted),
            }
        )


@contextlib.contextmanager
def serve_identity_provider(identity_provider: FakeIdentityProvider) -> typing.Iterator[str]:
    """Serve ``identity_provider`` on a thread of its own, and yield its issuer.

    The gateway reads the issuer's metadata and signing keys with blocking calls,
    so the issuer cannot share the event loop the gateway serves on.
    """
    sock = bind_loopback_socket()
    identity_provider.issuer = build_socket_url(sock)
    server = uvicorn.Server(uvicorn.Config(identity_provider.app, log_config=None, ws='none'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError('the fake identity provider failed to start')
        time.sleep(0.01)
    try:
        yield identity_provider.issuer
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _client_credentials_of(request: Request, form: dict[str, str]) -> tuple[str, str]:
    """The client id and secret a token request presents, by HTTP Basic or in the form."""
    header = request.headers.get('authorization', '')
    if header.startswith('Basic '):
        client_id, _, client_secret = base64.b64decode(header.removeprefix('Basic ')).decode().partition(':')
        return urllib.parse.unquote(client_id), urllib.parse.unquote(client_secret)
    return form.get('client_id', ''), form.get('client_secret', '')
