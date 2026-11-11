import abc
import collections.abc
import ipaddress
import logging

from mcp.server.auth.provider import RegistrationError
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull
from pydantic import AnyUrl

from ...settings import ClientConfig
from ...stores.base import TokenStore


logger = logging.getLogger(__name__)


# The host names RFC 8252 §7.3 treats as loopback besides the IP literals.
_LOOPBACK_NAMES = frozenset({'localhost'})


class ClientDirectory(abc.ABC):
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

    def registration_options(self, scopes: collections.abc.Sequence[str]) -> ClientRegistrationOptions:
        """The SDK's registration settings, granting a registered client ``scopes``."""
        return ClientRegistrationOptions(enabled=False)


class DynamicClientRegistry(ClientDirectory):
    """Clients that register themselves through Dynamic Client Registration, kept in ``store``.

    The 2026-07-28 spec deprecates Dynamic Client Registration, which stays only for servers without ``auth.clients``.
    """

    def __init__(self, store: TokenStore, issuer: str) -> None:
        self.store = store
        self.issuer = issuer

    async def get(self, client_id: str) -> OAuthClientInformationFull | None:
        # A registration stored before issuer binding lives under the bare client_id and is never read here.
        # The client is then unknown to this server and registers again, which is the migration path.
        data = await self.store.get('mcp_client', self._client_key(client_id))
        return OAuthClientInformationFull(**data) if data else None

    async def register(self, client_info: OAuthClientInformationFull) -> None:
        await self.store.set(
            'mcp_client',
            self._client_key(client_info.client_id),
            client_info.model_dump(exclude_none=True, mode='json'),
        )
        logger.info(
            'Registered MCP client: client_id=%s application_type=%s issuer=%s',
            client_info.client_id,
            client_info.application_type,
            self.issuer,
        )

    def registration_options(self, scopes: collections.abc.Sequence[str]) -> ClientRegistrationOptions:
        return ClientRegistrationOptions(enabled=True, valid_scopes=list(scopes), default_scopes=list(scopes))

    def _client_key(self, client_id: str) -> str:
        """Key a registration by this server's issuer as well as its ``client_id``.

        The MCP spec keys client credentials by issuer.
        Keying the record, rather than only checking a field in it,
        also keeps two servers from overwriting each other's registration of the same ``client_id``.
        """
        return f'{self.issuer} {client_id}'


class PreRegisteredClient(OAuthClientInformationFull):
    """A client declared under ``auth.clients``, which is granted scopes rather than refused them.

    Asking for a scope outside its set is not an error.
    RFC 6749 §3.3 lets the server grant less than asked, so the client receives what it asked for within its set,
    or its whole set when that leaves nothing.
    Without registered redirect URIs, any loopback redirect is accepted on any port and path,
    since OAuth 2.1 requires that of loopback redirects and a CLI client picks its port at run time.
    """

    def validate_scope(self, requested_scope: str | None) -> list[str]:
        granted = self.scope.split() if self.scope else []
        requested = requested_scope.split() if requested_scope else []
        within = [scope for scope in dict.fromkeys(requested) if scope in granted]
        return within or granted

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
                'Client "%s" used redirect_uri %s, which is not in auth.clients.%s.redirect_uris.',
                self.client_id,
                redirect_uri,
                self.client_id,
            )
            raise


class PreRegisteredClientDirectory(ClientDirectory):
    """The clients ``auth.clients`` declares, each granted ``endpoint_scopes`` plus its own scopes."""

    def __init__(
        self,
        clients: collections.abc.Mapping[str, ClientConfig],
        endpoint_scopes: collections.abc.Sequence[str],
    ) -> None:
        self._clients = {
            client_id: _pre_registered_client(client_id, client, endpoint_scopes)
            for client_id, client in clients.items()
        }

    async def get(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)


def _pre_registered_client(
    client_id: str, client: ClientConfig, endpoint_scopes: collections.abc.Sequence[str]
) -> PreRegisteredClient:
    """Build the SDK's record of a client declared in config.

    A confidential client authenticates with HTTP Basic,
    the method RFC 6749 §2.3.1 requires every server to support and the one clients try first.
    """
    secret = client.resolve_client_secret()
    return PreRegisteredClient(
        client_id=client_id,
        client_secret=secret,
        token_endpoint_auth_method='client_secret_basic' if secret else 'none',
        redirect_uris=client.redirect_uris,
        scope=' '.join(dict.fromkeys([*endpoint_scopes, *client.scopes])),
    )


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
