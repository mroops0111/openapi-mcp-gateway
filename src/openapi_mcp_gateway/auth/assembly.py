import abc
import dataclasses
import typing

import pydantic
from mcp.server.auth.provider import OAuthAuthorizationServerProvider, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.extension import Extension
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

from ..openapi import OpenAPISpec
from ..scopes import ScopeRegistry, UpstreamGrantSource, UpstreamNoGrantSource
from ..settings import AuthConfig, ServerConfig
from ..stores.base import TokenStore
from .mcp.client_metadata import McpClientMetadataResolver
from .routes import AuthRoutes
from .upstream.reauthorization import UpstreamNoReauthorizer, UpstreamReauthorizer
from .upstream.resolver import UpstreamAuthResolver


# Where each server's MCP endpoint sits under its mount path, the SDK's streamable HTTP default.
MCP_ENDPOINT_PATH = '/mcp'

# Whose identity the upstream sees on every call.
# ``gateway`` is one credential of the gateway's own, so every agent looks the same upstream.
# ``user`` is each signed-in user's own, and ``caller`` is whatever the caller's header carries.
UpstreamPrincipal = typing.Literal['gateway', 'user', 'caller']

# The ``auth.upstream`` fields besides ``type``, which each upstream strategy names as the ones it reads.
UpstreamSetting = typing.Literal[
    'credentials',
    'flow',
    'client_id',
    'client_secret',
    'authorization_url',
    'token_url',
    'scopes',
    'required_scopes',
    'issuer',
    'resource',
    'audience',
    'enforce_scopes',
    'scope_implies',
]

# The ``auth.mcp`` fields, which a strategy building the MCP half names as the ones it reads.
McpSetting = typing.Literal[
    'issuer',
    'required_scopes',
    'operation_scopes',
    'clients',
    'other_clients',
    'access_token_ttl',
    'refresh_token_ttl',
]


class AuthTypeContext(typing.NamedTuple):
    """Everything an ``AuthTypeHandler`` may need, whether or not it uses all of it.

    Passing one shape to every handler is what keeps the caller free of per-type branching.
    The store, the URLs, ``scope_registry`` and ``client_metadata_resolver`` serve whichever strategy builds an MCP half
    or an OAuth flow, and a static credential reads none of them.
    What a handler reads stays its own business.
    """

    entry: ServerConfig
    spec: OpenAPISpec
    store: TokenStore
    gateway_url: str
    scope_registry: ScopeRegistry
    client_metadata_resolver: McpClientMetadataResolver

    @property
    def issuer_url(self) -> str:
        """This server's URL, which is its issuer wherever the gateway issues its tokens.

        Normalized as ``pydantic.AnyHttpUrl`` has it, the form every document publishes.
        Every other URL of this server appends a path to it, and so stays normalized without parsing it again.
        """
        return str(pydantic.AnyHttpUrl(f'{self.gateway_url.rstrip("/")}{self.entry.mount_path}'))

    @property
    def mcp_resource_url(self) -> str:
        """The canonical URI of this server's MCP endpoint, which every token for it must be issued for.

        RFC 8707 has a client name it as ``resource``, and RFC 9728 has the server publish it.
        """
        return f'{self.issuer_url.rstrip("/")}{MCP_ENDPOINT_PATH}'


