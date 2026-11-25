import abc
import dataclasses
import typing

import pydantic
from mcp.server.auth.settings import AuthSettings
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

from ..settings import AuthConfig, McpAuthConfig, is_url_client_id
from .assembly import AuthTypeContext, McpSetting, McpSettingsReader, ServerAuthSetup, UpstreamPrincipal
from .external_issuer_setup import McpExternalIssuerSetup
from .mcp.client_credentials import (
    AUTHORIZE_PATH,
    TOKEN_ENDPOINT_AUTH_METHODS,
    TOKEN_PATH,
    McpClientCredentialsExtension,
    McpClientCredentialsGrant,
    McpClientCredentialsRoutes,
)
from .mcp.record_origin import McpRecordOrigin
from .mcp.token_issuer import McpGatewayIssuerTokenVerifier, McpTokenIssuer
from .settings_mapping import build_mcp_client_directory, build_mcp_token_policy, refuse_unusable_mcp_token_scopes


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
        _refuse_client_settings_without_sign_in(auth.mcp, server_name)
        refuse_unusable_mcp_token_scopes(auth.mcp, server_name)

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

    It applies to an upstream that sees only the gateway's identity once ``auth.mcp.issuer`` is set,
    and builds the MCP half ``McpExternalIssuerSetup`` describes.
    """

    def __init__(self) -> None:
        self._issuer_setup = McpExternalIssuerSetup()

    @property
    def upstream_principal(self) -> UpstreamPrincipal:
        return 'gateway'

    @property
    def mcp_settings(self) -> frozenset[McpSetting]:
        return self._issuer_setup.mcp_settings

    @property
    def mcp_settings_requirement(self) -> str:
        return "auth.mcp.issuer on an upstream that sees only the gateway's identity"

    def applies_to(self, auth: AuthConfig, upstream_principal: UpstreamPrincipal) -> bool:
        return upstream_principal == self.upstream_principal and bool(auth.mcp.resolve_issuer())

    def refuse_unusable_settings(self, auth: AuthConfig, server_name: str) -> None:
        self._issuer_setup.refuse_unusable_settings(auth, server_name)

    def attach(self, setup: ServerAuthSetup, context: AuthTypeContext) -> ServerAuthSetup:
        # Selected only once auth.mcp.issuer is set, as applies_to checks.
        issuer = typing.cast(str, context.entry.auth.mcp.resolve_issuer())
        return self._issuer_setup.attach(setup, context, self._issuer_setup.discover(issuer))


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


def _refuse_client_settings_without_sign_in(config: McpAuthConfig, server_name: str) -> None:
    """Refuse what only a user signing in reads, for a server where agents obtain tokens as themselves.

    Each client needs a secret, since it is its only proof of identity.
    A redirect, default scopes and the consent page all belong to a sign-in that never happens.
    A request with no scope is granted all of ``scopes``, so ``default_scopes`` would have nothing to narrow.
    """
    problems = [
        f'auth.mcp.clients.{client_id}.client_secret is required, since it is the only proof of who the agent is'
        for client_id, client in config.clients.items()
        if not client.client_secret
    ]
    problems += [
        f'auth.mcp.clients.{client_id}.{field} has no effect, since no user signs in'
        for client_id, client in config.clients.items()
        for field in ('redirect_uris', 'default_scopes', 'consent')
        if field in client.model_fields_set
    ]
    problems += [
        f'auth.mcp.clients.{client_id} names a Client ID Metadata Document, whose client signs a user in'
        for client_id in config.clients
        if is_url_client_id(client_id)
    ]
    if problems:
        raise ValueError(
            f'Server "{server_name}": agents here obtain tokens with their own credentials, and no user signs in. '
            + '. '.join(problems)
            + '.'
        )
