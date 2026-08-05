import asyncio
import contextlib
import dataclasses
import logging
import typing
import warnings

import httpx
import uvicorn
from fastapi import FastAPI
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.caching import CacheableMethod, CacheHint
from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse

from .app import _ServerBundle, build_app, build_mcp_asgi_app, register_auth_routes
from .auth.detector import detect_oauth_flows, detect_unsupported_oauth_flows
from .auth.flows import AuthorizationCodeProvider, build_oauth_flow
from .auth.resolver import (
    AuthResolver,
    CompositeAuthResolver,
    NullAuthResolver,
    PassthroughAuthResolver,
    StaticAuthResolver,
)
from .exposure import MetaToolGenerator, ResourceGenerator, ToolGenerator, UpstreamBinding
from .fastapi import (
    collect_marked_routes,
    filter_marked_operations,
    infer_auth_from_declared_flows,
    override_with_metadata,
    warn_on_mixed_security_schemes,
)
from .openapi import McpIntegration, OpenAPISpec, OperationInfo, load_spec, parse_spec
from .policy import filter_operations
from .settings import AuthConfig, GatewayConfig, PolicyConfig, ServerConfig
from .stores import create_store


logger = logging.getLogger(__name__)


_FASTAPI_BASE_URL = 'http://fastapi.local'
_DEFAULT_FASTAPI_PASSTHROUGH_HEADERS: tuple[str, ...] = ('Authorization', 'X-API-Key')

# The list and discover results are static for a running gateway,
# so advertise a public freshness hint (2026-07-28 CacheableResult).
_STATIC_CACHE_TTL_MS = 300_000
_STATIC_CACHE_HINTS: dict[CacheableMethod, CacheHint] = {
    'tools/list': CacheHint(ttl_ms=_STATIC_CACHE_TTL_MS, scope='public'),
    'resources/list': CacheHint(ttl_ms=_STATIC_CACHE_TTL_MS, scope='public'),
    'resources/templates/list': CacheHint(ttl_ms=_STATIC_CACHE_TTL_MS, scope='public'),
    'server/discover': CacheHint(ttl_ms=_STATIC_CACHE_TTL_MS, scope='public'),
}


def _warn_if_deprecated_transport(transport: str) -> None:
    """Emit a deprecation warning when the caller selects the ``sse`` transport.

    HTTP+SSE was reclassified as Deprecated in the 2026-07-28 spec.
    The gateway still serves it for now,
    but steers callers to ``streamable-http`` and will remove it in a future release.
    """
    if transport == 'sse':
        message = (
            "The 'sse' transport is deprecated and will be removed in a future release. "
            'HTTP+SSE was reclassified as Deprecated in the MCP 2026-07-28 spec. '
            "Use 'streamable-http' instead."
        )
        warnings.warn(message, DeprecationWarning, stacklevel=2)
        logger.warning(message)


def _compose_with_passthrough(
    base: AuthResolver,
    forward_incoming_headers: tuple[str, ...],
) -> AuthResolver:
    """Forward ``forward_incoming_headers`` from the live MCP request alongside ``base``.

    ``base`` wins on collision, so gateway-minted credentials override the client's incoming token.
    """
    if not forward_incoming_headers:
        return base
    return CompositeAuthResolver([PassthroughAuthResolver(forward_incoming_headers), base])


def _resource_eligibility_problem(operation: OperationInfo) -> tuple[str, str] | None:
    """Return ``None`` if ``operation`` can be exposed as an MCP resource, else ``(reason, hint)``.

    Eligibility: method is ``GET`` and no required parameter outside the path.
    """
    if operation.method.lower() != 'get':
        return (
            f'method is {operation.method.upper()}',
            'Resources are read-only. Declare tool instead, or remove the resource declaration.',
        )
    required_non_path = [
        f'{parameter.location}:{parameter.name}'
        for parameter in operation.parameters
        if parameter.required and parameter.location != 'path'
    ]
    if required_non_path:
        return (
            f'has required non-path parameter(s) {required_non_path}',
            'URI templates can only carry path parameters. Make these optional, or expose as a tool.',
        )
    return None


