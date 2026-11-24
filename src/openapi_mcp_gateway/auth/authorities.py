import abc
import dataclasses

import pydantic
from mcp.server.auth.settings import AuthSettings
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

from ..settings import AuthConfig
from .assembly import AuthTypeContext, McpSetting, McpSettingsReader, ServerAuthSetup, UpstreamPrincipal
from .mcp.client_credentials import (
    AUTHORIZE_PATH,
    TOKEN_ENDPOINT_AUTH_METHODS,
    TOKEN_PATH,
    McpClientCredentialsExtension,
    McpClientCredentialsGrant,
    McpClientCredentialsRoutes,
)
from .mcp.external_issuer import (
    McpExternalIssuerMetadata,
    McpExternalIssuerTokenVerifier,
    fetch_mcp_external_issuer_metadata,
)
from .mcp.record_origin import McpRecordOrigin
from .mcp.token_issuer import McpGatewayIssuerTokenVerifier, McpTokenIssuer
from .settings_mapping import build_mcp_client_directory, build_mcp_token_policy


class McpTokenAuthority(abc.ABC):
    """Strategy deciding who issues the tokens of an MCP endpoint whose upstream strategy left that open.

    A flow that binds both directions, such as ``authorization_code``, builds its MCP half itself.
    Every other upstream strategy builds only the upstream half, and ``build_auth`` hands its setup here.
    Adding a way to issue MCP tokens means adding a subclass and an entry in ``MCP_TOKEN_AUTHORITIES``.
    An authority that reads ``auth.mcp`` is also an ``McpSettingsReader``.
    """

    @abc.abstractmethod
    def applies_to(self, auth: AuthConfig, upstream_principal: UpstreamPrincipal) -> bool:
        """Whether ``auth`` selects this authority for an upstream that sees ``upstream_principal``."""

    @abc.abstractmethod
    def attach(self, setup: ServerAuthSetup, context: AuthTypeContext) -> ServerAuthSetup:
        """Return ``setup`` with the MCP half this authority provides."""


class McpNoTokenAuthority(McpTokenAuthority):
    """Leave the endpoint open, as for a server on localhost or inside a private network."""

    def applies_to(self, auth: AuthConfig, upstream_principal: UpstreamPrincipal) -> bool:
        return True

    def attach(self, setup: ServerAuthSetup, context: AuthTypeContext) -> ServerAuthSetup:
        return setup


class McpClientCredentialsAuthority(McpTokenAuthority, McpSettingsReader):
    """Issue MCP tokens to the agents ``auth.mcp.clients`` declares, through the ``client_credentials`` grant.

    It suits an upstream that sees only the gateway's identity, where only the gateway can tell agents apart,
    so each agent is limited to the operations its scopes open.
    """

    @property
    def upstream_principal(self) -> UpstreamPrincipal:
        return 'gateway'

    @property
    def mcp_settings(self) -> frozenset[McpSetting]:
        return frozenset({'required_scopes', 'operation_scopes', 'clients', 'access_token_ttl'})

    @property
    def mcp_settings_requirement(self) -> str:
        return "auth.mcp.clients on an upstream that sees only the gateway's identity"

    def applies_to(self, auth: AuthConfig, upstream_principal: UpstreamPrincipal) -> bool:
        return upstream_principal == self.upstream_principal and bool(auth.mcp.clients)

    def refuse_unusable_settings(self, auth: AuthConfig, server_name: str) -> None:
        auth.mcp.refuse_client_settings_without_sign_in(server_name)
        auth.mcp.refuse_scope_settings_for_gateway_tokens(server_name)

    def attach(self, setup: ServerAuthSetup, context: AuthTypeContext) -> ServerAuthSetup:
        entry = context.entry
        # The same string the metadata publishes as ``issuer``, since clients compare the two exactly.
        issuer = context.issuer_url
        server_url = pydantic.AnyHttpUrl(issuer)
        base = issuer.rstrip('/')
        policy = build_mcp_token_policy(entry.auth.mcp)
        resource = context.mcp_resource_url
        tokens = McpTokenIssuer(context.store, McpRecordOrigin(issuer=issuer), policy, resource)
        clients = build_mcp_client_directory(entry.auth.mcp.clients, policy.scopes)
        grant = McpClientCredentialsGrant(tokens, clients)
        protected_resource = ProtectedResourceMetadata.model_validate(
            {'resource': resource, 'authorization_servers': [issuer], 'scopes_supported': policy.scopes or None}
        )
        metadata = OAuthMetadata.model_validate(
            {
                'issuer': issuer,
                'authorization_endpoint': f'{base}{AUTHORIZE_PATH}',
                'token_endpoint': f'{base}{TOKEN_PATH}',
                'grant_types_supported': ['client_credentials'],
                'token_endpoint_auth_methods_supported': list(TOKEN_ENDPOINT_AUTH_METHODS),
                'response_types_supported': [],
                'scopes_supported': policy.scopes or None,
            }
        )
        return dataclasses.replace(
            setup,
            verifier=McpGatewayIssuerTokenVerifier(tokens, clients),
            settings=AuthSettings(
                issuer_url=server_url,
                resource_server_url=pydantic.AnyHttpUrl(resource),
                required_scopes=policy.scopes,
                validate_token_resource=True,
            ),
            protected_resource=protected_resource,
            routes=[*setup.routes, McpClientCredentialsRoutes(grant)],
            authorization_server_metadata=metadata,
            extensions=[*setup.extensions, McpClientCredentialsExtension()],
            mcp_summary='gateway (client_credentials)',
        )


