import dataclasses
import logging
import typing
import urllib.parse

import httpx
from starlette.exceptions import HTTPException


logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class UpstreamTokenResponse:
    """What the upstream token endpoint answered, with ``scope`` exactly as sent, or ``None`` when omitted."""

    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str | None

    def resolve_granted_scopes(self, previous: list[str]) -> list[str]:
        """The scopes the upstream granted, read as RFC 6749 §5.1 states them.

        An omitted ``scope`` means the grant did not change, so it is ``previous``,
        which for a first grant is what the gateway requested.
        """
        return self.scope.split() if self.scope is not None else list(previous)


@dataclasses.dataclass(frozen=True)
class UpstreamAuthorizationCodeClient:
    """The gateway as an OAuth client of the upstream authorization server, in the ``authorization_code`` grant.

    ``callback_url`` is the gateway's own address,
    and belongs here because it is only ever meaningful as the ``redirect_uri`` of this conversation.

    ``scope_descriptions`` maps each upstream scope to what it allows, as the spec's OAuth flow states it.

    ``audience_params`` names the API the token is for,
    for an upstream whose API and authorization server are different parties.

    ``issuer`` is the upstream authorization server's RFC 8414 issuer identifier.
    It is what an RFC 9207 ``iss`` on the authorization response is compared against,
    so without it a present ``iss`` cannot be checked at all.
    """

    authorization_url: str
    token_url: str
    client_id: str
    client_secret: str
    callback_url: str
    scopes: list[str] = dataclasses.field(default_factory=list)
    scope_descriptions: dict[str, str] = dataclasses.field(default_factory=dict)
    audience_params: dict[str, str] = dataclasses.field(default_factory=dict)
    issuer: str | None = None

    @property
    def authorization_server(self) -> str:
        """Identify the upstream authorization server that persisted upstream tokens belong to.

        The issuer when one is configured, since that is the identifier the MCP spec keys credentials by.
        Otherwise the token endpoint, the one place every stored upstream token is redeemed or refreshed,
        so that pointing the gateway at another server still invalidates what the old one issued.
        """
        return self.issuer or self.token_url

    @property
    def host(self) -> str:
        """Where the user signs in, as a consent page names it."""
        return urllib.parse.urlsplit(self.authorization_url).netloc

    def build_authorize_url(self, state: str) -> str:
        """The upstream authorize URL for a sign-in under ``state``."""
        query_params = {
            'client_id': self.client_id,
            'redirect_uri': self.callback_url,
            'state': state,
            'response_type': 'code',
        }
        if self.scopes:
            query_params['scope'] = ' '.join(self.scopes)
        # Sent on the authorization request as well as the token request,
        # since an authorization server binds the audience at consent time,
        # and RFC 8707 §2 requires the parameter on both.
        query_params.update(self.audience_params)

        logger.info('Upstream OAuth authorize: scopes=%s audience_params=%s', self.scopes, self.audience_params)
        return f'{self.authorization_url}?{urllib.parse.urlencode(query_params)}'

    def validate_issuer(self, iss: str | None) -> None:
        """Apply RFC 9207 to the upstream's authorization response, before its code is redeemed.

        A present ``iss`` must equal ``issuer``, compared exactly as the RFC requires.
        A sign-in started against another issuer never reaches here,
        since it was bound to the authorization server it was sent to.
        """
        if iss is None:
            return
        if not self.issuer:
            logger.warning(
                'Upstream authorization response carries iss=%s but auth.upstream.issuer is not set, '
                'so it cannot be checked. Set auth.upstream.issuer to enable RFC 9207 mix-up protection.',
                iss,
            )
            return
        if iss != self.issuer:
            logger.warning('OAuth callback rejected: reason=iss_mismatch iss=%s expected=%s', iss, self.issuer)
            raise HTTPException(400, 'Authorization response rejected: iss does not match the upstream issuer')

    async def exchange_code(self, code: str) -> UpstreamTokenResponse:
        """Redeem the upstream authorization ``code`` at the token endpoint."""
        return await self._request_token(
            {
                'client_id': self.client_id,
                'client_secret': self.client_secret,
                'code': code,
                'redirect_uri': self.callback_url,
                'grant_type': 'authorization_code',
            }
        )

    async def refresh(self, refresh_token: str) -> UpstreamTokenResponse:
        """Trade ``refresh_token`` for a new upstream access token."""
        return await self._request_token(
            {
                'client_id': self.client_id,
                'client_secret': self.client_secret,
                'refresh_token': refresh_token,
                'grant_type': 'refresh_token',
            }
        )

    async def _request_token(self, request_data: dict[str, typing.Any]) -> UpstreamTokenResponse:
        """POST ``request_data`` to ``token_url``.

        Audience parameters are added here rather than at each call site,
        so the initial exchange and every later refresh stay bound to the same API.
        Dropping them on refresh would return a token the upstream refuses,
        and that failure would only surface once the first token expired.

        Raises ``HTTPException`` when the upstream rejects the exchange.
        """
        # Grant fields last, so a stray audience key can never displace ``grant_type`` or the credentials.
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.token_url,
                data={**self.audience_params, **request_data},
                headers={'Accept': 'application/json'},
            )

            if response.status_code != 200:
                logger.warning(
                    'Upstream token exchange failed: status=%d url=%s',
                    response.status_code,
                    self.token_url,
                )
                raise HTTPException(400, f'Upstream token exchange failed: {response.text}')

            data = response.json()
            access_token = data.get('access_token')
            if not access_token:
                logger.warning('Upstream token exchange returned no access_token: url=%s', self.token_url)
                raise HTTPException(400, 'Upstream returned no access_token')

            logger.info(
                'Upstream token response: granted_scope=%r expires_in=%s', data.get('scope'), data.get('expires_in')
            )
            return UpstreamTokenResponse(
                access_token=access_token,
                refresh_token=data.get('refresh_token'),
                expires_in=data.get('expires_in', 3600),
                scope=data.get('scope'),
            )
