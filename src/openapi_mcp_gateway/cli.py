import importlib.metadata
import json
import logging
import typing

import click

from .gateway import Gateway
from .logger import FORMATS, LEVELS, setup
from .openapi import ExposedTool
from .settings import AuthConfig, GatewayConfig, build_gateway_config, single_spec_layer, yaml_layer


logger = logging.getLogger(__name__)

# How much of a server's instructions the dry-run table shows, enough to recognise them without wrapping the line.
_INSTRUCTIONS_PREVIEW_LENGTH = 60

# The entry point group through which an installed package adds a subcommand, as openapi-mcp-gateway-eval adds eval.
COMMAND_ENTRY_POINTS = 'openapi_mcp_gateway.commands'


def command_entry_points() -> importlib.metadata.EntryPoints:
    return importlib.metadata.entry_points(group=COMMAND_ENTRY_POINTS)


def build_auth_config(
    auth_upstream_type: typing.Literal['none', 'oauth2'] | None,
    auth_upstream_credentials: tuple[str, ...],
    auth_upstream_client_id: str | None,
    auth_upstream_client_secret: str | None,
    auth_upstream_scopes: str | None,
    auth_upstream_authorization_url: str | None,
    auth_upstream_token_url: str | None,
    auth_upstream_flow: typing.Literal['authorization_code', 'client_credentials'] | None,
) -> AuthConfig:
    """Construct ``AuthConfig`` from the auth-related CLI flags."""
    has_auth_flags = any(
        [
            auth_upstream_type,
            auth_upstream_credentials,
            auth_upstream_client_id,
            auth_upstream_client_secret,
            auth_upstream_scopes,
            auth_upstream_authorization_url,
            auth_upstream_token_url,
            auth_upstream_flow,
        ]
    )

    if not has_auth_flags:
        return AuthConfig()

    if not auth_upstream_type:
        if auth_upstream_client_id or auth_upstream_flow:
            auth_upstream_type = 'oauth2'
        elif auth_upstream_credentials:
            auth_upstream_type = 'none'
        else:
            raise click.UsageError(
                'Cannot infer --auth-upstream-type from the provided flags. Specify --auth-upstream-type explicitly.'
            )

    scopes = [scope.strip() for scope in auth_upstream_scopes.split(',')] if auth_upstream_scopes else []

    # Only flags the caller actually gave.
    # AuthConfig reads which fields were set to tell a decision from a default,
    # so passing None for an absent flag would read as a decision.
    upstream: dict[str, typing.Any] = {
        key: value
        for key, value in {
            'client_id': auth_upstream_client_id,
            'client_secret': auth_upstream_client_secret,
            'authorization_url': auth_upstream_authorization_url,
            'token_url': auth_upstream_token_url,
            'scopes': scopes or None,
        }.items()
        if value is not None
    }
    upstream['type'] = auth_upstream_type
    if auth_upstream_credentials:
        upstream['credentials'] = _parse_credentials(auth_upstream_credentials)
    if auth_upstream_flow is not None:
        upstream['flow'] = auth_upstream_flow
    return AuthConfig.model_validate({'upstream': upstream})


def instructions_label(instructions: str | None) -> str:
    """Render a server's instructions as their first line, marking anything left out."""
    if instructions is None:
        return 'none'
    first_line, _, rest = instructions.partition('\n')
    if len(first_line) > _INSTRUCTIONS_PREVIEW_LENGTH:
        return f'{first_line[:_INSTRUCTIONS_PREVIEW_LENGTH]}…'
    return f'{first_line} …' if rest else first_line


class _CommandGroup(click.Group):
    """The gateway's commands, its own and those installed packages add.

    An added command is loaded only when named, so a missing or broken package never stops ``serve``.
    """

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted({*super().list_commands(ctx), *(entry_point.name for entry_point in command_entry_points())})

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command
        for entry_point in command_entry_points():
            if entry_point.name == cmd_name:
                return entry_point.load()
        return None


@click.group(cls=_CommandGroup)
def main() -> None:
    """Mount OpenAPI APIs as MCP servers.

    \b
    Serve an API:
        omgw serve --spec api.json

    \b
    Measure what a config gains for a model, with openapi-mcp-gateway[eval] installed:
        omgw eval --help
    """