class McpExternalIssuerAuthority(McpTokenAuthority, McpSettingsReader):
    """Accept the tokens an authorization server the gateway does not own mints for this endpoint.

    The gateway only verifies them, as a resource server, and the issuer alone decides what each token holds.
    It applies to an upstream that sees only the gateway's identity once ``auth.mcp.issuer`` is set,
    and ``token_exchange`` builds its MCP half with it too.
    """

    @property
    def upstream_principal(self) -> UpstreamPrincipal:
        return 'gateway'

    @property
    def mcp_settings(self) -> frozenset[McpSetting]:
        return frozenset({'issuer', 'required_scopes', 'operation_scopes'})

    @property
    def mcp_settings_requirement(self) -> str:
        return "auth.mcp.issuer on an upstream that sees only the gateway's identity"

    def applies_to(self, auth: AuthConfig, upstream_principal: UpstreamPrincipal) -> bool:
        return upstream_principal == self.upstream_principal and bool(auth.mcp.resolve_issuer())

    def attach(self, setup: ServerAuthSetup, context: AuthTypeContext) -> ServerAuthSetup:
        return self.attach_discovered(setup, context, self.discover(context))

    def discover(self, context: AuthTypeContext) -> McpExternalIssuerMetadata:
        """The metadata ``auth.mcp.issuer`` publishes, fetched at startup.

        Raises ``ValueError`` when no issuer is set.
        """
        issuer = context.entry.auth.mcp.resolve_issuer()
        if not issuer:
            raise ValueError(
                f'Server "{context.entry.name}": auth.mcp.issuer is unset, '
                'so no authorization server mints tokens for this MCP endpoint.'
            )
        return fetch_mcp_external_issuer_metadata(issuer)

    def attach_discovered(
        self, setup: ServerAuthSetup, context: AuthTypeContext, metadata: McpExternalIssuerMetadata
    ) -> ServerAuthSetup:
        """Return ``setup`` with the MCP half that verifies the tokens the issuer behind ``metadata`` mints."""
        # RFC 8707 §2: the canonical URI of this MCP server.
        # A client asks the issuer for this, so it is what the issuer stamps into the token's audience.
        resource = context.mcp_resource_url
        mcp_endpoint_scopes = list(context.entry.auth.mcp.required_scopes)
        verifier = McpExternalIssuerTokenVerifier(
            issuer=metadata.issuer,
            audience=resource,
            jwks_uri=metadata.jwks_uri,
            required_scopes=mcp_endpoint_scopes,
            # The issuer alone knows the most it would grant, so every scope an operation names stays in reach.
            # A client then sees each tool and steps up for it, and the issuer decides whether to grant it.
            mcp_grantable_scopes=list(context.scope_registry.mcp_policy.mcp_defined_scopes) or None,
        )
        return dataclasses.replace(
            setup,
            verifier=verifier,
            settings=AuthSettings(
                issuer_url=pydantic.AnyHttpUrl(metadata.issuer),
                resource_server_url=pydantic.AnyHttpUrl(resource),
                validate_token_resource=True,
                required_scopes=mcp_endpoint_scopes or None,
            ),
            # Validated from strings so a path-less issuer keeps its exact form.
            # RFC 8414 §2 compares issuers by exact string, and ``AnyHttpUrl`` would append a slash.
            protected_resource=ProtectedResourceMetadata.model_validate(
                {
                    'resource': resource,
                    'authorization_servers': [metadata.issuer],
                    # RFC 9728 defines this as what a client must obtain for this endpoint,
                    # so it is the inbound list, and a registering client reads it to know what to ask for.
                    'scopes_supported': mcp_endpoint_scopes or None,
                }
            ),
            mcp_summary=f'issuer ({metadata.issuer})',
        )


# The extension point, tried in order, so the default that always applies comes last.
MCP_TOKEN_AUTHORITIES: tuple[type[McpTokenAuthority], ...] = (
    McpExternalIssuerAuthority,
    McpClientCredentialsAuthority,
    McpNoTokenAuthority,
)


def select_mcp_token_authority(
    auth: AuthConfig, upstream_principal: UpstreamPrincipal, server_name: str
) -> McpTokenAuthority:
    """The one authority ``auth`` selects for an upstream that sees ``upstream_principal``, or the default.

    Raises ``ValueError`` when two authorities both apply, since only one may issue an endpoint's tokens.
    """
    *specific_classes, default_class = MCP_TOKEN_AUTHORITIES
    applying = [
        authority
        for authority in (authority_class() for authority_class in specific_classes)
        if authority.applies_to(auth, upstream_principal)
    ]
    if len(applying) > 1:
        named = ' and '.join(_describe_selection(authority) for authority in applying)
        raise ValueError(
            f'Server "{server_name}": {named} each issue this endpoint\'s tokens, so set only one of them.'
        )
    return applying[0] if applying else default_class()


def _describe_selection(authority: McpTokenAuthority) -> str:
    """What selects ``authority``, as an error names it."""
    if isinstance(authority, McpSettingsReader):
        return authority.mcp_settings_requirement
    return type(authority).__name__
