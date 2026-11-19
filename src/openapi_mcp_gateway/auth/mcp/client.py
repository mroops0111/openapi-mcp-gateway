import abc
import collections.abc
import ipaddress
import logging

import pydantic
from mcp.server.auth.provider import RegistrationError
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import InvalidRedirectUriError, InvalidScopeError, OAuthClientInformationFull
from pydantic import AnyUrl

from ...stores.base import TokenStore


logger = logging.getLogger(__name__)


# The host names RFC 8252 §7.3 treats as loopback besides the IP literals.
_LOOPBACK_NAMES = frozenset({'localhost'})


class McpClientDirectory(abc.ABC):
    """Where the gateway's authorization server finds the MCP clients allowed to sign in.

    Only a directory that accepts Dynamic Client Registration overrides ``register``,
    so the default refuses it, and the matching registration options turn the endpoint off.
    """

    @abc.abstractmethod
    async def get(self, client_id: str) -> OAuthClientInformationFull | None:
        """Return the client registered under ``client_id``, or ``None`` for one this server does not know."""

    async def register(self, client_info: OAuthClientInformationFull) -> None:
        """Record a dynamically registered client, which this directory refuses."""
        raise RegistrationError(
            error='invalid_client_metadata',
            error_description='This server accepts only pre-registered clients.',
        )

    def requires_consent(self, client_id: str) -> bool:
        """Whether a sign-in by ``client_id`` passes the gateway's consent page, which every client does by default.

        A client that registered itself is vouched for by no one, so only an operator can exempt one.
        """
        return True

    def build_registration_options(
        self, mcp_endpoint_scopes: collections.abc.Sequence[str]
    ) -> ClientRegistrationOptions:
        """The SDK's registration settings, granting a registered client ``mcp_endpoint_scopes``.

        Registration stays off, but the endpoint scopes are still advertised as ``scopes_supported``,
        and none are when ``auth.mcp.operation_scopes`` leaves the endpoint without any.
        """
        return ClientRegistrationOptions(enabled=False, valid_scopes=list(mcp_endpoint_scopes) or None)


class McpDynamicClientRegistry(McpClientDirectory):
    """Clients that register themselves through Dynamic Client Registration, kept in ``store``.

    The 2026-07-28 spec deprecates Dynamic Client Registration.
    It stays only for servers without ``auth.mcp.clients``.
    """

    def __init__(self, store: TokenStore, issuer: str) -> None:
        self._store = store
        self._issuer = issuer

    async def get(self, client_id: str) -> OAuthClientInformationFull | None:
        # A registration stored before issuers keyed them lives under the bare client_id, so it is never read here.
        # The client is then unknown to this server and registers again, which is the migration path.
        data = await self._store.get('mcp_client', self._client_key(client_id))
        return OAuthClientInformationFull(**data) if data else None

    async def register(self, client_info: OAuthClientInformationFull) -> None:
        await self._store.set(
            'mcp_client',
            self._client_key(client_info.client_id),
            client_info.model_dump(exclude_none=True, mode='json'),
        )
        logger.info(
            'Registered MCP client: client_id=%s application_type=%s issuer=%s',
            client_info.client_id,
            client_info.application_type,
            self._issuer,
        )

    def build_registration_options(
        self, mcp_endpoint_scopes: collections.abc.Sequence[str]
    ) -> ClientRegistrationOptions:
        return ClientRegistrationOptions(
            enabled=True, valid_scopes=list(mcp_endpoint_scopes), default_scopes=list(mcp_endpoint_scopes)
        )

    def _client_key(self, client_id: str) -> str:
        """Key a registration by this server's issuer as well as its ``client_id``.

        The MCP spec keys client credentials by issuer.
        Keying the record, rather than only checking a field in it,
        also keeps two servers from overwriting each other's registration of the same ``client_id``.
        """
        return f'{self._issuer} {client_id}'


class McpPreRegisteredClient(OAuthClientInformationFull):
    """A client declared under ``auth.mcp.clients``, granted what it asks for within ``scope``, its maximum.

    Asking for nothing grants ``default_scopes``.
    Asking for scopes partly outside its set grants the part within, as RFC 6749 §3.3 lets the server grant less.
    Asking only for scopes outside it is ``invalid_scope``, since granting something else instead would be silent.
    Without registered redirect URIs, any loopback redirect is accepted on any port and path,
    since OAuth 2.1 requires that of loopback redirects and a CLI client picks its port at run time.
    ``consent_required`` is off only where the operator vouches for the client.
    """

    default_scopes: list[str] = pydantic.Field(default_factory=list)
    consent_required: bool = True

    def validate_scope(self, requested_scope: str | None) -> list[str]:
        requested = requested_scope.split() if requested_scope else []
        if not requested:
            return list(self.default_scopes)
        allowed = get_mcp_registered_scopes(self)
        within = [scope for scope in dict.fromkeys(requested) if scope in allowed]
        if not within:
            logger.warning(
                'Client "%s" asked for scopes %s, none of which it may hold (%s).',
                self.client_id,
                requested,
                allowed,
            )
            raise InvalidScopeError(f'None of the requested scopes is allowed for this client: {" ".join(requested)}')
        return within

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        try:
            if self.redirect_uris:
                return super().validate_redirect_uri(redirect_uri)
            if redirect_uri is None or not _is_loopback(redirect_uri):
                raise InvalidRedirectUriError(
                    f"Redirect URI '{redirect_uri}' is not a loopback address, the only kind this client may use."
                )
            return redirect_uri
        except InvalidRedirectUriError:
            logger.warning(
                'Client "%s" used redirect_uri %s, which is not in auth.mcp.clients.%s.redirect_uris.',
                self.client_id,
                redirect_uri,
                self.client_id,
            )
            raise


class McpPreRegisteredClientDirectory(McpClientDirectory):
    """The clients ``auth.mcp.clients`` declares, and no others."""

    def __init__(self, clients: collections.abc.Iterable[McpPreRegisteredClient]) -> None:
        self._clients: dict[str, McpPreRegisteredClient] = {}
        for client in clients:
            if client.client_id in self._clients:
                raise ValueError(f'Client "{client.client_id}" is registered twice.')
            self._clients[client.client_id] = client

    async def get(self, client_id: str) -> McpPreRegisteredClient | None:
        return self._clients.get(client_id)

    def requires_consent(self, client_id: str) -> bool:
        client = self._clients.get(client_id)
        return client is None or client.consent_required


def get_mcp_registered_scopes(client: OAuthClientInformationFull) -> list[str]:
    """Every scope ``client`` is registered with, the most it may be granted, or none when it names none."""
    return client.scope.split() if client.scope else []


def _is_loopback(uri: AnyUrl) -> bool:
    """Whether ``uri`` is an ``http`` redirect to this machine, as RFC 8252 §7.3 defines a loopback redirect."""
    if uri.scheme != 'http' or uri.host is None:
        return False
    host = uri.host.strip('[]')
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