def _validate_resource_eligibility(operation: OperationInfo, server_name: str) -> None:
    """Raise ``ValueError`` when an explicit ``resource`` opt-in cannot be honored.

    Covers both the basic eligibility rules and the ``uri_template`` scheme override,
    which only spec-side opt-ins can carry.
    """
    ineligibility = _resource_eligibility_problem(operation)
    if ineligibility is not None:
        reason, hint = ineligibility
        raise ValueError(
            f'Server "{server_name}": operation "{operation.operation_id}" declares '
            f'x-mcp-integration.resource but {reason}. {hint}'
        )

    override = operation.x_mcp_integration.resource
    if override and override.uri_template and not override.uri_template.startswith(f'{server_name}://'):
        raise ValueError(
            f'Server "{server_name}": operation "{operation.operation_id}".resource.uri_template '
            f'must start with "{server_name}://" (got "{override.uri_template}").'
        )


def _apply_yaml_overrides(
    operations: list[OperationInfo],
    yaml_overrides: dict[str, McpIntegration],
    server_name: str,
) -> list[OperationInfo]:
    """Return ``operations`` with the YAML-side ``operations.<id>`` override applied to each match.

    Each override fully replaces the spec-side ``x-mcp-integration`` for that operation; no merging is attempted.
    Raises ``ValueError`` if an ``operation_id`` in ``yaml_overrides`` is not exposed by this server.
    """
    if not yaml_overrides:
        return operations
    unmatched = set(yaml_overrides) - {operation.operation_id for operation in operations}
    if unmatched:
        raise ValueError(
            f'Server "{server_name}": YAML operations override targets unknown operation_id(s) {sorted(unmatched)!r}; '
            'check the spec or your allow / deny / marked_only policy.'
        )
    return [
        operation.model_copy(update={'x_mcp_integration': yaml_overrides[operation.operation_id]})
        if operation.operation_id in yaml_overrides
        else operation
        for operation in operations
    ]


def _partition_operations(
    operations: list[OperationInfo],
    server_name: str,
    mode: typing.Literal['tool_only', 'auto'] = 'tool_only',
) -> tuple[list[OperationInfo], list[OperationInfo]]:
    """Split ``operations`` into ``(resource_operations, tool_operations)`` based on ``mode`` and per-operation opt-ins.

    Under ``mode='tool_only'`` (default) every operation becomes a tool,
    and ``x-mcp-integration.resource`` declarations are ignored.

    Under ``mode='auto'`` an operation becomes a resource when it either declares ``resource`` explicitly,
    or qualifies for auto-promotion (no ``tool`` and the eligibility rules pass).
    Declaring both ``tool`` and ``resource`` registers the operation in both lists.
    """
    if mode == 'tool_only':
        return [], list(operations)

    resource_operations: list[OperationInfo] = []
    tool_operations: list[OperationInfo] = []
    for operation in operations:
        if operation.resource_exposed:
            _validate_resource_eligibility(operation, server_name)
            resource_operations.append(operation)
            if operation.tool_exposed:
                tool_operations.append(operation)
        elif not operation.tool_exposed and _resource_eligibility_problem(operation) is None:
            resource_operations.append(operation)
        else:
            tool_operations.append(operation)
    return resource_operations, tool_operations


@dataclasses.dataclass
class _AppContext:
    """Per-server lifespan context injected into MCPServer tool handlers."""

    auth_provider: AuthorizationCodeProvider | None = None


