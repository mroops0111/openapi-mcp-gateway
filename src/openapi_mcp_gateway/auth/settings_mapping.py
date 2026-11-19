import collections.abc

from ..settings import ClientConfig, McpAuthConfig
from .mcp.client import McpPreRegisteredClient, McpPreRegisteredClientDirectory
from .mcp.token_issuer import MCP_DEFAULT_SCOPE, McpTokenPolicy


def build_mcp_token_policy(config: McpAuthConfig) -> McpTokenPolicy:
    """The policy ``auth.mcp`` describes for the tokens the gateway mints.

    Every token carries ``required_scopes``, or ``MCP_DEFAULT_SCOPE`` so the OAuth machinery has one to name.
    With ``operation_scopes`` there are none, since each operation names its own scopes instead.
    """
    scopes = [] if config.operation_scopes is not None else list(config.required_scopes) or [MCP_DEFAULT_SCOPE]
    return McpTokenPolicy(
        scopes=scopes, access_token_ttl=config.access_token_ttl, refresh_token_ttl=config.refresh_token_ttl
    )


def build_mcp_client_directory(
    clients: collections.abc.Mapping[str, ClientConfig], mcp_endpoint_scopes: collections.abc.Sequence[str]
) -> McpPreRegisteredClientDirectory:
    """The clients ``auth.mcp.clients`` declares, each granted ``mcp_endpoint_scopes`` beside its own scopes."""
    return McpPreRegisteredClientDirectory(
        _build_pre_registered_client(client_id, client, mcp_endpoint_scopes) for client_id, client in clients.items()
    )


def _build_pre_registered_client(
    client_id: str, client: ClientConfig, mcp_endpoint_scopes: collections.abc.Sequence[str]
) -> McpPreRegisteredClient:
    """The SDK's record of ``client``.

    A confidential client authenticates with HTTP Basic,
    the method RFC 6749 §2.3.1 requires every server to support and the one clients try first.
    """
    secret = client.resolve_client_secret()
    if client.client_secret is not None and not secret:
        raise ValueError(
            f'auth.mcp.clients.{client_id}.client_secret resolves to nothing, so the client would turn public. '
            'Set its environment variable, or remove client_secret to make the client public on purpose.'
        )
    return McpPreRegisteredClient(
        client_id=client_id,
        client_secret=secret,
        token_endpoint_auth_method='client_secret_basic' if secret else 'none',
        redirect_uris=client.redirect_uris,
        scope=' '.join(dict.fromkeys([*mcp_endpoint_scopes, *client.scopes])),
        default_scopes=list(dict.fromkeys([*mcp_endpoint_scopes, *client.granted_by_default])),
        consent_required=client.consent,
    )
