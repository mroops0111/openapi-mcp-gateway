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
from mcp.server.auth.provider import OAuthAuthorizationServerProvider
from mcp.server.caching import CacheableMethod, CacheHint
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .app import _ServerBundle, build_app, mount_mcp_apps, register_auth_routes, serve_lifespan
from .auth.assembly import AuthTypeContext, ServerAuthSetup
from .auth.factory import build_auth
from .auth.mcp.client_metadata import McpClientMetadataResolver
from .auth.upstream.oauth_flow import detect_unsupported_upstream_oauth_flows, detect_upstream_oauth_flows
from .auth.upstream.resolver import (
    UpstreamAuthResolver,
    UpstreamCompositeAuthResolver,
    UpstreamPassthroughAuthResolver,
)
from .exposure import (
    DynamicOperationTarget,
    MetaToolGenerator,
    ResourceGenerator,
    SemanticSearchOptions,
    ToolGenerator,
    UpstreamConnection,
    build_embedder,
    embeddings_available,
)
from .fastapi import (
    collect_marked_routes,
    filter_marked_operations,
    infer_auth_from_declared_flows,
    override_with_metadata,
    warn_on_mixed_security_schemes,
)
from .openapi import ExposedTool, OpenAPISpec, OperationInfo, load_spec, parse_spec
from .policy import filter_operations, unmatched_patterns
from .scopes import (
    McpScopePolicy,
    NameHeaderTarget,
    RequestTarget,
    ScopeFilterMiddleware,
    ScopeRegistry,
    UpstreamSpecSecurityScopePolicy,
    build_mcp_scope_policy,
    build_upstream_scope_policy,
)
from .settings import (
    AuthConfig,
    ExposureConfig,
    GatewayConfig,
    OperationOverride,
    PolicyConfig,
    SemanticSearchConfig,
    ServerConfig,
)
from .stores import create_store


logger = logging.getLogger(__name__)


_FASTAPI_BASE_URL = 'http://fastapi.local'
_DEFAULT_FASTAPI_PASSTHROUGH_HEADERS: tuple[str, ...] = ('Authorization', 'X-API-Key')

