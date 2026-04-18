"""CLI entry point for the OpenAPI MCP Gateway."""

import click

from .gateway import Gateway
from .settings import GatewayConfig


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
def main(
    spec: str | None,
    config_path: str | None,
    name: str,
    base_url: str | None,
    transport: str,
    host: str,
    port: int,
) -> None:
    """Turn any OpenAPI specification into an MCP server.

    \b
    Single spec:
        openapi-mcp-gateway --spec petstore.json
        openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json

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
        # CLI flags override config file values
        config.transport = transport
        config.host = host
        config.port = port
    else:
        config = GatewayConfig.from_single_spec(
            spec=spec,
            name=name,
            base_url=base_url,
            transport=transport,
            host=host,
            port=port,
        )

    gateway = Gateway.from_config(config)
    gateway.run()


if __name__ == '__main__':
    main()
