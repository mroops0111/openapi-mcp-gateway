import logging
import typing

import click

from .gateway import Gateway
from .logger import FORMATS, LEVELS, setup
from .openapi import ExposedTool
from .settings import AuthConfig, GatewayConfig, UpstreamAuthConfig, build_gateway_config, single_spec_layer, yaml_layer


logger = logging.getLogger(__name__)


@click.command()
@click.option('--spec', type=str, default=None, help='Path or URL to a single OpenAPI spec.')
@click.option(
    '--config',
    'config_path',
    type=click.Path(exists=True),
    default=None,
    help=(
        'Path to a YAML config file with multiple servers. See the README Configuration section '
        'for the full schema (policy, operations, and tool shaping).'
    ),
)
@click.option(
    '--dry-run',
    is_flag=True,
    default=False,
    help='Validate the config (load specs, apply policy, compile shaping) and exit without serving.',
)
@click.option('--name', type=str, default='api', help='Server name when using --spec (default: api).')
@click.option('--base-url', type=str, default=None, help='Override the upstream API base URL.')
@click.option(
    '--transport',
    type=click.Choice(['sse', 'streamable-http', 'stdio']),
    default=None,
    help='MCP transport protocol (default: streamable-http; overridable in --config).',
)
@click.option(
    '--host',
    type=str,
    default=None,
    help='Bind host (default: 0.0.0.0; overridable in --config).',
)
@click.option(
    '--port',
    type=int,
    default=None,
    help='Bind port (default: 8000; overridable in --config).',
)
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
@click.option(
    '--auth-upstream-scopes',
    type=str,
    default=None,
    help='Comma-separated scopes to request from the upstream authorization server.',
)
@click.option('--auth-authorization-url', type=str, default=None, help='OAuth2 authorization URL (if not in spec).')
@click.option('--auth-token-url', type=str, default=None, help='OAuth2 token URL (if not in spec).')
@click.option(
    '--auth-flow',
    type=click.Choice(['authorization_code', 'client_credentials']),
    default=None,
    help='OAuth2 flow when the spec declares more than one (defaults to authorization_code).',
)
@click.option(
    '--log-level',
    type=click.Choice(LEVELS, case_sensitive=False),
    default=None,
    help='Logging level (default: INFO, or value from --config).',
)
@click.option(
    '--log-format',
    type=click.Choice(FORMATS, case_sensitive=False),
    default=None,
    help='Log output format: text (human-readable) or json (structured).',
)
@click.option(
    '--log-file',
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help='Also write logs to this file (in addition to stderr).',
)
@click.option(
    '-v',
    '--verbose',
    is_flag=True,
    default=False,
    help='Shortcut for --log-level DEBUG.',
)
@click.option(
    '-q',
    '--quiet',
    is_flag=True,
    default=False,
    help='Shortcut for --log-level WARNING.',
)
def main(
    spec: str | None,
    config_path: str | None,
    dry_run: bool,
    name: str,
    base_url: str | None,
    transport: typing.Literal['sse', 'streamable-http', 'stdio'] | None,
    host: str | None,
    port: int | None,
    auth_type: typing.Literal['none', 'bearer', 'api_key', 'oauth2'] | None,
    auth_token: str | None,
    auth_client_id: str | None,
    auth_client_secret: str | None,
    auth_upstream_scopes: str | None,
    auth_authorization_url: str | None,
    auth_token_url: str | None,
    auth_flow: typing.Literal['authorization_code', 'client_credentials'] | None,
    log_level: str | None,
    log_format: str | None,
    log_file: str | None,
    verbose: bool,
    quiet: bool,
) -> None:
    """Run the gateway CLI: load config from ``--spec`` or ``--config``, then serve.

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
    stdio transport (Claude Desktop / IDE integration):
        openapi-mcp-gateway --spec petstore.json --transport stdio

    \b
    Verbose JSON logs written to a file:
        openapi-mcp-gateway --spec petstore.json -v --log-format json --log-file gateway.log

    \b
    Validate a config without serving (CI or an editor check):
        openapi-mcp-gateway --config servers.yml --dry-run
    """
    if not spec and not config_path:
        raise click.UsageError('Either --spec or --config is required.')

    if verbose and quiet:
        raise click.UsageError('--verbose and --quiet are mutually exclusive.')

    if config_path:
        source_layer = yaml_layer(config_path)
    else:
        auth = _build_auth_config(
            auth_type=auth_type,
            auth_token=auth_token,
            auth_client_id=auth_client_id,
            auth_client_secret=auth_client_secret,
            auth_upstream_scopes=auth_upstream_scopes,
            auth_authorization_url=auth_authorization_url,
            auth_token_url=auth_token_url,
            auth_flow=auth_flow,
        )
        source_layer = single_spec_layer(spec=typing.cast(str, spec), name=name, base_url=base_url, auth=auth)

    cli_layer = _cli_layer(
        host=host,
        port=port,
        transport=transport,
        log_level=log_level,
        log_format=log_format,
        log_file=log_file,
        verbose=verbose,
        quiet=quiet,
    )

    config = build_gateway_config(source_layer, cli_layer)

    setup(
        level=config.logging.level,
        format=config.logging.format,
        file=config.logging.file,
    )
    logger.debug(
        'CLI invoked transport=%s host=%s port=%s servers=%d',
        config.transport,
        config.host,
        config.port,
        len(config.servers),
    )

    if dry_run:
        try:
            gateway = Gateway.from_config(config)
        except Exception as error:
            click.secho('✗ Invalid', fg='red', bold=True, err=True)
            click.echo(f'  {error}', err=True)
            raise SystemExit(1) from error
        _echo_dry_run_summary(gateway, config)
        return

    gateway = Gateway.from_config(config)
    gateway.run()


