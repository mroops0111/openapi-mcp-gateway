import collections.abc
import dataclasses
import secrets
import time
import typing

from mcp.server.auth.provider import construct_redirect_uri
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from ...scopes import McpAccessToken
from ...stores.base import TokenStore
from .client import mcp_registered_scopes
from .consent import PendingAuthorization


# The scope the gateway issues and demands when it is the authorization server.
# It has no upstream meaning, since the gateway mints these tokens itself,
# and exists only so the OAuth machinery has a scope to name.
MCP_DEFAULT_SCOPE = 'api'

# How long an MCP authorization code may wait to be redeemed, in seconds.
MCP_AUTHORIZATION_CODE_TTL = 300


@dataclasses.dataclass(frozen=True)
class McpTokenPolicy:
    """What the gateway grants on the ``mcp_...`` tokens it mints for MCP clients.

    ``scopes`` are the endpoint scopes every token carries, on top of what its client is granted.
    """

    scopes: list[str] = dataclasses.field(default_factory=lambda: [MCP_DEFAULT_SCOPE])
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 86400


@dataclasses.dataclass(frozen=True)
class McpTokenAttachment:
    """What one sign-in attaches to its MCP tokens, carried from the code to every token refreshed from it.

    ``upstream_scopes`` are what the upstream granted the user behind the tokens.
    ``links`` are the credentials the tokens map to, keyed by the store namespace each is found under.
    Both stay empty for a token no upstream sign-in stands behind.
    """

    upstream_scopes: list[str] = dataclasses.field(default_factory=list)
    links: collections.abc.Mapping[str, str] = dataclasses.field(default_factory=dict)