class Gateway:
    """Expose several OpenAPI backends as MCP servers in one process.

    Typical usage::

        gateway = Gateway()
        gateway.add_server(name="petstore", spec="petstore.json")
        gateway.run()

    Or load multiple servers from YAML::

        config = GatewayConfig.from_yaml("servers.yml")
        Gateway.from_config(config).run()
    """

    def __init__(self, config: GatewayConfig | None = None):
        self._config = config or GatewayConfig()
        self._servers: list[_ServerBundle] = []
        self._shutdown_hooks: list[typing.Callable[[], typing.Awaitable[None]]] = []
        store_config = self._config.store
        self._store = create_store(
            store_type=store_config.type,
            url=store_config.redis_url,
            prefix=store_config.key_prefix,
        )
        logger.debug('Token store initialised: type=%s prefix=%s', store_config.type, store_config.key_prefix)

    @classmethod
    def from_config(cls, config: GatewayConfig) -> typing.Self:
        """Build a gateway and register every entry in ``config.servers``."""
        gateway: typing.Self = cls(config=config)
        for server_config in config.servers:
            gateway._add_server_from_server_config(server_config=server_config)
        return gateway

    @classmethod
    def from_fastapi(
        cls,
        app: FastAPI,
        *,
        name: str = 'fastapi',
        path_prefix: str | None = None,
        auth: AuthConfig | None = None,
        timeout: float = 90,
        passthrough_headers: tuple[str, ...] = _DEFAULT_FASTAPI_PASSTHROUGH_HEADERS,
        config: GatewayConfig | None = None,
    ) -> typing.Self:
        """Expose ``@mcp_tool``-decorated routes of ``app`` in-process via ``httpx.ASGITransport``.

        Auth is auto-detected from the spec's ``securitySchemes``: no scheme gives no auth,
        ``client_credentials`` selects the service-token flow,
        ``authorization_code`` uses the full provider when ``client_id`` and ``client_secret`` are set,
        else it falls back to passthrough of the client's ``Authorization`` header.

        ``passthrough_headers`` copies headers verbatim from the live MCP call to the FastAPI route.
        The auth resolver wins on ``Authorization`` collision, so minted tokens take priority.
        """
        gateway: typing.Self = cls(config=config or GatewayConfig())
        gateway._add_fastapi_app(
            app=app,
            name=name,
            path_prefix=path_prefix,
            auth=auth,
            timeout=timeout,
            passthrough_headers=passthrough_headers,
        )
        return gateway

    def add_server(
        self,
        name: str,
        spec: str,
        base_url: str | None = None,
        path_prefix: str | None = None,
        auth: dict[str, typing.Any] | None = None,
        policy: dict[str, typing.Any] | None = None,
        timeout: float = 90,
        exposure: typing.Literal['static', 'dynamic'] = 'static',
    ) -> None:
        """Register a server inline (convenience over building ``ServerConfig`` directly)."""
        server_config = ServerConfig(
            name=name,
            spec=spec,
            base_url=base_url,
            path_prefix=path_prefix,
            auth=AuthConfig.model_validate(auth) if auth else AuthConfig(),
            policy=PolicyConfig.model_validate(policy) if policy else PolicyConfig(),
            timeout=timeout,
            exposure=exposure,
        )
        self._add_server_from_server_config(server_config=server_config)

    def run(
        self,
        transport: str | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        """Serve over ``uvicorn`` (HTTP/SSE) or stdio (single-server only).

        ``transport`` / ``host`` / ``port`` only override ``self._config`` when explicitly set,
        matching the precedence used by the CLI and YAML loader: non-None wins,
        otherwise the layered config value (which already accounts for ``--config`` and Pydantic defaults) stands.
        """
        transport = transport if transport is not None else self._config.transport
        host = host if host is not None else self._config.host
        port = port if port is not None else self._config.port
        _warn_if_deprecated_transport(transport)

        if transport == 'stdio':
            if len(self._servers) != 1:
                raise ValueError('stdio transport only supports a single server')
            logger.info('Starting MCP server "%s" over stdio', self._servers[0].name)
            try:
                self._servers[0].mcp.run(transport='stdio')
            finally:
                self._run_shutdown_hooks_blocking()
            return

        app = self._build_app(transport=transport)
        logger.info(
            'Starting gateway: transport=%s bind=%s:%d servers=%d',
            transport,
            host,
            port,
            len(self._servers),
        )
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=self._config.logging.level.lower(),
            log_config=None,
        )

    def mount(self, app: FastAPI, transport: str | None = None) -> None:
        """Mount every registered MCP sub-app onto ``app`` at its configured path.

        Also registers the OAuth and ``.well-known`` discovery routes those servers own,
        so an embedded gateway serves a complete OAuth flow.
        """
        transport = transport if transport is not None else self._config.transport
        _warn_if_deprecated_transport(transport)
        register_auth_routes(app, self._servers)
        for bundle in self._servers:
            app.mount(bundle.mount_path, build_mcp_asgi_app(bundle.mcp, transport))

    def _add_server_from_server_config(self, server_config: ServerConfig) -> None:
        logger.info('Loading server "%s" from spec=%s', server_config.name, server_config.spec)
        raw = load_spec(server_config.spec)
        spec = parse_spec(raw, source=server_config.spec)
        logger.debug(
            'Parsed spec for "%s": title=%r version=%r operations=%d',
            server_config.name,
            spec.title,
            spec.version,
            len(spec.operations),
        )

        base_url = server_config.base_url or spec.default_base_url
        if not base_url:
            raise ValueError(
                f'Server "{server_config.name}": no base_url provided and none found in OpenAPI spec. '
                'Set base_url in config or add a servers entry to your spec.'
            )

        operations = filter_operations(
            spec.operations,
            allow=server_config.policy.allow,
            deny=server_config.policy.deny,
            marked_only=server_config.policy.marked_only,
        )
        logger.debug(
            'Policy applied to "%s": %d → %d operations (allow=%s deny=%s marked_only=%s)',
            server_config.name,
            len(spec.operations),
            len(operations),
            server_config.policy.allow,
            server_config.policy.deny,
            server_config.policy.marked_only,
        )

        if not operations:
            raise ValueError(
                f'Server "{server_config.name}": no operations to expose after applying policy. '
                'Check your allow/deny rules or the OpenAPI spec.'
            )

        operations = _apply_yaml_overrides(operations, server_config.operations, server_config.name)

        self._register_server_bundle(
            server_config=server_config,
            spec=spec,
            operations=operations,
            base_url=base_url,
        )

    def _add_fastapi_app(
        self,
        app: FastAPI,
        *,
        name: str,
        path_prefix: str | None,
        auth: AuthConfig | None,
        timeout: float,
        passthrough_headers: tuple[str, ...],
    ) -> None:
        logger.info('Registering FastAPI app as MCP server "%s"', name)
        spec = parse_spec(app.openapi())

        declared = detect_oauth_flows(spec)
        unsupported = detect_unsupported_oauth_flows(spec)
        if unsupported and not declared:
            raise ValueError(
                f'FastAPI server "{name}" declares only unsupported OAuth2 flows: {unsupported}. '
                'Only authorizationCode and clientCredentials are supported.'
            )

        selections = collect_marked_routes(app)
        if not selections:
            raise ValueError(f'FastAPI server "{name}": no routes are decorated with @mcp_tool, nothing to expose.')

        marked = filter_marked_operations(spec.operations, selections)
        if not marked:
            raise ValueError(
                f'FastAPI server "{name}": @mcp_tool routes did not match any OpenAPI operations '
                '(check that the decorator is applied below the FastAPI route decorator).'
            )
        operations = [override_with_metadata(operation, metadata) for operation, metadata in marked]

        warn_on_mixed_security_schemes(name, operations)

        server_config = ServerConfig(
            name=name,
            spec=f'<fastapi:{name}>',
            path_prefix=path_prefix,
            auth=auth or infer_auth_from_declared_flows(declared),
            timeout=timeout,
        )

        self._register_server_bundle(
            server_config=server_config,
            spec=spec,
            operations=operations,
            base_url=_FASTAPI_BASE_URL,
            transport=httpx.ASGITransport(app=app),
            forward_incoming_headers=passthrough_headers,
        )

    def _register_server_bundle(
        self,
        *,
        server_config: ServerConfig,
        spec: OpenAPISpec,
        operations: list[OperationInfo],
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        forward_incoming_headers: tuple[str, ...] = (),
    ) -> None:
        base_resolver, auth_provider, auth_settings = self._resolve_auth(server_config, spec)
        auth_resolver = _compose_with_passthrough(base_resolver, forward_incoming_headers)
        mcp = self._build_mcp_server(server_config.name, auth_provider, auth_settings)
        if auth_provider is not None:
            self._register_oauth_callback(mcp, auth_provider)

        binding = UpstreamBinding(
            base_url=base_url,
            auth_resolver=auth_resolver,
            timeout=server_config.timeout,
            transport=transport,
        )
        resource_count = 0
        if server_config.exposure == 'dynamic':
            resource_optins = [op.operation_id for op in operations if op.resource_exposed]
            if resource_optins:
                logger.warning(
                    'Server "%s": dynamic exposure mode ignores x-mcp-integration.resource declarations '
                    'on %d operation(s) (%s). '
                    'The meta-tools surface every operation uniformly.',
                    server_config.name,
                    len(resource_optins),
                    ', '.join(resource_optins[:5]) + ('...' if len(resource_optins) > 5 else ''),
                )
            if server_config.mode == 'auto':
                logger.warning(
                    'Server "%s": dynamic exposure overrides mode=auto. '
                    'The meta-tools surface every operation uniformly, so resource promotion is skipped.',
                    server_config.name,
                )
            MetaToolGenerator(mcp=mcp, binding=binding).register(operations)
            tool_count = len(operations)
        else:
            resource_ops, tool_ops = _partition_operations(operations, server_config.name, server_config.mode)
            if resource_ops:
                ResourceGenerator(mcp=mcp, binding=binding, server_name=server_config.name).register(resource_ops)
            if tool_ops:
                ToolGenerator(mcp=mcp, binding=binding).register(tool_ops)
            resource_count = len(resource_ops)
            tool_count = len(tool_ops)

        self._servers.append(
            _ServerBundle(
                name=server_config.name,
                mount_path=server_config.mount_path,
                mcp=mcp,
                spec=spec,
                auth_provider=auth_provider,
                auth_settings=auth_settings,
            )
        )
        logger.info(
            'Registered server "%s": mount=%s base_url=%s tools=%d resources=%d auth=%s resolver=%s',
            server_config.name,
            server_config.mount_path,
            base_url,
            tool_count,
            resource_count,
            server_config.auth.type,
            type(auth_resolver).__name__,
        )

    def _resolve_auth(
        self,
        entry: ServerConfig,
        spec: OpenAPISpec,
    ) -> tuple[AuthResolver, AuthorizationCodeProvider | None, AuthSettings | None]:
        auth_provider: AuthorizationCodeProvider | None = None
        auth_resolver: AuthResolver
        auth_settings: AuthSettings | None = None

        if entry.auth.type == 'oauth2':
            setup = build_oauth_flow(
                entry=entry,
                spec=spec,
                store=self._store,
                gateway_url=self._config.url,
                mount_path=entry.mount_path,
            )
            auth_resolver = setup.resolver
            auth_provider = setup.provider
            auth_settings = setup.settings
            if setup.on_shutdown is not None:
                self._shutdown_hooks.append(setup.on_shutdown)
        elif entry.auth.type in ('bearer', 'api_key'):
            header_value = entry.auth.resolve_header()
            if header_value:
                auth_resolver = StaticAuthResolver(
                    header_value=header_value,
                    header_name=entry.auth.resolve_header_name(),
                )
            else:
                auth_resolver = NullAuthResolver()
        else:
            auth_resolver = NullAuthResolver()

        return auth_resolver, auth_provider, auth_settings

    @staticmethod
    def _build_mcp_server(
        name: str,
        auth_provider: AuthorizationCodeProvider | None,
        auth_settings: AuthSettings | None,
    ) -> MCPServer:
        @contextlib.asynccontextmanager
        async def lifespan(_app: MCPServer, _auth_provider=auth_provider):
            try:
                yield _AppContext(auth_provider=_auth_provider)
            finally:
                pass

        return MCPServer(
            f'{name} (via OpenAPI MCP Gateway)',
            auth_server_provider=auth_provider,
            auth=auth_settings,
            lifespan=lifespan,
            cache_hints=_STATIC_CACHE_HINTS,
        )

    @staticmethod
    def _register_oauth_callback(mcp: MCPServer, auth_provider: AuthorizationCodeProvider) -> None:
        _auth_provider = auth_provider

        @mcp.custom_route('/auth/callback', methods=['GET'])
        async def upstream_callback_handler(request, _provider=_auth_provider):
            code = request.query_params.get('code')
            state = request.query_params.get('state')
            if not code or not state:
                raise HTTPException(400, 'Missing code or state parameter')
            redirect_uri = await _provider.handle_upstream_callback(code, state)
            return RedirectResponse(status_code=302, url=redirect_uri)

    async def _run_shutdown_hooks(self) -> None:
        for hook in self._shutdown_hooks:
            try:
                await hook()
            except Exception:
                logger.exception('Shutdown hook raised an exception')

    def _run_shutdown_hooks_blocking(self) -> None:
        if not self._shutdown_hooks:
            return

        try:
            asyncio.run(self._run_shutdown_hooks())
        except RuntimeError:
            # Loop already running (unlikely on stdio shutdown); best-effort.
            logger.warning('Could not run shutdown hooks: event loop already running')

    def _build_app(self, transport: str) -> FastAPI:
        return build_app(
            servers=self._servers,
            config=self._config,
            store=self._store,
            on_shutdown=self._run_shutdown_hooks,
            transport=transport,
        )