def _build_auth_config(
    auth_type: typing.Literal['none', 'bearer', 'api_key', 'oauth2'] | None,
    auth_token: str | None,
    auth_client_id: str | None,
    auth_client_secret: str | None,
    auth_upstream_scopes: str | None,
    auth_authorization_url: str | None,
    auth_token_url: str | None,
    auth_flow: typing.Literal['authorization_code', 'client_credentials'] | None,
) -> AuthConfig:
    """Construct ``AuthConfig`` from the auth-related CLI flags."""
    has_auth_flags = any(
        [
            auth_type,
            auth_token,
            auth_client_id,
            auth_client_secret,
            auth_upstream_scopes,
            auth_authorization_url,
            auth_token_url,
            auth_flow,
        ]
    )

    if not has_auth_flags:
        return AuthConfig()

    if not auth_type:
        if auth_client_id or auth_flow:
            auth_type = 'oauth2'
        elif auth_token:
            auth_type = 'bearer'
        else:
            raise click.UsageError('Cannot infer --auth-type from the provided flags. Specify --auth-type explicitly.')

    scopes = [scope.strip() for scope in auth_upstream_scopes.split(',')] if auth_upstream_scopes else []

    return AuthConfig(
        type=auth_type,
        token=auth_token,
        flow=auth_flow,
        upstream=UpstreamAuthConfig(
            client_id=auth_client_id,
            client_secret=auth_client_secret,
            authorization_url=auth_authorization_url,
            token_url=auth_token_url,
            scopes=scopes,
        ),
    )


def _cli_layer(
    *,
    host: str | None,
    port: int | None,
    transport: typing.Literal['sse', 'streamable-http', 'stdio'] | None,
    log_level: str | None,
    log_format: str | None,
    log_file: str | None,
    verbose: bool,
    quiet: bool,
) -> dict[str, typing.Any]:
    """Translate CLI flags into a partial ``GatewayConfig`` dict (only fields the user set).

    ``-v`` and ``-q`` are resolved against ``--log-level`` here,
    so the rest of the pipeline only sees a single ``logging.level`` value alongside other layers.
    Unset flags are omitted entirely so they do not shadow earlier layers.
    """
    layer: dict[str, typing.Any] = {}
    if host is not None:
        layer['host'] = host
    if port is not None:
        layer['port'] = port
    if transport is not None:
        layer['transport'] = transport

    log_layer: dict[str, typing.Any] = {}
    if verbose:
        log_layer['level'] = 'DEBUG'
    elif quiet:
        log_layer['level'] = 'WARNING'
    elif log_level is not None:
        log_layer['level'] = log_level.upper()
    if log_format is not None:
        log_layer['format'] = log_format.lower()
    if log_file is not None:
        log_layer['file'] = log_file
    if log_layer:
        layer['logging'] = log_layer

    return layer


def _dry_run_kv(label: str, value: str) -> None:
    """Print one aligned, dim-labelled key/value line in the dry-run summary."""
    click.echo(f'    {click.style(f"{label:<9}", dim=True)}  {value}')


def _dry_run_tool_table(tools: tuple[ExposedTool, ...]) -> None:
    """Print a server's tools as an aligned table with a dim header row."""
    name_width = max([len('NAME'), *(len(tool.name) for tool in tools)])
    method_width = max([len('METHOD'), *(len(tool.method) for tool in tools)])
    path_width = max([len('PATH'), *(len(tool.path) for tool in tools)])
    header = f'      {"NAME":<{name_width}}  {"METHOD":<{method_width}}  {"PATH":<{path_width}}  SHAPING'
    click.secho(header, dim=True)
    for tool in tools:
        click.echo(
            f'      {tool.name:<{name_width}}  {tool.method.upper():<{method_width}}  '
            f'{tool.path:<{path_width}}  {tool.shaping}'
        )


def _echo_dry_run_summary(gateway: Gateway, config: GatewayConfig) -> None:
    """Print a structured, human-readable summary of what the config would serve."""
    servers = gateway.describe_servers()
    total_tools = sum(len(server.tools) for server in servers)
    total_resources = sum(len(server.resource_names) for server in servers)
    counts = f'{len(servers)} server(s), {total_tools} tool(s), {total_resources} resource(s)'
    click.echo(f'{click.style("✓ Valid", fg="green", bold=True)}   {click.style(counts, dim=True)}')
    for server in servers:
        click.echo('')
        click.secho(f'  {server.name}', bold=True)
        _dry_run_kv('mount', server.mount_path)
        _dry_run_kv('transport', config.transport)
        _dry_run_kv('base url', server.base_url)
        _dry_run_kv('auth', server.auth_summary)
        _dry_run_kv('exposure', server.exposure)
        if server.tools:
            _dry_run_kv('tools', str(len(server.tools)))
            _dry_run_tool_table(server.tools)
        if server.resource_names:
            _dry_run_kv('resources', ', '.join(server.resource_names))


if __name__ == '__main__':
    main()