class McpTokenIssuer:
    """Mints, stores and reads back the MCP authorization codes and tokens of one server.

    ``issuer`` is the gateway's own issuer identifier for this server, the one its metadata publishes.
    Every record is stored with ``binding``,
    because one store serves every server in the gateway and outlives configuration changes.
    Without it a token minted for one server would unlock the upstream credential of the next,
    which is the cross-server credential reuse the MCP spec forbids.
    A record that carries another binding, or none at all, is treated as absent,
    so the client re-registers or re-authorizes rather than failing outright.
    """

    def __init__(
        self,
        store: TokenStore,
        issuer: str,
        policy: McpTokenPolicy,
        binding: collections.abc.Mapping[str, str],
    ) -> None:
        self.store = store
        self.issuer = issuer
        self.policy = policy
        self._binding = dict(binding)

    @property
    def binding(self) -> dict[str, str]:
        """The values that tie a record to this server, such as its issuer and the upstream it signs in to."""
        return dict(self._binding)

    def is_bound_here(self, data: dict[str, typing.Any] | None) -> typing.TypeGuard[dict[str, typing.Any]]:
        """Whether a stored record was written for this server under its current binding."""
        return bool(data) and data.get('binding') == self._binding

    async def complete_authorization(self, pending: PendingAuthorization, attachment: McpTokenAttachment) -> str:
        """Mint the MCP authorization code that ends ``pending``, and return the redirect carrying it to the client.

        Per RFC 9207 the redirect names this authorization server, as its metadata advertises,
        so an MCP client talking to several authorization servers can tell which one answered.
        """
        code = f'mcp_{secrets.token_hex(16)}'
        await self.store.set(
            'mcp_auth_code',
            code,
            {
                'code': code,
                'client_id': pending.client_id,
                'redirect_uri': pending.redirect_uri,
                'redirect_uri_provided_explicitly': pending.redirect_uri_provided_explicitly,
                'expires_at': time.time() + MCP_AUTHORIZATION_CODE_TTL,
                'scopes': pending.scopes,
                'code_challenge': pending.code_challenge,
                **self._attachment_fields(attachment),
            },
            ttl=MCP_AUTHORIZATION_CODE_TTL,
        )
        await self._link('mcp_auth_code', code, attachment, MCP_AUTHORIZATION_CODE_TTL)
        return construct_redirect_uri(pending.redirect_uri, code=code, state=pending.client_state, iss=self.issuer)

    async def load_attachment(self, namespace: str, key: str, data: dict[str, typing.Any]) -> McpTokenAttachment | None:
        """What the code or token stored under ``key`` carries, or ``None`` once a credential it links to is gone."""
        links: dict[str, str] = {}
        for link in data.get('links', []):
            value = await self.store.get_mapping(namespace, key, link)
            if not value:
                return None
            links[link] = value
        return McpTokenAttachment(upstream_scopes=list(data.get('upstream_scopes', [])), links=links)

    async def issue_tokens(
        self, client: OAuthClientInformationFull, scopes: list[str], attachment: McpTokenAttachment
    ) -> OAuthToken:
        """Mint MCP access and refresh tokens for ``client``, carrying ``attachment``.

        Every scope ``client`` may be granted is recorded as well, which is what the token can step up to.
        A client registered without any scope is taken to be limited to the ones it was granted.
        """
        client_id = require_client_id(client)
        access_token = f'mcp_{secrets.token_hex(32)}'
        refresh_token = f'mcp_refresh_{secrets.token_hex(32)}'
        now = int(time.time())
        record = {
            'client_id': client_id,
            'scopes': scopes,
            'mcp_grantable_scopes': mcp_registered_scopes(client) or list(scopes),
            **self._attachment_fields(attachment),
        }
        access_ttl = self.policy.access_token_ttl
        refresh_ttl = self.policy.refresh_token_ttl

        await self.store.set(
            'mcp_access_token',
            access_token,
            {'token': access_token, **record, 'expires_at': now + access_ttl},
            ttl=access_ttl,
        )
        await self.store.set(
            'mcp_refresh_token',
            refresh_token,
            {'token': refresh_token, **record, 'expires_at': now + refresh_ttl},
            ttl=refresh_ttl,
        )
        await self._link('mcp_access_token', access_token, attachment, access_ttl)
        await self._link('mcp_refresh_token', refresh_token, attachment, refresh_ttl)
        # Pair access and refresh in both directions for revoke lookup.
        await self.store.set_mapping(
            'mcp_access_token', access_token, 'mcp_refresh_token', refresh_token, ttl=access_ttl
        )
        await self.store.set_mapping(
            'mcp_refresh_token', refresh_token, 'mcp_access_token', access_token, ttl=refresh_ttl
        )

        # The granted scope is always stated, since RFC 6749 §5.1 requires it whenever it differs from the request.
        return OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=access_ttl,
            scope=' '.join(scopes) or None,
        )

    async def load_access_token(self, token: str) -> McpAccessToken | None:
        """The MCP access token stored under ``token``, or ``None`` when it is unknown here or expired."""
        data = await self.store.get('mcp_access_token', token)
        if not self.is_bound_here(data):
            return None
        return McpAccessToken(
            token=data['token'],
            client_id=data['client_id'],
            scopes=data['scopes'],
            expires_at=data.get('expires_at'),
            upstream_scopes=data.get('upstream_scopes', []),
            mcp_grantable_scopes=data.get('mcp_grantable_scopes'),
        )

    async def read_access_token_link(self, token: str, link: str) -> str | None:
        """The credential the MCP access token ``token`` links to under ``link``, or ``None`` for none."""
        return await self.store.get_mapping('mcp_access_token', token, link)

    async def revoke_access_token(self, token: str) -> None:
        """Delete the MCP access token ``token`` and the refresh token paired with it."""
        paired = await self.store.get_mapping('mcp_access_token', token, 'mcp_refresh_token')
        if paired:
            await self.store.delete('mcp_refresh_token', paired)
        await self.store.delete('mcp_access_token', token)

    async def revoke_refresh_token(self, token: str) -> None:
        """Delete the MCP refresh token ``token`` and the access token paired with it."""
        paired = await self.store.get_mapping('mcp_refresh_token', token, 'mcp_access_token')
        if paired:
            await self.store.delete('mcp_access_token', paired)
        await self.store.delete('mcp_refresh_token', token)

    def _attachment_fields(self, attachment: McpTokenAttachment) -> dict[str, typing.Any]:
        """The record fields every code and token carries besides its own."""
        return {
            'upstream_scopes': list(attachment.upstream_scopes),
            'links': sorted(attachment.links),
            'binding': self._binding,
        }

    async def _link(self, namespace: str, key: str, attachment: McpTokenAttachment, ttl: int) -> None:
        """Map the record under ``key`` to each credential ``attachment`` links, for as long as the record lives."""
        for link, value in attachment.links.items():
            await self.store.set_mapping(namespace, key, link, value, ttl=ttl)


def require_client_id(client: OAuthClientInformationFull) -> str:
    """The ``client_id`` of ``client``, which every client the gateway knows has."""
    if not client.client_id:
        raise ValueError('client_id is required')
    return client.client_id