@main.command()
@click.option('--spec', type=str, default=None, help='Path or URL to a single OpenAPI spec.')
@click.option(
    '--config',
    'config_path',
    type=click.Path(exists=True),
    default=None,
    help=(
        'Path to a YAML config file with multiple servers. '
        'See https://mroops0111.github.io/openapi-mcp-gateway/reference/configuration/ for every field.'
    ),
)
@click.option(
    '--output',
    type=click.Choice(['text', 'json']),
    default='text',
    show_default=True,
    help='Format for the --dry-run summary. json emits the same facts for a program to consume.',
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
    help='MCP transport protocol, streamable-http by default, or stdio, or sse (deprecated). Overridable in --config.',
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
    '--auth-upstream-type',
    type=click.Choice(['none', 'oauth2']),
    default=None,
    help='Authentication type for the upstream API.',
)
@click.option(
    '--auth-upstream-credential',
    'auth_upstream_credentials',
    type=str,
    multiple=True,
    metavar='SCHEME=VALUE',
    help='Static credential for a security scheme the spec declares, as SCHEME=VALUE or SCHEME=${ENV_VAR}. Repeatable.',
)
@click.option(
    '--security-scheme',
    'security_schemes',
    type=str,
    multiple=True,
    metavar='NAME=JSON',
    help=(
        'Declare or replace a security scheme, as NAME and its OpenAPI scheme object in JSON, '
        'such as token={"type": "http", "scheme": "bearer"}. Repeatable.'
    ),
)
@click.option(
    '--security',
    'security',
    type=str,
    multiple=True,
    metavar='SCHEME[+SCHEME]',
    help=(
        "Replace the spec's root security with one alternative per flag, "
        'joining with + the schemes it needs together. Repeatable.'
    ),
)
@click.option(
    '--auth-upstream-client-id',
    type=str,
    default=None,
    help='OAuth2 client ID or ${ENV_VAR} reference.',
)
@click.option(
    '--auth-upstream-client-secret',
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
@click.option(
    '--auth-upstream-authorization-url', type=str, default=None, help='OAuth2 authorization URL (if not in spec).'
)
@click.option('--auth-upstream-token-url', type=str, default=None, help='OAuth2 token URL (if not in spec).')
@click.option(
    '--auth-upstream-flow',
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
def serve(
    spec: str | None,
    config_path: str | None,
    dry_run: bool,
    output: str,
    name: str,
    base_url: str | None,
    transport: typing.Literal['sse', 'streamable-http', 'stdio'] | None,
    host: str | None,
    port: int | None,
    auth_upstream_type: typing.Literal['none', 'oauth2'] | None,
    auth_upstream_credentials: tuple[str, ...],
    security_schemes: tuple[str, ...],
    security: tuple[str, ...],
    auth_upstream_client_id: str | None,
    auth_upstream_client_secret: str | None,
    auth_upstream_scopes: str | None,
    auth_upstream_authorization_url: str | None,
    auth_upstream_token_url: str | None,
    auth_upstream_flow: typing.Literal['authorization_code', 'client_credentials'] | None,
    log_level: str | None,
    log_format: str | None,
    log_file: str | None,
    verbose: bool,
    quiet: bool,
) -> None:
    """Serve the gateway from --spec or --config.

    \b
    Single spec (no auth):
        omgw serve --spec api.json

    \b
    Single spec with a static credential for the spec's bearerAuth scheme (via env var):
        omgw serve --spec api.json --auth-upstream-credential 'bearerAuth=${MY_API_TOKEN}'

    \b
    Single spec that declares no scheme, with a bearer token for every operation:
        omgw serve --spec api.json --security-scheme 'token={"type": "http", "scheme": "bearer"}' \\
            --security token --auth-upstream-credential 'token=${MY_API_TOKEN}'

    \b
    Single spec with OAuth2 (URLs detected from spec):
        omgw serve --spec api.json --auth-upstream-type oauth2 \\
            --auth-upstream-client-id '${CLIENT_ID}' --auth-upstream-client-secret '${CLIENT_SECRET}'

    \b
    Single spec with OAuth2 (URLs provided explicitly):
        omgw serve --spec api.json --auth-upstream-type oauth2 \\
            --auth-upstream-client-id '${CLIENT_ID}' --auth-upstream-client-secret '${CLIENT_SECRET}' \\
            --auth-upstream-authorization-url https://example.com/oauth/authorize \\
            --auth-upstream-token-url https://example.com/oauth/token

    \b
    Multiple servers via config:
        omgw serve --config config.yml

    \b
    stdio transport (Claude Desktop / IDE integration):
        omgw serve --spec api.json --transport stdio

    \b
    Verbose JSON logs written to a file:
        omgw serve --spec api.json -v --log-format json --log-file gateway.log

    \b
    Validate a config without serving (CI or an editor check):
        omgw serve --config config.yml --dry-run
    """
    if not spec and not config_path:
        raise click.UsageError('Either --spec or --config is required.')

    if verbose and quiet:
        raise click.UsageError('--verbose and --quiet are mutually exclusive.')

    if config_path:
        source_layer = yaml_layer(config_path)
    else:
        auth = build_auth_config(
            auth_upstream_type=auth_upstream_type,
            auth_upstream_credentials=auth_upstream_credentials,
            auth_upstream_client_id=auth_upstream_client_id,
            auth_upstream_client_secret=auth_upstream_client_secret,
            auth_upstream_scopes=auth_upstream_scopes,
            auth_upstream_authorization_url=auth_upstream_authorization_url,
            auth_upstream_token_url=auth_upstream_token_url,
            auth_upstream_flow=auth_upstream_flow,
        )
        source_layer = single_spec_layer(
            spec=typing.cast(str, spec),
            name=name,
            base_url=base_url,
            auth=auth,
            security_schemes=_parse_security_schemes(security_schemes),
            security=_parse_security(security) if security else None,
        )

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

    try:
        config = build_gateway_config(source_layer, cli_layer)
    except ValueError as error:
        _refuse_invalid_config(error)

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
            _refuse_invalid_config(error)
        _echo_dry_run_summary(gateway, config, output)
        return

    gateway = Gateway.from_config(config)
    gateway.run()


def _refuse_invalid_config(error: Exception) -> typing.NoReturn:
    """Print why the config is invalid and exit with status 1, with no traceback."""
    click.secho('✗ Invalid', fg='red', bold=True, err=True)
    click.echo(f'  {error}', err=True)
    raise SystemExit(1) from error


def _parse_credentials(credentials: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """Read each ``SCHEME=VALUE`` of ``--auth-upstream-credential`` into ``auth.upstream.credentials``."""
    parsed: dict[str, dict[str, str]] = {}
    for credential in credentials:
        scheme, separator, value = credential.partition('=')
        if not separator or not scheme:
            raise click.UsageError(f'--auth-upstream-credential takes SCHEME=VALUE, got {credential!r}.')
        parsed[scheme] = {'value': value}
    return parsed


def _parse_security_schemes(security_schemes: tuple[str, ...]) -> dict[str, dict[str, typing.Any]]:
    """Read each ``NAME=JSON`` of ``--security-scheme`` into ``security_schemes``."""
    parsed: dict[str, dict[str, typing.Any]] = {}
    for security_scheme in security_schemes:
        name, separator, document = security_scheme.partition('=')
        try:
            scheme = json.loads(document) if separator and name else None
        except json.JSONDecodeError:
            scheme = None
        if not isinstance(scheme, dict):
            raise click.UsageError(f'--security-scheme takes NAME=JSON with a JSON object, got {security_scheme!r}.')
        parsed[name] = scheme
    return parsed


def _parse_security(security: tuple[str, ...]) -> list[dict[str, list[str]]]:
    """Read each ``SCHEME[+SCHEME]`` of ``--security`` into one alternative of the root ``security``."""
    alternatives: list[dict[str, list[str]]] = []
    for alternative in security:
        schemes = [scheme.strip() for scheme in alternative.split('+')]
        if not all(schemes):
            raise click.UsageError(f'--security takes SCHEME or SCHEME+SCHEME, got {alternative!r}.')
        alternatives.append({scheme: [] for scheme in schemes})
    return alternatives


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
    click.echo(f'    {click.style(f"{label:<13}", dim=True)}  {value}')


def _shaping_label(tool: ExposedTool) -> str:
    """Render a tool's shaping as the short label the table column expects."""
    if not tool.shaping:
        return 'passthrough'
    parts = [tool.shaping['params']] if tool.shaping.get('params') else []
    parts += [key for key in ('request', 'response', 'media') if tool.shaping.get(key)]
    return ', '.join(parts)


def _dry_run_tool_table(tools: tuple[ExposedTool, ...]) -> None:
    """Print a server's tools as an aligned table with a dim header row."""
    labels = {tool.name: _shaping_label(tool) for tool in tools}
    name_width = max([len('NAME'), *(len(tool.name) for tool in tools)])
    method_width = max([len('METHOD'), *(len(tool.method) for tool in tools)])
    path_width = max([len('PATH'), *(len(tool.path) for tool in tools)])
    header = f'      {"NAME":<{name_width}}  {"METHOD":<{method_width}}  {"PATH":<{path_width}}  SHAPING'
    click.secho(header, dim=True)
    for tool in tools:
        click.echo(
            f'      {tool.name:<{name_width}}  {tool.method.upper():<{method_width}}  '
            f'{tool.path:<{path_width}}  {labels[tool.name]}'
        )


def _echo_dry_run_summary(gateway: Gateway, config: GatewayConfig, output: str = 'text') -> None:
    """Print a summary of what the config would serve, for a reader or for a program."""
    servers = gateway.describe_servers()
    if output == 'json':
        click.echo(json.dumps(gateway.describe(), indent=2))
        return
    total_tools = sum(len(server.tools) for server in servers)
    total_resources = sum(len(server.resource_names) for server in servers)
    counts = f'{len(servers)} server(s), {total_tools} tool(s), {total_resources} resource(s)'
    click.echo(f'{click.style("✓ Valid", fg="green", bold=True)}   {click.style(counts, dim=True)}')
    # Transport is process-wide, so printing it once per server implied a per-server setting,
    # and `ServerConfig` has no field for one.
    _dry_run_kv('transport', config.transport)
    for server in servers:
        click.echo('')
        click.secho(f'  {server.name}', bold=True)
        _dry_run_kv('mount', server.mount_path)
        _dry_run_kv('base url', server.base_url)
        _dry_run_kv('upstream auth', server.auth.upstream_description.summary)
        _dry_run_kv('mcp auth', server.auth.mcp_summary)
        _dry_run_kv('policy', server.policy_summary)
        _dry_run_kv('exposure', server.exposure)
        _dry_run_kv('instructions', instructions_label(server.instructions))
        if server.tools:
            _dry_run_kv('tools', str(len(server.tools)))
            _dry_run_tool_table(server.tools)
        if server.resource_names:
            _dry_run_kv('resources', ', '.join(server.resource_names))


if __name__ == '__main__':
    main()
