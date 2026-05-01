import typing

import click

from .gateway import Gateway
from .settings import AuthConfig, GatewayConfig


@click.command()
@click.option('--spec', type=str, default=None, help='Path or URL to a single OpenAPI spec.')
@click.option(
    '--config',
    'config_path',
    type=click.Path(exists=True),
    default=None,
    help='Path to a YAML config file with multiple servers.',
)
@click.option('--name', type=str, default='api', help='Server name when using --spec (default: api).')
@click.option('--base-url', type=str, default=None, help='Override the upstream API base URL.')
@click.option(
    '--transport',
    type=click.Choice(['sse', 'streamable-http', 'stdio']),
    default='streamable-http',
    help='MCP transport protocol.',
)
@click.option('--host', type=str, default='0.0.0.0', help='Bind host.')
@click.option('--port', type=int, default=8000, help='Bind port.')
@click.option(
    '--auth-type',
    type=click.Choice(['none', 'bearer', 'api_key', 'oauth2']),
    default=None,
    help='Authentication type for the upstream API.',
)
@click.option(
    '--auth-token',
    type=str,
    default=None,
    help='Static token or ${ENV_VAR} reference (for bearer / api_key auth).',
)
@click.option(
    '--auth-client-id',
    type=str,
    default=None,
    help='OAuth2 client ID or ${ENV_VAR} reference.',
)
@click.option(
    '--auth-client-secret',
    type=str,
    default=None,
    help='OAuth2 client secret or ${ENV_VAR} reference.',
)
@click.option('--auth-scopes', type=str, default=None, help='Comma-separated upstream OAuth2 scopes.')
@click.option('--auth-authorization-url', type=str, default=None, help='OAuth2 authorization URL (if not in spec).')
@click.option('--auth-token-url', type=str, default=None, help='OAuth2 token URL (if not in spec).')
def main(
    spec: str | None,
    config_path: str | None,
    name: str,
    base_url: str | None,
    transport: typing.Literal['sse', 'streamable-http', 'stdio'],
    host: str,
    port: int,
    auth_type: typing.Literal['none', 'bearer', 'api_key', 'oauth2'] | None,
    auth_token: str | None,
    auth_client_id: str | None,
    auth_client_secret: str | None,
    auth_scopes: str | None,
    auth_authorization_url: str | None,
    auth_token_url: str | None,
) -> None:
    """Turn any OpenAPI specification into an MCP server.

    \b
    Single spec (no auth):
        openapi-mcp-gateway --spec petstore.json

    \b
    Single spec with bearer token (via env var):
        openapi-mcp-gateway --spec api.json --auth-type bearer --auth-token '${MY_API_TOKEN}'

    \b
    Single spec with OAuth2 (URLs detected from spec):
        openapi-mcp-gateway --spec api.json --auth-type oauth2 \\
            --auth-client-id '${CLIENT_ID}' --auth-client-secret '${CLIENT_SECRET}'

    \b
    Single spec with OAuth2 (URLs provided explicitly):
        openapi-mcp-gateway --spec api.json --auth-type oauth2 \\
            --auth-client-id '${CLIENT_ID}' --auth-client-secret '${CLIENT_SECRET}' \\
            --auth-authorization-url https://example.com/oauth/authorize \\
            --auth-token-url https://example.com/oauth/token

    \b
    Multiple servers via config:
        openapi-mcp-gateway --config servers.yml

    \b
    stdio transport (for Claude Desktop / IDE integration):
        openapi-mcp-gateway --spec petstore.json --transport stdio
    """
    if not spec and not config_path:
        raise click.UsageError('Either --spec or --config is required.')

    if config_path:
        config = GatewayConfig.from_yaml(config_path)
        config.transport = transport
        config.host = host
        config.port = port
    elif spec:
        auth = _build_auth_config(
            auth_type=auth_type,
            auth_token=auth_token,
            auth_client_id=auth_client_id,
            auth_client_secret=auth_client_secret,
            auth_scopes=auth_scopes,
            auth_authorization_url=auth_authorization_url,
            auth_token_url=auth_token_url,
        )
        config = GatewayConfig.from_single_spec(
            spec=spec,
            name=name,
            base_url=base_url,
            auth=auth,
            transport=transport,
            host=host,
            port=port,
        )
    else:
        raise click.UsageError('Either --spec or --config is required.')

    gateway = Gateway.from_config(config)
    gateway.run()


def _build_auth_config(
    auth_type: typing.Literal['none', 'bearer', 'api_key', 'oauth2'] | None,
    auth_token: str | None,
    auth_client_id: str | None,
    auth_client_secret: str | None,
    auth_scopes: str | None,
    auth_authorization_url: str | None,
    auth_token_url: str | None,
) -> AuthConfig:
    """Build an AuthConfig from CLI flags."""
    has_auth_flags = any(
        [
            auth_type,
            auth_token,
            auth_client_id,
            auth_client_secret,
            auth_scopes,
            auth_authorization_url,
            auth_token_url,
        ]
    )

    if not has_auth_flags:
        return AuthConfig()

    if not auth_type:
        if auth_client_id:
            auth_type = 'oauth2'
        elif auth_token:
            auth_type = 'bearer'
        else:
            raise click.UsageError('Cannot infer --auth-type from the provided flags. Specify --auth-type explicitly.')

    scopes = [s.strip() for s in auth_scopes.split(',')] if auth_scopes else []

    return AuthConfig(
        type=auth_type,
        token=auth_token,
        client_id=auth_client_id,
        client_secret=auth_client_secret,
        authorization_url=auth_authorization_url,
        token_url=auth_token_url,
        scopes=scopes,
    )


if __name__ == '__main__':
    main()
