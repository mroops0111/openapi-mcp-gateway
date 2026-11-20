import abc
import collections.abc
import dataclasses
import logging
import urllib.parse

import pydantic
from mcp.server.auth.provider import RegistrationError
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import InvalidRedirectUriError, InvalidScopeError, OAuthClientInformationFull
from pydantic import AnyUrl

from ...stores.base import TokenStore
from .client_metadata import (
    ClientMetadataError,
    McpClientMetadataResolver,
    is_client_metadata_url,
    is_loopback_redirect,
)


logger = logging.getLogger(__name__)


class McpClientDirectory(abc.ABC):
    """Where the gateway's authorization server finds the MCP clients allowed to sign in.

    Only a directory that accepts Dynamic Client Registration overrides ``register``,
    so the default refuses it, and the matching registration options turn the endpoint off.
    """

    @abc.abstractmethod
    async def get(self, client_id: str) -> OAuthClientInformationFull | None:
        """Return the client registered under ``client_id``, or ``None`` for one this server does not know."""

    async def admits(self, client_id: str) -> bool:
        """Whether ``client_id`` may still use the tokens it holds, checked on every MCP request.

        By default that is whether it is still found.
        A directory whose lookup reaches the network overrides it, so a request never waits on another server.
        """
        return await self.get(client_id) is not None

    @property
    def accepts_client_metadata_documents(self) -> bool:
        """Whether a client may sign in with its metadata document's URL as its ``client_id``, which by default none may."""
        return False

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
    """A client the operator admits, granted what it asks for within ``scope``, its maximum.

    It is one declared under ``auth.mcp.clients``,
    or one that signs in with a Client ID Metadata Document, whose redirect URIs come from that document.
    Asking for nothing grants ``default_scopes``.
    Asking for scopes partly outside its set grants the part within, as RFC 6749 §3.3 lets the server grant less.
    Asking only for scopes outside it is ``invalid_scope``, since granting something else instead would be silent.
    A redirect must match a registered one exactly, except that a loopback one may use any port,
    since OAuth 2.1 §8.4.2 requires that and a CLI client picks its port at run time.
    Without registered redirect URIs, any loopback redirect is accepted on any port and path.
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
                if redirect_uri is None:
                    return super().validate_redirect_uri(redirect_uri)
                if not any(_matches_registered(redirect_uri, registered) for registered in self.redirect_uris):
                    raise InvalidRedirectUriError(f"Redirect URI '{redirect_uri}' not registered for client")
                return redirect_uri
            if redirect_uri is None or not is_loopback_redirect(redirect_uri):
                raise InvalidRedirectUriError(
                    f"Redirect URI '{redirect_uri}' is not a loopback address, the only kind this client may use."
                )
            return redirect_uri
        except InvalidRedirectUriError:
            where = (
                'the redirect_uris of its metadata document'
                if is_client_metadata_url(self.client_id)
                else f'auth.mcp.clients.{self.client_id}.redirect_uris'
            )
            logger.warning('Client "%s" used redirect_uri %s, which is not in %s.', self.client_id, redirect_uri, where)
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

    def find(self, client_id: str) -> McpPreRegisteredClient | None:
        """The client declared as ``client_id``, without the wait ``get`` has for other directories."""
        return self._clients.get(client_id)

    @property
    def client_ids(self) -> list[str]:
        return list(self._clients)

    def requires_consent(self, client_id: str) -> bool:
        client = self._clients.get(client_id)
        return client is None or client.consent_required


@dataclasses.dataclass(frozen=True)
class McpOtherClients:
    """The rule for Client ID Metadata Document clients that ``auth.mcp.clients`` does not list.

    ``trusted_hosts`` limits them to the hosts their documents sit on, matched exactly, and ``None`` admits any.
    ``scope`` and ``default_scopes`` are what each is granted, as for a listed client.
    """

    scope: str
    default_scopes: list[str]
    trusted_hosts: frozenset[str] | None = None

    def admits(self, client_id: str) -> bool:
        """Whether ``client_id`` is a metadata document URL on a trusted host, without fetching the document."""
        if not is_client_metadata_url(client_id):
            return False
        return self.trusted_hosts is None or urllib.parse.urlsplit(client_id).hostname in self.trusted_hosts

    def build_client(self, client_id: str) -> McpPreRegisteredClient:
        """The client ``client_id`` stands for, before its document supplies its name and redirect URIs."""
        return McpPreRegisteredClient(
            client_id=client_id,
            token_endpoint_auth_method='none',  # noqa: S106
            scope=self.scope,
            default_scopes=self.default_scopes,
        )


class McpClientMetadataDirectory(McpClientDirectory):
    """The clients ``listed`` holds, the Client ID Metadata Document clients ``other_clients`` admits,
    and Dynamic Client Registration into ``registry`` when it is given.

    A ``client_id`` that is a metadata document's URL is admitted when ``listed`` holds it or ``other_clients`` admits it.
    Its document then supplies its name and redirect URIs,
    and the listing or ``other_clients`` its scopes and whether it needs consent.
    Whether it may keep using its tokens is read from that configuration alone,
    so the document is fetched only to sign in or refresh, and a client whose server is down keeps working.
    """

    def __init__(
        self,
        listed: McpPreRegisteredClientDirectory,
        other_clients: McpOtherClients | None,
        resolver: McpClientMetadataResolver,
        registry: McpDynamicClientRegistry | None = None,
    ) -> None:
        self._listed = listed
        self._other_clients = other_clients
        self._resolver = resolver
        self._registry = registry

    async def get(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self._find_metadata_client(client_id)
        if client is None:
            return self._listed.find(client_id) or (await self._registry.get(client_id) if self._registry else None)
        try:
            document = await self._resolver.resolve(client_id)
        except ClientMetadataError as error:
            logger.warning('Client "%s" cannot sign in, since its metadata document %s.', client_id, error)
            return None
        return client.model_copy(update={'client_name': document.client_name, 'redirect_uris': document.redirect_uris})

    async def admits(self, client_id: str) -> bool:
        if self._listed.find(client_id) is not None:
            return True
        if self._other_clients is not None and self._other_clients.admits(client_id):
            return True
        return self._registry is not None and await self._registry.get(client_id) is not None

    @property
    def accepts_client_metadata_documents(self) -> bool:
        return self._other_clients is not None or any(map(is_client_metadata_url, self._listed.client_ids))

    async def register(self, client_info: OAuthClientInformationFull) -> None:
        if self._registry is None:
            await super().register(client_info)
        else:
            await self._registry.register(client_info)

    def requires_consent(self, client_id: str) -> bool:
        return self._listed.requires_consent(client_id)

    def build_registration_options(
        self, mcp_endpoint_scopes: collections.abc.Sequence[str]
    ) -> ClientRegistrationOptions:
        if self._registry is None:
            return super().build_registration_options(mcp_endpoint_scopes)
        return self._registry.build_registration_options(mcp_endpoint_scopes)

    def _find_metadata_client(self, client_id: str) -> McpPreRegisteredClient | None:
        """The client a metadata document URL stands for, listed or admitted as another, before its document is read."""
        if not is_client_metadata_url(client_id):
            return None
        listed = self._listed.find(client_id)
        if listed is not None:
            return listed
        if self._other_clients is not None and self._other_clients.admits(client_id):
            return self._other_clients.build_client(client_id)
        return None


def get_mcp_registered_scopes(client: OAuthClientInformationFull) -> list[str]:
    """Every scope ``client`` is registered with, the most it may be granted, or none when it names none."""
    return client.scope.split() if client.scope else []


def _matches_registered(redirect_uri: AnyUrl, registered: AnyUrl) -> bool:
    """Whether ``redirect_uri`` is ``registered``, ignoring only the port of a loopback one, per OAuth 2.1 §8.4.2."""
    if redirect_uri == registered:
        return True
    if not (is_loopback_redirect(redirect_uri) and is_loopback_redirect(registered)):
        return False
    return (redirect_uri.scheme, redirect_uri.host, redirect_uri.path, redirect_uri.query) == (
        registered.scheme,
        registered.host,
        registered.path,
        registered.query,
    )
