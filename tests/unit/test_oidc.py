import time
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from openapi_mcp_gateway.auth.oidc import (
    JWKSTokenVerifier,
    OIDCConfigurationError,
    TokenVerificationError,
    fetch_issuer_metadata,
)


ISSUER = 'https://auth.example.com'
GATEWAY_RESOURCE = 'https://gw.example.com/braid/mcp'


@pytest.fixture(scope='module')
def signing_key():
    """One RSA keypair reused across tests, since generation is the slow part."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _metadata_response(payload: dict | None, status_code: int = 200) -> MagicMock:
    """Mock an issuer metadata endpoint response."""
    response = MagicMock()
    response.status_code = status_code
    if payload is None:
        response.json.side_effect = ValueError('not json')
    else:
        response.json.return_value = payload
    return response


def _issue(signing_key, **claims) -> str:
    """Sign a JWT with sensible defaults for every registered claim the verifier requires."""
    payload = {
        'iss': ISSUER,
        'aud': GATEWAY_RESOURCE,
        'sub': 'user-123',
        'exp': int(time.time()) + 300,
        **claims,
    }
    return jwt.encode(payload, signing_key, algorithm='RS256')


def _verifier(signing_key, **kwargs) -> JWKSTokenVerifier:
    """Build a verifier whose JWKS client resolves to ``signing_key``'s public half."""
    with patch('openapi_mcp_gateway.auth.oidc._build_jwk_client') as build:
        jwk = MagicMock()
        jwk.key = signing_key.public_key()
        jwk.key_type = 'RSA'
        build.return_value.get_signing_key_from_jwt.return_value = jwk
        return JWKSTokenVerifier(
            issuer=ISSUER,
            audience=GATEWAY_RESOURCE,
            jwks_uri=f'{ISSUER}/jwks',
            **kwargs,
        )


class TestFetchIssuerMetadata:
    """Discovery tries both well-known documents and refuses one that names another issuer."""

    def test_prefers_openid_configuration(self):
        """OpenID Connect discovery is tried first, so a provider serving both is read once."""
        payload = {'issuer': ISSUER, 'jwks_uri': f'{ISSUER}/jwks', 'token_endpoint': f'{ISSUER}/token'}
        with patch('httpx.get', return_value=_metadata_response(payload)) as get:
            metadata = fetch_issuer_metadata(ISSUER)

        assert metadata.jwks_uri == f'{ISSUER}/jwks'
        assert metadata.token_endpoint == f'{ISSUER}/token'
        assert get.call_args_list[0].args[0] == f'{ISSUER}/.well-known/openid-configuration'

    def test_falls_back_to_oauth_authorization_server(self):
        """A plain OAuth server serving only RFC 8414 is still discovered."""
        payload = {'issuer': ISSUER, 'jwks_uri': f'{ISSUER}/jwks', 'token_endpoint': f'{ISSUER}/token'}
        responses = [_metadata_response(None, status_code=404), _metadata_response(payload)]
        with patch('httpx.get', side_effect=responses) as get:
            metadata = fetch_issuer_metadata(ISSUER)

        assert metadata.jwks_uri == f'{ISSUER}/jwks'
        assert get.call_args_list[1].args[0] == f'{ISSUER}/.well-known/oauth-authorization-server'

    def test_rejects_document_declaring_another_issuer(self):
        """A document whose ``issuer`` differs is refused, per RFC 8414 §3.3.

        Trusting it would let a redirect substitute one authorization server's keys for another's.
        """
        payload = {'issuer': 'https://evil.example.com', 'jwks_uri': 'https://evil.example.com/jwks'}
        with (
            patch('httpx.get', return_value=_metadata_response(payload)),
            pytest.raises(OIDCConfigurationError, match='declares issuer'),
        ):
            fetch_issuer_metadata(ISSUER)

    def test_reports_every_attempt_when_discovery_fails(self):
        """The error names both URLs tried, so a misconfigured issuer is diagnosable."""
        with (
            patch('httpx.get', return_value=_metadata_response(None, status_code=404)),
            pytest.raises(OIDCConfigurationError) as excinfo,
        ):
            fetch_issuer_metadata(ISSUER)

        assert '.well-known/openid-configuration' in str(excinfo.value)
        assert '.well-known/oauth-authorization-server' in str(excinfo.value)


class TestJWKSTokenVerifier:
    """Inbound tokens are accepted only when minted by the issuer for this MCP endpoint.

    Every rejection reports as ``None``, the only refusal the SDK's bearer backend understands,
    while the reason stays reachable on the inner method for logging and for these tests.
    """

    async def _assert_rejected(self, verifier, token, reason_matches):
        """A token must both report as unacceptable and carry a diagnosable reason."""
        assert await verifier.verify_token(token) is None
        with pytest.raises(TokenVerificationError, match=reason_matches):
            verifier._verified_access_token(token)

    async def test_accepts_token_for_this_resource(self, signing_key):
        """A well-formed token yields an ``AccessToken`` carrying the caller's subject."""
        verifier = _verifier(signing_key)

        result = await verifier.verify_token(_issue(signing_key, scope='read write'))

        assert result is not None
        assert result.subject == 'user-123'
        assert result.scopes == ['read', 'write']
        assert result.resource == GATEWAY_RESOURCE

    async def test_non_jwt_returns_none(self, signing_key):
        """An opaque credential is not this verifier's to judge, so it reports no opinion.

        Returning ``None`` rather than raising is what keeps "unrecognised" distinct from "rejected".
        """
        verifier = _verifier(signing_key)

        assert await verifier.verify_token('opaque-session-token') is None

    async def test_token_for_the_upstream_is_rejected(self, signing_key):
        """A token minted for the API behind the gateway is refused.

        The MCP spec requires a server to accept only tokens naming itself,
        which is why the upstream is reached with a separately exchanged token.
        """
        verifier = _verifier(signing_key)
        token = _issue(signing_key, aud='https://braid.example.com')

        await self._assert_rejected(verifier, token, 'audience does not match')

    async def test_expired_token_reports_as_expired(self, signing_key):
        """Expiry is reported distinctly rather than as a generic invalid token.

        It is also the common rejection under this flow, since the issuer sets the lifetimes
        and five minutes is a widespread default.
        """
        verifier = _verifier(signing_key)
        token = _issue(signing_key, exp=int(time.time()) - 10)

        await self._assert_rejected(verifier, token, 'expired')

    async def test_token_from_another_issuer_is_rejected(self, signing_key):
        """An issuer mismatch is refused even when the signature verifies."""
        verifier = _verifier(signing_key)
        token = _issue(signing_key, iss='https://other.example.com')

        await self._assert_rejected(verifier, token, 'not issued by')

    async def test_missing_required_scope_is_rejected(self, signing_key):
        """Scopes the deployment requires are enforced at verification time."""
        verifier = _verifier(signing_key, required_scopes=['admin'])
        token = _issue(signing_key, scope='read')

        await self._assert_rejected(verifier, token, 'missing required scopes')
