"""Discovery and token verification for an authorization server the gateway does not own.

Used by the ``token_exchange`` flow, where the gateway issues no credentials of its own.
It validates tokens minted for it by an external issuer,
then exchanges each one for a separate upstream token under RFC 8693.

Two documents are fetched, both from the issuer:
its RFC 8414 / OpenID Connect metadata names the JWKS and token endpoints,
and the JWKS supplies the signing keys.
"""

import dataclasses
import logging
import typing

import httpx


logger = logging.getLogger(__name__)


# OpenID Connect Discovery 1.0 §4, then RFC 8414 §3.
# Both are tried because an OpenID provider serves the former and a plain OAuth server the latter,
# and a deployment should not have to tell the gateway which kind it runs.
ISSUER_METADATA_PATHS = ('/.well-known/openid-configuration', '/.well-known/oauth-authorization-server')

# Verification is restricted to the family of the key the issuer published,
# rather than to whatever the token's own header asks for.
# That is what closes the algorithm-confusion hole,
# where a token asks for an RSA public key to be read as an HMAC secret.
_ALGORITHMS_BY_KEY_TYPE = {
    'RSA': ['RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512'],
    'EC': ['ES256', 'ES384', 'ES512', 'ES256K'],
    'OKP': ['EdDSA'],
}


class OIDCConfigurationError(Exception):
    """Raised when discovery cannot produce a usable JWKS or token endpoint."""


class TokenVerificationError(Exception):
    """Raised when a bearer token is a JWT but is not acceptable.

    Distinct from returning ``None``, which means the credential is not a JWT at all.
    Keeping the two apart is what lets an expired token report as expired,
    rather than degrading into an indistinguishable "invalid token".
    """


@dataclasses.dataclass(frozen=True)
class IssuerMetadata:
    """The subset of an authorization server's metadata the gateway acts on."""

    issuer: str
    jwks_uri: str
    token_endpoint: str | None = None


def fetch_issuer_metadata(issuer: str, timeout: float = 10.0) -> IssuerMetadata:
    """Return the issuer's JWKS and token endpoints, trying OpenID Connect then RFC 8414.

    The ``issuer`` claim in the document must equal the issuer asked for.
    RFC 8414 §3.3 requires that check,
    and skipping it would let a redirect substitute one authorization server's keys for another's.
    """
    expected = issuer.rstrip('/')
    errors: list[str] = []

    for path in ISSUER_METADATA_PATHS:
        url = f'{expected}{path}'
        try:
            response = httpx.get(url, timeout=timeout, follow_redirects=True)
        except httpx.HTTPError as exc:
            errors.append(f'{url}: {exc}')
            continue

        if response.status_code != 200:
            errors.append(f'{url}: HTTP {response.status_code}')
            continue

        try:
            payload = response.json()
        except ValueError:
            errors.append(f'{url}: response is not JSON')
            continue

        declared = str(payload.get('issuer', '')).rstrip('/')
        if declared != expected:
            raise OIDCConfigurationError(
                f'Issuer metadata at {url} declares issuer "{declared}", expected "{expected}". '
                'Refusing to trust it, since the document does not belong to the configured issuer.'
            )

        jwks_uri = payload.get('jwks_uri')
        if not isinstance(jwks_uri, str) or not jwks_uri:
            errors.append(f'{url}: no jwks_uri in metadata')
            continue

        token_endpoint = payload.get('token_endpoint')
        logger.info(
            'Discovered issuer metadata: issuer=%s jwks_uri=%s token_endpoint=%s',
            expected,
            jwks_uri,
            token_endpoint,
        )
        return IssuerMetadata(
            issuer=expected,
            jwks_uri=jwks_uri,
            token_endpoint=token_endpoint if isinstance(token_endpoint, str) else None,
        )

    raise OIDCConfigurationError(f'Could not discover metadata for issuer "{issuer}". Tried:\n  ' + '\n  '.join(errors))


