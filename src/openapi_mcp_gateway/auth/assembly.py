import abc
import dataclasses
import typing

from mcp.server.auth.provider import OAuthAuthorizationServerProvider, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.extension import Extension
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

from ..openapi import OpenAPISpec
from ..scopes import McpScopePolicy
from ..settings import AuthConfig, ServerConfig
from ..stores.base import TokenStore
from .routes import AuthRoutes
from .upstream.oauth_flow import UpstreamOAuthFlow
from .upstream.resolver import UpstreamAuthResolver


# Where each server's MCP endpoint sits under its mount path, the SDK's streamable HTTP default.
MCP_ENDPOINT_PATH = '/mcp'

# Whose identity the upstream sees on every call.
# ``gateway`` is one credential of the gateway's own, so every agent looks the same upstream.
# ``user`` is each signed-in user's own, and ``caller`` is whatever the caller's header carries.
UpstreamPrincipal = typing.Literal['gateway', 'user', 'caller']

# The ``auth.mcp`` fields, which a strategy building the MCP half names as the ones it reads.
McpSetting = typing.Literal[
    'issuer', 'required_scopes', 'operation_scopes', 'clients', 'access_token_ttl', 'refresh_token_ttl'
]


class AuthTypeContext(typing.NamedTuple):
    """Everything an ``AuthTypeHandler`` may need, whether or not it uses all of it.

    Passing one shape to every handler is what keeps the caller free of per-type branching.
    Only ``oauth2`` reads the store, the URLs and ``mcp_scope_policy``, and that stays the handler's business.
    """

    entry: ServerConfig
    spec: OpenAPISpec
    store: TokenStore
    gateway_url: str
    mcp_scope_policy: McpScopePolicy

    @property
    def mcp_resource_url(self) -> str:
        """The canonical URI of this server's MCP endpoint, which every token for it must be issued for.

        RFC 8707 has a client name it as ``resource``, and RFC 9728 has the server publish it.
        """
        return f'{self.gateway_url.rstrip("/")}{self.entry.mount_path}{MCP_ENDPOINT_PATH}'


@dataclasses.dataclass
class ServerAuthSetup:
    """What assembling one server's auth produces, for both directions, consumed by ``Gateway``.

    ``resolver`` is the upstream half and is always set.
    The rest is the MCP half, and stays empty where nothing checks who calls the endpoint.

    ``provider`` is set when the gateway issues this endpoint's tokens through the SDK's routes.
    ``verifier`` is set instead when it only checks them, issued elsewhere or through its own ``routes``.
    The MCP SDK rejects a server given both.
    ``settings`` and ``protected_resource`` accompany either one and drive the discovery documents.
    ``protected_resource`` is the RFC 9728 document this server publishes, naming whoever mints its tokens.
    ``routes`` are mounted as they are, so the gateway never knows which flow asked for them.
    ``mcp_tokens_carry_upstream_grant`` says each MCP token records what the upstream granted the user behind it,
    which checking an operation's upstream scopes needs.
    ``authorization_server_metadata`` is the RFC 8414 document of the gateway's own authorization server,
    set wherever the gateway issues this endpoint's tokens, whether through ``provider`` or its own routes.
    ``extensions`` are the MCP extensions the server advertises, such as how a client may obtain a token.
    ``mcp_summary`` labels who issues this endpoint's tokens, as ``mechanism (detail)``.
    ``on_shutdown`` lets a flow register a cleanup callback the gateway invokes on shutdown.
    """

    resolver: UpstreamAuthResolver
    provider: OAuthAuthorizationServerProvider[typing.Any, typing.Any, typing.Any] | None = None
    settings: AuthSettings | None = None
    verifier: TokenVerifier | None = None
    protected_resource: ProtectedResourceMetadata | None = None
    routes: list[AuthRoutes] = dataclasses.field(default_factory=list)
    mcp_tokens_carry_upstream_grant: bool = False
    authorization_server_metadata: OAuthMetadata | None = None
    extensions: list[Extension] = dataclasses.field(default_factory=list)
    mcp_summary: str = 'open'
    on_shutdown: typing.Callable[[], typing.Awaitable[None]] | None = None
    # The flow that was resolved, which is not always the one the config named.
    # Set by the factory, so that every handler stays unaware of it.
    flow_type: str | None = None

    def __post_init__(self) -> None:
        if self.provider is not None and self.verifier is not None:
            raise ValueError('A server takes either an authorization server provider or a token verifier, not both.')
        if self.authorization_server_metadata is not None and self.settings is None:
            raise ValueError('A server publishing authorization server metadata needs the auth settings it describes.')


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
    ``mcp_settings_reader`` is set where the strategy builds the MCP half itself.
    Otherwise an ``McpTokenAuthority`` builds it.
    """

    upstream_principal: UpstreamPrincipal
    mcp_settings_reader: McpSettingsReader | None = None


class AuthTypeHandler(abc.ABC):
    """Strategy that turns one ``auth.upstream.type`` into the components the gateway wires up."""

    @abc.abstractmethod
    def select(self, context: AuthTypeContext) -> UpstreamSelection:
        """Settle what this handler would build, without any I/O."""

    @abc.abstractmethod
    def build(self, context: AuthTypeContext) -> ServerAuthSetup:
        """Return the resolver, and for OAuth also the provider or verifier and their settings."""


@dataclasses.dataclass
class OAuthFlowContext:
    """What an ``OAuthFlowHandler`` assembles from, which is what every auth type gets plus the resolved flow.

    Populated by ``build_oauth_flow`` before it dispatches to a concrete handler.
    ``upstream_oauth_flow`` is unset for ``token_exchange``, which runs no flow against the upstream.
    """

    auth_context: AuthTypeContext
    upstream_oauth_flow: UpstreamOAuthFlow | None = None

    @property
    def entry(self) -> ServerConfig:
        return self.auth_context.entry

    @property
    def spec(self) -> OpenAPISpec:
        return self.auth_context.spec

    @property
    def store(self) -> TokenStore:
        return self.auth_context.store

    @property
    def gateway_url(self) -> str:
        return self.auth_context.gateway_url

    @property
    def mcp_scope_policy(self) -> McpScopePolicy:
        return self.auth_context.mcp_scope_policy

    @property
    def mount_path(self) -> str:
        return self.auth_context.entry.mount_path

    @property
    def mcp_resource_url(self) -> str:
        return self.auth_context.mcp_resource_url

    def require_upstream_oauth_flow(self) -> UpstreamOAuthFlow:
        """The upstream OAuth flow, which every flow but ``token_exchange`` runs."""
        if self.upstream_oauth_flow is None:
            raise ValueError(
                f'Server "{self.entry.name}": this flow needs an upstream OAuth flow, and none was resolved.'
            )
        return self.upstream_oauth_flow


class OAuthFlowHandler(abc.ABC):
    """Strategy that assembles one ``auth.upstream.flow`` at startup.

    A flow whose two directions are independent builds only the upstream half.
    A flow that binds them builds both halves and the Bridge that joins them.
    Subclasses register in ``OAUTH_FLOW_HANDLERS``, so the factory can dispatch by ``flow_type``.
    A flow that builds the MCP half is also an ``McpSettingsReader``.
    """

    @property
    @abc.abstractmethod
    def upstream_principal(self) -> UpstreamPrincipal:
        """Whose identity the upstream sees under this flow."""

    @abc.abstractmethod
    def build(self, flow_context: OAuthFlowContext) -> ServerAuthSetup:
        """Return the upstream resolver and whatever MCP-side pieces and shutdown hook this flow brings."""
