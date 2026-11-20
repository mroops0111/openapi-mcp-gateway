import collections.abc

from ..settings import ClientConfig, McpAuthConfig, OtherClientsConfig, is_url_client_id
from .mcp.client import (
    McpClientMetadataDirectory,
    McpOtherClients,
    McpPreRegisteredClient,
    McpPreRegisteredClientDirectory,
)
from .mcp.client_id import is_client_metadata_url
from .mcp.client_metadata import McpClientMetadataResolver
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
    """The clients ``auth.mcp.clients`` declares under a plain ``client_id``,
    each granted ``mcp_endpoint_scopes`` beside its own scopes.
    """
    return McpPreRegisteredClientDirectory(
        _build_pre_registered_client(client_id, client, mcp_endpoint_scopes)
        for client_id, client in clients.items()
        if not is_url_client_id(client_id)
    )


def build_mcp_client_metadata_directory(
    config: McpAuthConfig,
    mcp_endpoint_scopes: collections.abc.Sequence[str],
    resolver: McpClientMetadataResolver,
) -> McpClientMetadataDirectory:
    """The Client ID Metadata Document clients ``auth.mcp.clients`` lists by URL, and those ``other_clients`` admits,
    each granted ``mcp_endpoint_scopes`` beside its own scopes.
    """
    other_clients = config.resolve_other_clients()
    return McpClientMetadataDirectory(
        (
            _build_pre_registered_client(client_id, client, mcp_endpoint_scopes)
            for client_id, client in config.clients.items()
            if is_url_client_id(client_id)
        ),
        build_mcp_other_clients(other_clients, mcp_endpoint_scopes) if other_clients is not None else None,
        resolver,
    )


def build_mcp_other_clients(
    other_clients: OtherClientsConfig, mcp_endpoint_scopes: collections.abc.Sequence[str]
) -> McpOtherClients:
    """The rule ``auth.mcp.other_clients`` sets, granting ``mcp_endpoint_scopes`` beside its own scopes."""
    return McpOtherClients(
        scope=' '.join(dict.fromkeys([*mcp_endpoint_scopes, *other_clients.scopes])),
        default_scopes=list(dict.fromkeys([*mcp_endpoint_scopes, *other_clients.granted_by_default])),
        trusted_hosts=frozenset(other_clients.trusted_hosts) if other_clients.trusted_hosts is not None else None,
    )


def _build_pre_registered_client(
    client_id: str, client: ClientConfig, mcp_endpoint_scopes: collections.abc.Sequence[str]
) -> McpPreRegisteredClient:
    """The SDK's record of ``client``.

    A confidential client authenticates with HTTP Basic,
    the method RFC 6749 §2.3.1 requires every server to support and the one clients try first.
    A ``client_id`` that is a URL must be one a Client ID Metadata Document may sit at,
    and that client's secret and redirect URIs are its document's business.
    """
    _refuse_unusable_metadata_client(client_id, client)
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


def _refuse_unusable_metadata_client(client_id: str, client: ClientConfig) -> None:
    """Refuse a URL ``client_id`` no metadata document can sit at, and settings its document overrides."""
    if not is_url_client_id(client_id):
        return
    if not is_client_metadata_url(client_id):
        raise ValueError(
            f'auth.mcp.clients.{client_id} is not a Client ID Metadata Document URL, '
            'which is https with a path and no fragment, userinfo, or . and .. segments.'
        )
    for field, reason in (
        ('client_secret', 'the CIMD draft forbids a shared secret'),
        ('redirect_uris', 'the client lists them in its metadata document'),
    ):
        if field in client.model_fields_set:
            raise ValueError(f'auth.mcp.clients.{client_id}.{field} cannot be set, since {reason}.')
