import collections.abc
import dataclasses
import secrets
import time
import typing

import pydantic
from mcp.server.auth.provider import construct_redirect_uri
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from ...scopes import McpAccessToken
from ...stores.base import TokenStore
from .client import get_mcp_registered_scopes
from .consent import PendingAuthorization
from .record_origin import McpRecordOrigin


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
    ``links`` are the credentials the tokens map to, keyed by a name the attaching side chooses.
    ``links_expire_at`` is when the first of those credentials stops working, as a Unix time.
    An access token never outlives it, so the client refreshes before its calls start failing.
    All stay empty for a token no upstream sign-in stands behind.
    """

    upstream_scopes: list[str] = dataclasses.field(default_factory=list)
    links: collections.abc.Mapping[str, str] = dataclasses.field(default_factory=dict)
    links_expire_at: int | None = None


class McpTokenIssuer:
    """Mints, stores and reads back the MCP authorization codes and tokens of one server.

    ``origin.issuer`` is the gateway's own issuer identifier for this server, the one its metadata publishes.
    Every record is stored with ``origin``, and one read elsewhere is treated as absent,
    so the client re-registers or re-authorizes rather than failing outright.
    ``resource`` is the canonical URI of the server's MCP endpoint.
    Every code and token records it as the RFC 8707 resource it was issued for,
    which the SDK compares against the endpoint on every request.
    """

    def __init__(self, store: TokenStore, origin: McpRecordOrigin, policy: McpTokenPolicy, resource: str) -> None:
        self.store = store
        self.origin = origin
        self.policy = policy
        self.resource = resource
        # Compared on every authenticated request, so serialized once.
        self._origin_fields = origin.model_dump()

    @property
    def issuer(self) -> str:
        return self.origin.issuer

    def is_from_here(self, data: dict[str, typing.Any] | None) -> typing.TypeGuard[dict[str, typing.Any]]:
        """Whether a stored record was written by this server against its current upstream."""
        return bool(data) and data.get('origin') == self._origin_fields

    def serves_resource(self, requested: str | None) -> bool:
        """Whether a client asking for ``requested`` as its RFC 8707 ``resource`` may get a token here.

        A client that names none gets one for this endpoint, the only resource it serves.
        """
        return requested is None or requested.rstrip('/') == self.resource.rstrip('/')

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
                **self._build_record_fields(attachment),
            },
            ttl=MCP_AUTHORIZATION_CODE_TTL,
        )
        await self._store_links('mcp_auth_code', code, attachment, MCP_AUTHORIZATION_CODE_TTL)
        return construct_redirect_uri(pending.redirect_uri, code=code, state=pending.client_state, iss=self.issuer)

    async def load_pending(self, namespace: str, key: str) -> PendingAuthorization | None:
        """The sign-in stored under ``key`` in ``namespace`` by this server, or ``None`` for none or another's.

        A record of an older shape, such as one from before sign-ins were tied to a browser, counts as none.
        """
        data = await self.store.get(namespace, key)
        if not self.is_from_here(data):
            return None
        try:
            return PendingAuthorization.model_validate(data)
        except pydantic.ValidationError:
            return None

    async def load_attachment(self, namespace: str, key: str, data: dict[str, typing.Any]) -> McpTokenAttachment | None:
        """What the code or token stored under ``key`` carries, or ``None`` once a credential it links to is gone."""
        links: dict[str, str] = {}
        for link in data.get('links', []):
            value = await self.store.get_mapping(namespace, key, link)
            if not value:
                return None
            links[link] = value
        return McpTokenAttachment(
            upstream_scopes=list(data.get('upstream_scopes', [])),
            links=links,
            links_expire_at=data.get('links_expire_at'),
        )

    async def issue_tokens(
        self, client: OAuthClientInformationFull, scopes: list[str], attachment: McpTokenAttachment
    ) -> OAuthToken:
        """Mint an MCP access token and a refresh token paired with it for ``client``, carrying ``attachment``."""
        token = await self.issue_access_token(client, scopes, attachment)
        refresh_token = f'mcp_refresh_{secrets.token_hex(32)}'
        refresh_ttl = self.policy.refresh_token_ttl
        await self.store.set(
            'mcp_refresh_token',
            refresh_token,
            {
                'token': refresh_token,
                **self._build_token_record(client, scopes, attachment),
                'expires_at': int(time.time()) + refresh_ttl,
            },
            ttl=refresh_ttl,
        )
        await self._store_links('mcp_refresh_token', refresh_token, attachment, refresh_ttl)
        # Pair access and refresh in both directions for revoke lookup.
        await self.store.set_mapping(
            'mcp_access_token', token.access_token, 'mcp_refresh_token', refresh_token, ttl=token.expires_in
        )
        await self.store.set_mapping(
            'mcp_refresh_token', refresh_token, 'mcp_access_token', token.access_token, ttl=refresh_ttl
        )
        return token.model_copy(update={'refresh_token': refresh_token})

    async def issue_access_token(
        self, client: OAuthClientInformationFull, scopes: list[str], attachment: McpTokenAttachment
    ) -> OAuthToken:
        """Mint an MCP access token for ``client``, carrying ``attachment``, with no refresh token.

        Every scope ``client`` may be granted is recorded as well, which is what the token can step up to.
        A client registered without any scope is taken to be limited to the ones it was granted.
        The token never outlives what ``attachment`` links.
        """
        access_token = f'mcp_{secrets.token_hex(32)}'
        now = int(time.time())
        access_ttl = self.policy.access_token_ttl
        if attachment.links_expire_at is not None:
            access_ttl = max(1, min(access_ttl, attachment.links_expire_at - now))
        await self.store.set(
            'mcp_access_token',
            access_token,
            {
                'token': access_token,
                **self._build_token_record(client, scopes, attachment),
                'expires_at': now + access_ttl,
            },
            ttl=access_ttl,
        )
        await self._store_links('mcp_access_token', access_token, attachment, access_ttl)
        # The granted scope is always stated, since RFC 6749 §5.1 requires it whenever it differs from the request.
        return OAuthToken(access_token=access_token, expires_in=access_ttl, scope=' '.join(scopes) or None)

    async def load_access_token(self, token: str) -> McpAccessToken | None:
        """The MCP access token stored under ``token``, or ``None`` when it is unknown here or expired."""
        data = await self.store.get('mcp_access_token', token)
        if not self.is_from_here(data):
            return None
        return McpAccessToken(
            token=data['token'],
            client_id=data['client_id'],
            scopes=data['scopes'],
            expires_at=data.get('expires_at'),
            resource=data.get('resource'),
            upstream_scopes=data.get('upstream_scopes', []),
            mcp_grantable_scopes=data.get('mcp_grantable_scopes'),
        )

    async def load_access_token_link(self, token: str, link: str) -> str | None:
        """The credential the MCP access token ``token`` links to under ``link``, or ``None`` for none."""
        return await self.store.get_mapping('mcp_access_token', token, link)

    async def redeem_authorization_code(
        self, client_id: str, code: str
    ) -> tuple[dict[str, typing.Any], McpTokenAttachment | None] | None:
        """Take the MCP authorization code ``code`` out of the store, with what it carries.

        The code is removed in the same step it is read, so two exchanges racing for one code never both succeed,
        as OAuth 2.1 requires of a code.
        ``None`` when the code is unknown here or belongs to another client.
        The attachment is ``None`` once a credential the code links is gone.
        """
        data = await self.store.pop('mcp_auth_code', code)
        if not self.is_from_here(data) or data['client_id'] != client_id:
            return None
        attachment = await self.load_attachment('mcp_auth_code', code, data)
        await self._delete_links('mcp_auth_code', code, data)
        return data, attachment

    async def revoke_access_token(self, token: str) -> None:
        """Delete the MCP access token ``token`` and the refresh token paired with it, links included."""
        await self._revoke_pair('mcp_access_token', token, 'mcp_refresh_token')

    async def revoke_refresh_token(self, token: str) -> None:
        """Delete the MCP refresh token ``token`` and the access token paired with it, links included."""
        await self._revoke_pair('mcp_refresh_token', token, 'mcp_access_token')

    async def _revoke_pair(self, namespace: str, token: str, paired_namespace: str) -> None:
        """Delete ``token`` and the token paired with it, so no credential either links stays at rest."""
        paired = await self.store.get_mapping(namespace, token, paired_namespace)
        await self._delete_token(namespace, token, paired_namespace)
        if paired:
            await self._delete_token(paired_namespace, paired, namespace)

    async def _delete_token(self, namespace: str, token: str, paired_namespace: str) -> None:
        """Delete one token record with its links and its mapping to the token paired with it."""
        data = await self.store.pop(namespace, token)
        if data:
            await self._delete_links(namespace, token, data)
        await self.store.delete_mapping(namespace, token, paired_namespace)

    async def _delete_links(self, namespace: str, key: str, data: dict[str, typing.Any]) -> None:
        """Delete the mappings from the record under ``key`` to the credentials it links."""
        for link in data.get('links', []):
            await self.store.delete_mapping(namespace, key, link)

    def _build_token_record(
        self, client: OAuthClientInformationFull, scopes: list[str], attachment: McpTokenAttachment
    ) -> dict[str, typing.Any]:
        """The fields an access or refresh token record carries besides its own value and expiry."""
        return {
            'client_id': require_client_id(client),
            'scopes': scopes,
            'mcp_grantable_scopes': get_mcp_registered_scopes(client) or list(scopes),
            **self._build_record_fields(attachment),
        }

    def _build_record_fields(self, attachment: McpTokenAttachment) -> dict[str, typing.Any]:
        """The record fields every code and token carries besides its own."""
        return {
            'upstream_scopes': list(attachment.upstream_scopes),
            'links': sorted(attachment.links),
            'links_expire_at': attachment.links_expire_at,
            'resource': self.resource,
            'origin': self._origin_fields,
        }

    async def _store_links(self, namespace: str, key: str, attachment: McpTokenAttachment, ttl: int) -> None:
        """Map the record under ``key`` to each credential ``attachment`` links, for as long as the record lives."""
        for link, value in attachment.links.items():
            await self.store.set_mapping(namespace, key, link, value, ttl=ttl)


def require_client_id(client: OAuthClientInformationFull) -> str:
    """The ``client_id`` of ``client``, which every client the gateway knows has."""
    if not client.client_id:
        raise ValueError('client_id is required')
    return client.client_id
