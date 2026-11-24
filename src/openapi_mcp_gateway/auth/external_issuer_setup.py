import dataclasses

import pydantic
from mcp.server.auth.settings import AuthSettings
from mcp.shared.auth import ProtectedResourceMetadata

from ..settings import AuthConfig
from .assembly import AuthTypeContext, McpSetting, ServerAuthSetup
from .mcp.external_issuer import (
    McpExternalIssuerMetadata,
    McpExternalIssuerTokenVerifier,
    fetch_mcp_external_issuer_metadata,
)


class McpExternalIssuerSetup:
    """The MCP half of an endpoint whose tokens an authorization server the gateway does not own mints.

    The gateway only verifies those tokens, as a resource server, and the issuer alone decides what each one holds.
    ``McpExternalIssuerAuthority`` and the ``token_exchange`` flow both build their MCP half with it,
    so the ``auth.mcp`` fields it reads and the rules it holds them to are stated once.
    """

    @property
    def mcp_settings(self) -> frozenset[McpSetting]:
        return frozenset({'issuer', 'required_scopes', 'operation_scopes'})

    def refuse_unusable_settings(self, auth: AuthConfig, server_name: str) -> None:
        """Refuse an issuer whose clients would be told no scope to ask for first.

        A client reads ``scopes_supported`` from the protected resource metadata, and ``required_scopes`` is what that lists.
        With none listed, the MCP SDK falls back to every scope the issuer advertises,
        which the issuer refuses for any client it restricts.
        """
        if auth.mcp.resolve_issuer() and not auth.mcp.required_scopes:
            raise ValueError(
                f'Server "{server_name}": auth.mcp.issuer needs auth.mcp.required_scopes, '
                'the scopes every client asks the issuer for first. '
                'Without them the protected resource metadata lists no scopes_supported, '
                'and the MCP SDK then asks for every scope the issuer advertises, '
                'which the issuer refuses for any client it restricts.'
            )

    def discover(self, issuer: str) -> McpExternalIssuerMetadata:
        """The metadata ``issuer`` publishes, fetched at startup."""
        return fetch_mcp_external_issuer_metadata(issuer)

    def attach(
        self, setup: ServerAuthSetup, context: AuthTypeContext, metadata: McpExternalIssuerMetadata
    ) -> ServerAuthSetup:
        """Return ``setup`` with the MCP half that verifies the tokens the issuer behind ``metadata`` mints."""
        # The canonical URI of this MCP server, as RFC 8707 §2 defines it.
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