# The list and discover results are static for a running gateway,
# so advertise a freshness hint (2026-07-28 CacheableResult).
_STATIC_CACHE_TTL_MS = 300_000


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

    def __init__(
        self, config: GatewayConfig | None = None, *, client_metadata_resolver: McpClientMetadataResolver | None = None
    ):
        self._config = config or GatewayConfig()
        # One resolver serves every server, so a client's metadata document is fetched once for all of them.
        self._client_metadata_resolver = client_metadata_resolver or McpClientMetadataResolver()
        self._servers: list[_ServerBundle] = []
        self._shutdown_hooks: list[typing.Callable[[], typing.Awaitable[None]]] = []
        # The session managers ``mount`` created, which only the host app's lifespan can start.
        self._session_managers: list[StreamableHTTPSessionManager] = []
        store_config = self._config.store
        self._store = create_store(
            store_type=store_config.type,
            url=store_config.redis_url,
            prefix=store_config.key_prefix,
        )
        logger.debug('Token store initialised: type=%s prefix=%s', store_config.type, store_config.key_prefix)

    @classmethod
    def from_config(
        cls, config: GatewayConfig, *, client_metadata_resolver: McpClientMetadataResolver | None = None
    ) -> typing.Self:
        """Build a gateway and register every entry in ``config.servers``.

        ``client_metadata_resolver`` replaces how Client ID Metadata Documents are fetched, as tests need.
        """
        gateway: typing.Self = cls(config=config, client_metadata_resolver=client_metadata_resolver)
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

        Auth is auto-detected from the spec's ``securitySchemes``.
        No scheme gives no auth, and any supported OAuth flow passes the client's ``Authorization`` header through.
        An explicit ``auth`` overrides it, such as one with credentials for the gateway to mint upstream tokens.

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
        exposure: dict[str, typing.Any] | None = None,
        headers: dict[str, str] | None = None,
        instructions: str | None = None,
    ) -> None:
        """Register a server inline (convenience over building ``ServerConfig`` directly).

        ``auth``, ``policy``, ``exposure`` and ``headers`` take the same mappings their YAML keys do.
        """
        server_config = ServerConfig(
            name=name,
            spec=spec,
            base_url=base_url,
            path_prefix=path_prefix,
            auth=AuthConfig.model_validate(auth) if auth else AuthConfig(),
            policy=PolicyConfig.model_validate(policy) if policy else PolicyConfig(),
            timeout=timeout,
            exposure=ExposureConfig.model_validate(exposure) if exposure else ExposureConfig(),
            headers=headers or {},
            instructions=instructions,
        )
        self._add_server_from_server_config(server_config=server_config)

    def describe_servers(self) -> tuple[_ServerBundle, ...]:
        """Return the registered server bundles, for a dry-run summary of what would be served."""
        return tuple(self._servers)

    def describe(self) -> dict[str, typing.Any]:
        """Return what this gateway would serve, as plain JSON-serialisable data.

        The stable counterpart to ``--dry-run --output json``, which emits exactly this document.
        Prefer it over ``describe_servers``, whose bundle type is internal and whose shape is not a promise.
        Every name here is the one the model is shown, so a caller reads the same strings the model will.
        """
        return {
            'transport': self._config.transport,
            'servers': [server.describe() for server in self.describe_servers()],
        }

    def run(
        self,
        transport: str | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        """Serve over ``uvicorn`` (HTTP/SSE) or stdio (single-server only).

        ``transport`` / ``host`` / ``port`` only override ``self._config`` when explicitly set,
        matching the precedence used by the CLI and YAML loader, where non-None wins,
        otherwise the layered config value (which already accounts for ``--config`` and Pydantic defaults) stands.
        """
        transport = transport if transport is not None else self._config.transport
        host = host if host is not None else self._config.host
        port = port if port is not None else self._config.port
        _warn_if_deprecated_transport(transport)

        if transport == 'stdio':
            if len(self._servers) != 1:
                raise ValueError('stdio transport only supports a single server')
            server = self._servers[0]
            # A stdio client launches the gateway itself and presents no token, so a check on one would refuse it.
            if server.auth_provider is not None or server.token_verifier is not None:
                raise ValueError(
                    f'Server "{server.name}" checks MCP tokens, which a stdio client never presents. '
                    'Serve it over streamable-http, or drop the auth.mcp settings and auth.upstream.flow that need them.'
                )
            logger.info('Starting MCP server "%s" over stdio', self._servers[0].name)
            try:
                self._servers[0].mcp.run(transport='stdio')
            finally:
                self._run_shutdown_hooks_blocking()
            return

        app = self._build_app(transport=transport, host=host)
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
        The host app must enter ``lifespan`` in its own lifespan, or every MCP request fails.
        """
        transport = transport if transport is not None else self._config.transport
        _warn_if_deprecated_transport(transport)
        register_auth_routes(app, self._servers)
        self._session_managers = mount_mcp_apps(
            app, self._servers, transport, self._config.dns_rebinding_protection, host=None
        )

    def lifespan(self) -> contextlib.AbstractAsyncContextManager[None]:
        """Run a mounted gateway for as long as the host app serves, by entering this in the host app's lifespan.

        ``mount`` has no hold on the host app's lifespan, so without this every MCP request to a mounted server fails.
        On exit it runs the shutdown hooks and closes the token store, as ``run`` does.
        """
        return serve_lifespan(self._session_managers, self._store, self._run_shutdown_hooks)

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
            annotated_only=server_config.policy.annotated_only,
        )
        logger.debug(
            'Policy applied to "%s": %d → %d operations (allow=%s deny=%s annotated_only=%s)',
            server_config.name,
            len(spec.operations),
            len(operations),
            server_config.policy.allow,
            server_config.policy.deny,
            server_config.policy.annotated_only,
        )

        if not operations:
            raise ValueError(
                f'Server "{server_config.name}": no operations to expose after applying policy. '
                'Check your allow/deny rules or the OpenAPI spec.'
            )

        operations = _apply_yaml_overrides(
            operations, server_config.operations, server_config.name, spec.security_schemes
        )

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

        declared = detect_upstream_oauth_flows(spec)
        unsupported = detect_unsupported_upstream_oauth_flows(spec)
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
        mcp_scope_policy = build_mcp_scope_policy(
            server_config.auth.mcp.operation_scopes, operations, server_config.name
        )
        _refuse_undefined_mcp_scopes(server_config, mcp_scope_policy)
        upstream = server_config.auth.upstream
        spec_security = UpstreamSpecSecurityScopePolicy(spec.security_schemes, upstream.scope_implies)
        if upstream.enforce_scopes and not upstream.scopes:
            server_config = _with_requested_upstream_scopes(server_config, spec_security.declared_scopes(operations))
        scope_registry = ScopeRegistry(
            mcp_scope_policy,
            build_upstream_scope_policy(upstream.enforce_scopes, spec_security),
        )
        auth = self._resolve_auth(server_config, spec, scope_registry)
        operations = _check_upstream_grant(server_config, auth, spec_security, operations, mcp_scope_policy)
        auth_resolver = _compose_with_passthrough(auth.resolver, forward_incoming_headers)
        mcp = self._build_mcp_server(server_config.name, auth, server_config.instructions, scope_registry)
        for routes in auth.routes:
            routes.register(mcp)

        connection = UpstreamConnection(
            base_url=base_url,
            auth_resolver=auth_resolver,
            reauthorizer=auth.reauthorizer,
            timeout=server_config.timeout,
            transport=transport,
            headers=server_config.resolve_headers(),
        )
        self._shutdown_hooks.append(connection.client.aclose)
        exposed_tools: list[ExposedTool] = []
        # A policy pattern that matches no operation at all is almost always a typo,
        # and the result is only ever a shorter list that nobody notices is short.
        # Warned at load rather than reported in the document, so that it reaches every caller.
        stray_patterns = unmatched_patterns(spec.operations, server_config.policy.allow, server_config.policy.deny)
        if stray_patterns:
            logger.warning(
                'Server "%s": %d policy pattern(s) matched no operation (%s). '
                'Check them against the operationIds in the spec.',
                server_config.name,
                len(stray_patterns),
                ', '.join(stray_patterns),
            )

        resource_names: list[str] = []
        tool_call_target: RequestTarget = NameHeaderTarget()
        _warn_on_ignored_resource_declarations(server_config, operations)
        if server_config.exposure.style == 'dynamic':
            search = server_config.exposure.search
            exposed_tools = MetaToolGenerator(
                mcp=mcp,
                connection=connection,
                field_weight_overrides=search.field_weights.model_dump(exclude_none=True) if search else None,
                scope_registry=scope_registry,
                semantic_search=_semantic_search_options(server_config.name, search.semantic if search else None),
                grant_source=auth.upstream_grant_source,
            ).register(operations)
            tool_call_target = DynamicOperationTarget()
        else:
            resource_ops, tool_ops = _partition_operations(
                operations, server_config.name, server_config.exposure.promote_resources
            )
            if resource_ops:
                resource_names = ResourceGenerator(
                    mcp=mcp, connection=connection, server_name=server_config.name, scope_registry=scope_registry
                ).register(resource_ops)
            if tool_ops:
                exposed_tools = ToolGenerator(mcp=mcp, connection=connection, scope_registry=scope_registry).register(
                    tool_ops
                )

        self._servers.append(
            _ServerBundle(
                name=server_config.name,
                mount_path=server_config.mount_path,
                mcp=mcp,
                spec=spec,
                auth_provider=auth.provider,
                auth_settings=auth.settings,
                authorization_server_metadata=auth.authorization_server_metadata,
                token_verifier=auth.verifier,
                protected_resource=auth.protected_resource,
                scope_registry=scope_registry,
                tool_call_target=tool_call_target,
                base_url=base_url,
                upstream_auth_summary=auth.upstream_summary,
                mcp_auth_summary=auth.mcp_summary,
                auth_type=server_config.auth.upstream.type,
                auth_api_key_header=auth.upstream_api_key_header,
                auth_flow=auth.flow_type,
                policy_summary=_policy_summary(server_config.policy),
                exposure=server_config.exposure.style,
                instructions=server_config.instructions,
                tools=tuple(exposed_tools),
                resource_names=tuple(resource_names),
            )
        )
        logger.info(
            'Registered server "%s": mount=%s base_url=%s tools=%d resources=%d upstream_auth=%s mcp_auth=%s',
            server_config.name,
            server_config.mount_path,
            base_url,
            len(exposed_tools),
            len(resource_names),
            _format_log_label(auth.upstream_summary),
            _format_log_label(auth.mcp_summary),
        )

    def _resolve_auth(
        self,
        entry: ServerConfig,
        spec: OpenAPISpec,
        scope_registry: ScopeRegistry,
    ) -> ServerAuthSetup:
        """Return the auth components for ``entry``, dispatching on ``auth.upstream.type``.

        Every type produces the same shape, so callers never branch on which one ran.
        Registering the shutdown hook is the gateway's business rather than a handler's,
        since only the gateway owns the process lifecycle.
        """
        setup = build_auth(
            AuthTypeContext(
                entry=entry,
                spec=spec,
                store=self._store,
                gateway_url=self._config.url,
                scope_registry=scope_registry,
                client_metadata_resolver=self._client_metadata_resolver,
            )
        )
        if setup.on_shutdown is not None:
            self._shutdown_hooks.append(setup.on_shutdown)
        return setup

    @staticmethod
    def _build_mcp_server(
        name: str,
        auth: ServerAuthSetup,
        instructions: str | None,
        scope_registry: ScopeRegistry,
    ) -> MCPServer:
        """Build the MCP server, wiring whichever of provider or verifier ``auth`` holds, and its extensions.

        The SDK rejects a server given both,
        since issuing tokens and validating someone else's are alternatives rather than layers.
        ``scope_registry`` decides which tools each token sees and may call.
        """

        @contextlib.asynccontextmanager
        async def lifespan(_app: MCPServer, _auth_provider=auth.provider):
            try:
                yield _AppContext(auth_provider=_auth_provider)
            finally:
                pass

        return MCPServer(
            f'{name} (via OpenAPI MCP Gateway)',
            auth_server_provider=auth.provider,
            token_verifier=auth.verifier,
            auth=auth.settings,
            extensions=auth.extensions,
            instructions=instructions,
            lifespan=lifespan,
            cache_hints=_cache_hints(scope_registry),
            middleware=[ScopeFilterMiddleware(scope_registry, auth.upstream_grant_source)],
        )

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
            # Loop already running (unlikely on stdio shutdown), so this is best-effort.
            logger.warning('Could not run shutdown hooks: event loop already running')

    def _build_app(self, transport: str, host: str | None = None) -> FastAPI:
        return build_app(
            servers=self._servers,
            config=self._config,
            store=self._store,
            on_shutdown=self._run_shutdown_hooks,
            transport=transport,
            host=host if host is not None else self._config.host,
        )


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
    base: UpstreamAuthResolver,
    forward_incoming_headers: tuple[str, ...],
) -> UpstreamAuthResolver:
    """Forward ``forward_incoming_headers`` from the live MCP request alongside ``base``.

    ``base`` wins on collision, so gateway-minted credentials override the client's incoming token.
    """
    if not forward_incoming_headers:
        return base
    return UpstreamCompositeAuthResolver([UpstreamPassthroughAuthResolver(forward_incoming_headers), base])


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
    yaml_overrides: dict[str, OperationOverride],
    server_name: str,
    security_schemes: dict[str, typing.Any],
) -> list[OperationInfo]:
    """Return ``operations`` with the YAML-side ``operations.<id>`` override applied to each match.

    Each override fully replaces the spec-side ``x-mcp-integration`` for that operation, with no merging,
    unless it overrides ``security`` alone.
    A ``security`` override replaces the spec's ``security`` for that operation.
    Raises ``ValueError`` if an ``operation_id`` in ``yaml_overrides`` is not exposed by this server,
    or if a ``security`` override names a scheme the spec does not declare.
    """
    if not yaml_overrides:
        return operations
    unmatched = set(yaml_overrides) - {operation.operation_id for operation in operations}
    if unmatched:
        raise ValueError(
            f'Server "{server_name}": YAML operations override targets unknown operation_id(s) {sorted(unmatched)!r}; '
            'check the spec or your allow / deny / annotated_only policy.'
        )
    for operation_id, override in yaml_overrides.items():
        undeclared = sorted(
            {scheme for requirement in override.security or () for scheme in requirement} - set(security_schemes)
        )
        if undeclared:
            raise ValueError(
                f'Server "{server_name}": operations.{operation_id}.security names schemes the spec does not declare '
                f'{undeclared!r}; use the names under components.securitySchemes.'
            )
    return [
        _apply_yaml_override(operation, yaml_overrides[operation.operation_id])
        if operation.operation_id in yaml_overrides
        else operation
        for operation in operations
    ]


def _apply_yaml_override(operation: OperationInfo, override: OperationOverride) -> OperationInfo:
    """Return ``operation`` with what ``override`` sets replaced."""
    update: dict[str, typing.Any] = {}
    if override.overrides_integration:
        update['x_mcp_integration'] = override.integration
    if override.security is not None:
        update['security'] = override.security
    return operation.model_copy(update=update)


def _check_upstream_grant(
    server_config: ServerConfig,
    auth: ServerAuthSetup,
    spec_security: UpstreamSpecSecurityScopePolicy,
    operations: list[OperationInfo],
    mcp_scope_policy: McpScopePolicy,
) -> list[OperationInfo]:
    """Return the operations to expose, once ``auth`` shows whether the flow records a user grant.

    Raises ``ValueError`` when ``enforce_scopes`` leaves no operation any sign-in can open.
    A flow that records no user grant never reads ``enforce_scopes``, so ``build_auth`` has already refused it.
    Under ``enforce_scopes``, an operation no sign-in can open is left out, since every call to it is refused.
    Without it the gateway does not check the grant, so such an operation is only warned about.
    """
    upstream = server_config.auth.upstream
    if not auth.upstream_grant_source.records_user_grant:
        return operations
    if not upstream.enforce_scopes:
        spec_security.warn_on_unrequested_scopes(
            operations,
            [*upstream.required_scopes, *upstream.scopes],
            mcp_scope_policy,
            _scoped_clients(server_config),
            server_config.name,
        )
        return operations
    spec_security.warn_on_missing_oauth_scheme(server_config.name)
    exposed = spec_security.drop_unreachable_operations(
        operations, upstream.scopes, upstream.required_scopes, server_config.name
    )
    if not exposed:
        raise ValueError(
            f'Server "{server_config.name}": no operation is left to expose, '
            'since no sign-in can open any within auth.upstream.scopes and auth.upstream.required_scopes. '
            'Add the scopes the operations need to auth.upstream.scopes, or correct them under operations.<id>.security.'
        )
    spec_security.warn_on_unasked_ceiling_scopes(exposed, upstream.scopes, server_config.name)
    spec_security.warn_on_empty_sign_in(
        exposed, upstream.scopes, upstream.required_scopes, mcp_scope_policy, server_config.name
    )
    return exposed


def _with_requested_upstream_scopes(server_config: ServerConfig, scopes: list[str]) -> ServerConfig:
    """Return ``server_config`` requesting at most ``scopes`` upstream, as when ``auth.upstream.scopes`` lists them."""
    if scopes:
        logger.info(
            'Server "%s": auth.upstream.scopes is unset, so each sign-in asks for what it needs '
            'of the upstream scopes the exposed operations declare (%s).',
            server_config.name,
            ' '.join(scopes),
        )
    upstream = server_config.auth.upstream.model_copy(update={'scopes': scopes})
    return server_config.model_copy(update={'auth': server_config.auth.model_copy(update={'upstream': upstream})})


def _warn_on_ignored_resource_declarations(server_config: ServerConfig, operations: list[OperationInfo]) -> None:
    """Warn about the spec's ``x-mcp-integration.resource`` declarations that no resource comes of.

    Only static exposure with ``exposure.promote_resources`` registers resources.
    An ``operations.<operationId>.resource`` the operator wrote is refused at load instead.
    """
    if server_config.exposure.promote_resources:
        return
    declared = [operation.operation_id for operation in operations if operation.resource_exposed]
    if declared:
        logger.warning(
            'Server "%s": %d operation(s) declare x-mcp-integration.resource (%s), '
            'but only exposure.style static with exposure.promote_resources makes resources, so they stay tools.',
            server_config.name,
            len(declared),
            ', '.join(declared[:5]) + ('...' if len(declared) > 5 else ''),
        )


def _partition_operations(
    operations: list[OperationInfo],
    server_name: str,
    promote_resources: bool = False,
) -> tuple[list[OperationInfo], list[OperationInfo]]:
    """Split ``operations`` into ``(resource_operations, tool_operations)``.

    With ``promote_resources`` off, the default,
    every operation becomes a tool and ``x-mcp-integration.resource`` declarations are ignored.

    With it on, an operation becomes a resource when it either declares ``resource`` explicitly,
    or qualifies for promotion (no ``tool`` and the eligibility rules pass).
    Declaring both ``tool`` and ``resource`` registers the operation in both lists.
    """
    if not promote_resources:
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

    auth_provider: OAuthAuthorizationServerProvider[typing.Any, typing.Any, typing.Any] | None = None


def _policy_summary(policy: PolicyConfig) -> str:
    """A short human label for which operations a server exposes.

    Printed so a tool count reads as the result of a decision rather than a bare number.
    An unfiltered server says so outright, since that is the case worth noticing.
    """
    filters = []
    if policy.annotated_only:
        filters.append('annotated only')
    if policy.allow:
        filters.append(f'allow {policy.allow}')
    if policy.deny:
        filters.append(f'deny {policy.deny}')
    return ', '.join(filters) if filters else 'no filter, every operation exposed'


def _cache_hints(scope_registry: ScopeRegistry) -> dict[CacheableMethod, CacheHint]:
    """Freshness hints for the static lists.

    The tool and resource lists turn private once they depend on the token.
    """
    list_cache_scope = scope_registry.list_cache_scope
    return {
        'tools/list': CacheHint(ttl_ms=_STATIC_CACHE_TTL_MS, scope=list_cache_scope),
        'resources/list': CacheHint(ttl_ms=_STATIC_CACHE_TTL_MS, scope=list_cache_scope),
        'resources/templates/list': CacheHint(ttl_ms=_STATIC_CACHE_TTL_MS, scope=list_cache_scope),
        'server/discover': CacheHint(ttl_ms=_STATIC_CACHE_TTL_MS, scope='public'),
    }


def _semantic_search_options(server_name: str, config: SemanticSearchConfig | None) -> SemanticSearchOptions | None:
    """The semantic search a server's config asks for, refused without the ``embeddings`` extra or a usable model."""
    if config is None:
        return None
    if not embeddings_available():
        raise ValueError(
            f'Server "{server_name}": exposure.search.semantic needs the embeddings extra. '
            'Install openapi-mcp-gateway[embeddings].'
        )
    try:
        embedder = build_embedder(config.model)
    except Exception as error:
        raise ValueError(
            f'Server "{server_name}": exposure.search.semantic cannot use {config.model!r}: {error}'
        ) from error
    return SemanticSearchOptions(
        model=config.model,
        embedder=embedder,
        dimensions=config.dimensions,
        # Each server keeps its vectors in its own directory, so servers sharing one never overwrite each other.
        cache_directory=config.cache_directory.expanduser() / server_name if config.cache_directory else None,
        retrieval=config.retrieval,
    )


def _refuse_undefined_mcp_scopes(server_config: ServerConfig, mcp_scope_policy: McpScopePolicy) -> None:
    """Refuse a client granted a scope the server does not define, since it would open nothing."""
    undefined = {
        client_id: sorted(set(scopes) - set(mcp_scope_policy.mcp_defined_scopes))
        for client_id, scopes in _scoped_clients(server_config).items()
    }
    named = '; '.join(f'{client_id}: {", ".join(scopes)}' for client_id, scopes in undefined.items() if scopes)
    if named:
        raise ValueError(
            f'Server "{server_config.name}": auth.mcp.clients or auth.mcp.other_clients grant scopes '
            f'auth.mcp.operation_scopes does not define ({named}).'
        )


def _scoped_clients(server_config: ServerConfig) -> dict[str, list[str]]:
    """The scopes each configured client may hold, with unlisted metadata document clients as ``other_clients``."""
    mcp = server_config.auth.mcp
    scopes = {client_id: client.scopes for client_id, client in mcp.clients.items()}
    other_clients = mcp.resolve_other_clients()
    if mcp.operation_scopes is not None and other_clients is not None:
        scopes['other_clients'] = other_clients.scopes
    return scopes


def _format_log_label(label: str) -> str:
    """``label`` as one token, so a ``key=value`` log line stays parseable."""
    return label.replace(' (', '(')