@dataclasses.dataclass(frozen=True)
class UpstreamDescription:
    """How an upstream strategy reaches the upstream, as ``describe`` and the dry-run table report it.

    ``summary`` labels it as ``mechanism (detail)``.
    ``resolved_settings`` are the ``auth.upstream`` fields the strategy settled, keyed as the config names them,
    such as a flow picked from the spec. Each strategy lists only the fields it has, and never a credential.
    """

    summary: str = 'none'
    resolved_settings: typing.Mapping[UpstreamSetting, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ServerAuthSetup:
    """What assembling one server's auth produces, for both directions, consumed by ``Gateway``.

    ``resolver`` is the upstream half and is always set.
    ``reauthorizer`` cures an upstream refusal of the caller's own upstream token,
    by renewing it or sending the user to sign in again, where the flow holds such a token.
    The rest is the MCP half, and stays empty where nothing checks who calls the endpoint.

    ``provider`` is set when the gateway issues this endpoint's tokens through the SDK's routes.
    ``verifier`` is set instead when it only checks them, issued elsewhere or through its own ``routes``.
    The MCP SDK rejects a server given both.
    ``settings`` and ``protected_resource`` accompany either one and drive the discovery documents.
    ``protected_resource`` is the RFC 9728 document this server publishes, naming whoever mints its tokens.
    ``routes`` are mounted as they are, so the gateway never knows which flow asked for them.
    ``upstream_grant_source`` is where a call finds the upstream grant it is checked against.
    ``authorization_server_metadata`` is the RFC 8414 document of the gateway's own authorization server,
    set wherever the gateway issues this endpoint's tokens, whether through ``provider`` or its own routes.
    ``extensions`` are the MCP extensions the server advertises, such as how a client may obtain a token.
    ``mcp_summary`` labels who issues this endpoint's tokens, as ``mechanism (detail)``.
    ``upstream_description`` says how the upstream is reached, for reporting only.
    ``on_shutdown`` lets a flow register a cleanup callback the gateway invokes on shutdown.
    """

    resolver: UpstreamAuthResolver
    reauthorizer: UpstreamReauthorizer = dataclasses.field(default_factory=UpstreamNoReauthorizer)
    provider: OAuthAuthorizationServerProvider[typing.Any, typing.Any, typing.Any] | None = None
    settings: AuthSettings | None = None
    verifier: TokenVerifier | None = None
    protected_resource: ProtectedResourceMetadata | None = None
    routes: list[AuthRoutes] = dataclasses.field(default_factory=list)
    upstream_grant_source: UpstreamGrantSource = dataclasses.field(default_factory=UpstreamNoGrantSource)
    authorization_server_metadata: OAuthMetadata | None = None
    extensions: list[Extension] = dataclasses.field(default_factory=list)
    mcp_summary: str = 'open'
    upstream_description: UpstreamDescription = dataclasses.field(default_factory=UpstreamDescription)
    on_shutdown: typing.Callable[[], typing.Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        if self.provider is not None and self.verifier is not None:
            raise ValueError('A server takes either an authorization server provider or a token verifier, not both.')
        if self.authorization_server_metadata is not None and self.settings is None:
            raise ValueError('A server publishing authorization server metadata needs the auth settings it describes.')
        if self.settings is not None and self.protected_resource is None:
            # Each strategy states who mints its tokens, so the app never has to work it out.
            raise ValueError('A server checking tokens needs the protected resource metadata naming their issuer.')


class McpSettingsReader(abc.ABC):
    """A strategy that builds the MCP half, and so reads ``auth.mcp``, a capability only some strategies have.

    An ``auth.mcp`` field that the reader selected for a server does not read is refused before anything is built.
    """

    @property
    @abc.abstractmethod
    def upstream_principal(self) -> UpstreamPrincipal:
        """Whose identity the upstream sees where this strategy applies."""

    @property
    @abc.abstractmethod
    def mcp_settings(self) -> frozenset[McpSetting]:
        """The ``auth.mcp`` fields this strategy reads."""

    @property
    @abc.abstractmethod
    def mcp_settings_requirement(self) -> str:
        """What selects this strategy, named by the error that refuses one of its fields elsewhere."""

    def refuse_unusable_settings(self, auth: AuthConfig, server_name: str) -> None:
        """Refuse ``auth`` settings this strategy cannot honour, before anything is built. Nothing, by default."""
        return None


@dataclasses.dataclass(frozen=True)
class UpstreamSelection:
    """What an upstream strategy settles before it builds anything, so a config mistake is refused first.

    ``upstream_principal`` is whose identity the upstream sees.
    ``upstream_settings`` are the ``auth.upstream`` fields the strategy reads, and any other one set is refused.
    ``mcp_settings_reader`` is set where the strategy builds the MCP half itself.
    Otherwise an ``McpTokenAuthority`` builds it.
    """

    upstream_principal: UpstreamPrincipal
    upstream_settings: frozenset[UpstreamSetting] = frozenset()
    mcp_settings_reader: McpSettingsReader | None = None