class JWKSTokenVerifier:
    """Verify inbound bearer tokens as JWTs signed by an external issuer.

    Implements the MCP SDK's ``TokenVerifier`` protocol.
    ``PyJWKClient`` caches the issuer's signing keys and refetches on an unknown ``kid``, so rotation needs no restart.

    ``audience`` is the gateway's own canonical URI, never the upstream API's.
    The MCP spec requires a server to accept only tokens minted for itself,
    which is why the upstream is reached with a separately exchanged token rather than this one.
    """

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_uri: str,
        required_scopes: typing.Sequence[str] = (),
    ) -> None:
        self.issuer = issuer.rstrip('/')
        self.audience = audience
        self.jwks_uri = jwks_uri
        self.required_scopes = tuple(required_scopes)
        self._jwk_client = _build_jwk_client(jwks_uri)

    async def verify_token(self, token: str) -> typing.Any | None:
        """Return an ``AccessToken`` for a valid JWT, or ``None`` when the credential is not a JWT."""
        import jwt
        from mcp.server.auth.provider import AccessToken

        # A JWT has three dot-separated segments.
        # Anything else is some other credential scheme,
        # and reporting it as invalid would misdescribe what went wrong.
        if token.count('.') != 2:
            return None

        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=_permitted_algorithms(signing_key),
                issuer=self.issuer,
                audience=self.audience,
                options={'require': ['exp', 'iss', 'aud']},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenVerificationError('Access token has expired') from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenVerificationError(
                f'Access token audience does not match "{self.audience}". '
                'The token must be requested for this MCP endpoint, not for the API behind it.'
            ) from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenVerificationError(f'Access token was not issued by "{self.issuer}"') from exc
        except jwt.PyJWTError as exc:
            raise TokenVerificationError(f'Access token is not valid: {exc}') from exc

        scopes = _extract_scopes(claims)
        missing = [scope for scope in self.required_scopes if scope not in scopes]
        if missing:
            raise TokenVerificationError(f'Access token is missing required scopes: {", ".join(missing)}')

        expires_at = claims.get('exp')
        subject = claims.get('sub')
        return AccessToken(
            token=token,
            client_id=str(claims.get('azp') or claims.get('client_id') or subject or 'unknown'),
            scopes=list(scopes),
            expires_at=int(expires_at) if expires_at is not None else None,
            resource=self.audience,
            subject=str(subject) if subject is not None else None,
        )


def _build_jwk_client(jwks_uri: str) -> typing.Any:
    """Construct a ``PyJWKClient``, turning a missing dependency into actionable guidance."""
    try:
        from jwt import PyJWKClient
    except ImportError as exc:  # pragma: no cover - only reachable without the extra installed
        raise OIDCConfigurationError(
            'auth.flow "token_exchange" needs the "oidc" extra for JWT signature verification. '
            'Install it with: pip install "openapi-mcp-gateway[oidc]" '
            '(or uvx --from "openapi-mcp-gateway[oidc]" openapi-mcp-gateway).'
        ) from exc

    return PyJWKClient(jwks_uri, cache_keys=True)


def _permitted_algorithms(signing_key: typing.Any) -> list[str]:
    """Return the algorithms acceptable for ``signing_key``, per ``_ALGORITHMS_BY_KEY_TYPE``.

    Symmetric keys are refused outright, since a JWKS has no business publishing one.
    """
    key_type = getattr(signing_key, 'key_type', None)
    algorithms = _ALGORITHMS_BY_KEY_TYPE.get(str(key_type))
    if not algorithms:
        raise TokenVerificationError(
            f'Issuer published an unusable signing key type "{key_type}" for this token. '
            'Only asymmetric keys (RSA, EC, OKP) are accepted.'
        )
    return algorithms


def _extract_scopes(claims: dict[str, typing.Any]) -> tuple[str, ...]:
    """Read scopes from ``scope`` (space-delimited) or ``scp`` (list or string, as Entra emits)."""
    raw = claims.get('scope') or claims.get('scp') or ''
    if isinstance(raw, list):
        return tuple(str(scope) for scope in raw)
    return tuple(str(raw).split())
