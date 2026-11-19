import abc
import dataclasses
import typing

import pydantic
from mcp.server.auth.settings import AuthSettings
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

from ..settings import AuthConfig
from .assembly import AuthTypeContext, ServerAuthSetup, UpstreamPrincipal
from .mcp.client import McpPreRegisteredClientDirectory
from .mcp.client_credentials import (
    AUTHORIZE_PATH,
    TOKEN_ENDPOINT_AUTH_METHODS,
    TOKEN_PATH,
    McpClientCredentialsExtension,
    McpClientCredentialsGrant,
    McpClientCredentialsRoutes,
    McpGatewayTokenVerifier,
)
from .mcp.record_origin import McpRecordOrigin
from .mcp.token_issuer import McpTokenIssuer, McpTokenPolicy


class McpTokenAuthority(abc.ABC):
    """Strategy deciding who issues the tokens of an MCP endpoint whose upstream strategy left that open.

    A flow that binds both directions, such as ``authorization_code``, builds its MCP half itself.
    Every other upstream strategy builds only the upstream half, and ``build_auth`` hands its setup here.
    Adding a way to issue MCP tokens means adding a subclass and an entry in ``MCP_TOKEN_AUTHORITIES``.

    ``upstream_principal`` is the identity an upstream must see for this authority to apply, or ``None`` for any.
    ``mcp_settings`` and ``mcp_settings_requirement`` mean what they mean on ``OAuthFlowHandler``.
    """

    upstream_principal: typing.ClassVar[UpstreamPrincipal | None] = None
    mcp_settings: typing.ClassVar[frozenset[str]] = frozenset()
    mcp_settings_requirement: typing.ClassVar[str] = ''

    @classmethod
    @abc.abstractmethod
    def applies_to(cls, auth: AuthConfig, upstream_principal: UpstreamPrincipal | None) -> bool:
        """Whether ``auth`` selects this authority for an upstream that sees ``upstream_principal``."""

    @abc.abstractmethod
    def attach(self, setup: ServerAuthSetup, context: AuthTypeContext) -> ServerAuthSetup:
        """Return ``setup`` with the MCP half this authority provides."""


class McpNoTokenAuthority(McpTokenAuthority):
    """Leave the endpoint open, as for a server on localhost or inside a private network."""

    @classmethod
    def applies_to(cls, auth: AuthConfig, upstream_principal: UpstreamPrincipal | None) -> bool:
        return True

    def attach(self, setup: ServerAuthSetup, context: AuthTypeContext) -> ServerAuthSetup:
        return setup


class McpClientCredentialsAuthority(McpTokenAuthority):
    """Issue MCP tokens to the agents ``auth.mcp.clients`` declares, through the ``client_credentials`` grant.

    It suits an upstream that sees only the gateway's identity, where only the gateway can tell agents apart,
    so each agent is limited to the operations its scopes open.
    """

    upstream_principal = 'gateway'
    mcp_settings = frozenset({'required_scopes', 'operation_scopes', 'clients', 'access_token_ttl'})
    mcp_settings_requirement = 'auth.mcp.clients, the agents the gateway issues MCP tokens to'

    @classmethod
    def applies_to(cls, auth: AuthConfig, upstream_principal: UpstreamPrincipal | None) -> bool:
        return upstream_principal == cls.upstream_principal and bool(auth.mcp.clients)

    def attach(self, setup: ServerAuthSetup, context: AuthTypeContext) -> ServerAuthSetup:
        entry = context.entry
        entry.auth.mcp.refuse_client_settings_without_sign_in(entry.name)
        server_url = pydantic.AnyHttpUrl(f'{context.gateway_url.rstrip("/")}{entry.mount_path}')
        # The same string the metadata publishes as ``issuer``, since clients compare the two exactly.
        issuer = str(server_url)
        base = issuer.rstrip('/')
        policy = McpTokenPolicy.from_config(entry.auth.mcp)
        tokens = McpTokenIssuer(context.store, McpRecordOrigin(issuer=issuer), policy)
        grant = McpClientCredentialsGrant(
            tokens, McpPreRegisteredClientDirectory(entry.auth.mcp.clients, policy.scopes)
        )
        # The endpoint's canonical URI, published in the protected resource document and enforced at the token endpoint.
        protected_resource = ProtectedResourceMetadata.model_validate(
            {'resource': f'{base}/mcp', 'authorization_servers': [issuer]}
        )
        metadata = OAuthMetadata.model_validate(
            {
                'issuer': issuer,
                'authorization_endpoint': f'{base}{AUTHORIZE_PATH}',
                'token_endpoint': f'{base}{TOKEN_PATH}',
                'grant_types_supported': ['client_credentials'],
                'token_endpoint_auth_methods_supported': list(TOKEN_ENDPOINT_AUTH_METHODS),
                'response_types_supported': [],
            }
        )
        return dataclasses.replace(
            setup,
            verifier=McpGatewayTokenVerifier(tokens),
            # The verifier reads only tokens whose origin is this server, so they cannot be for another resource.
            settings=AuthSettings(
                issuer_url=server_url,
                resource_server_url=server_url,
                required_scopes=policy.scopes,
                validate_token_resource=False,
            ),
            protected_resource=protected_resource,
            routes=[*setup.routes, McpClientCredentialsRoutes(grant, resource=str(protected_resource.resource))],
            authorization_server_metadata=metadata,
            extensions=[*setup.extensions, McpClientCredentialsExtension()],
            mcp_summary='gateway (client_credentials)',
            mcp_settings_read=self.mcp_settings,
        )


# The extension point, tried in order, so the default that always applies comes last.
MCP_TOKEN_AUTHORITIES: tuple[type[McpTokenAuthority], ...] = (McpClientCredentialsAuthority, McpNoTokenAuthority)


def attach_mcp_token_authority(setup: ServerAuthSetup, context: AuthTypeContext) -> ServerAuthSetup:
    """Give ``setup`` an MCP half from the first authority the config selects, unless it already has one.

    The authorities decide by the upstream's principal.
    The check for an existing MCP half only keeps one from overwriting a strategy that built both.
    """
    if setup.provider is not None or setup.verifier is not None:
        return setup
    auth = context.entry.auth
    authority = next(
        authority for authority in MCP_TOKEN_AUTHORITIES if authority.applies_to(auth, setup.upstream_principal)
    )
    return authority().attach(setup, context)
