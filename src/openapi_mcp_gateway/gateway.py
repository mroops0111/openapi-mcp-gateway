import asyncio
import contextlib
import dataclasses
import logging
import typing

import pydantic
import uvicorn
from fastapi import FastAPI
from mcp.server.auth.handlers.metadata import MetadataHandler, ProtectedResourceMetadataHandler
from mcp.server.auth.routes import build_metadata, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import ProtectedResourceMetadata
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from .auth.flows import AuthorizationCodeProvider, build_oauth_flow
from .auth.resolver import AuthResolver, NullAuthResolver, StaticAuthResolver
from .generator import ToolGenerator
from .openapi import OpenAPISpec, load_spec, parse_spec
from .policy import filter_operations
from .settings import AuthConfig, GatewayConfig, PolicyConfig, ServerConfig
from .stores import create_store


logger = logging.getLogger(__name__)


@dataclasses.dataclass
class _AppContext:
    """Values injected into MCP tool handlers via FastMCP lifespan."""

    auth_provider: AuthorizationCodeProvider | None = None


class _ServerBundle(typing.NamedTuple):
    name: str
    mount_path: str
    mcp: FastMCP
    spec: OpenAPISpec
    auth_provider: AuthorizationCodeProvider | None
    auth_settings: AuthSettings | None = None


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
        """Create a gateway; optionally pre-fill ``GatewayConfig``."""
        self._config = config or GatewayConfig()
        self._servers: list[_ServerBundle] = []
        self._shutdown_hooks: list[typing.Callable[[], typing.Awaitable[None]]] = []
        store_cfg = self._config.store
        self._store = create_store(
            store_type=store_cfg.type,
            url=store_cfg.redis_url,
            prefix=store_cfg.key_prefix,
        )
        logger.debug('Token store initialised: type=%s prefix=%s', store_cfg.type, store_cfg.key_prefix)

    @classmethod
    def from_config(cls, config: GatewayConfig) -> 'Gateway':
        """Construct a gateway and register every entry in ``config.servers``."""
        gateway = cls(config=config)
        for entry in config.servers:
            gateway._add_server_from_entry(entry)
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
    ) -> None:
        """Register a server from arguments (convenience over building ``ServerConfig``)."""
        entry = ServerConfig(
            name=name,
            spec=spec,
            base_url=base_url,
            path_prefix=path_prefix,
            auth=AuthConfig.model_validate(auth) if auth else AuthConfig(),
            policy=PolicyConfig.model_validate(policy) if policy else PolicyConfig(),
            timeout=timeout,
        )
        self._add_server_from_entry(entry)

    def run(
        self,
        transport: str | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        """Start ``uvicorn`` (HTTP/SSE) or the stdio transport for a single server."""
        transport = transport or self._config.transport
        host = host or self._config.host
        port = port or self._config.port

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
        """Mount each registered MCP sub-app on ``app`` at its configured path."""
        transport = transport or self._config.transport
        for handle in self._servers:
            mcp_app = handle.mcp.sse_app() if transport == 'sse' else handle.mcp.streamable_http_app()
            app.mount(handle.mount_path, mcp_app)

    def _add_server_from_entry(self, entry: ServerConfig) -> None:
        """Internal: wire one ``ServerConfig`` from parsed spec through tool registration."""
        logger.info('Loading server "%s" from spec=%s', entry.name, entry.spec)
        raw = load_spec(entry.spec)
        spec = parse_spec(raw, source=entry.spec)
        logger.debug(
            'Parsed spec for "%s": title=%r version=%r operations=%d',
            entry.name,
            spec.title,
            spec.version,
            len(spec.operations),
        )

        base_url = entry.base_url or spec.default_base_url
        if not base_url:
            raise ValueError(
                f'Server "{entry.name}": no base_url provided and none found in OpenAPI spec. '
                'Set base_url in config or add a servers entry to your spec.'
            )

        # Filter operations by policy
        operations = filter_operations(
            spec.operations,
            allow=entry.policy.allow,
            deny=entry.policy.deny,
            marked_only=entry.policy.marked_only,
        )
        logger.debug(
            'Policy applied to "%s": %d → %d operations (allow=%s deny=%s marked_only=%s)',
            entry.name,
            len(spec.operations),
            len(operations),
            entry.policy.allow,
            entry.policy.deny,
            entry.policy.marked_only,
        )

        if not operations:
            raise ValueError(
                f'Server "{entry.name}": no operations to expose after applying policy. '
                'Check your allow/deny rules or the OpenAPI spec.'
            )

        # Resolve auth strategy
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
            auth_resolver = StaticAuthResolver(header_value) if header_value else NullAuthResolver()
        else:
            auth_resolver = NullAuthResolver()

        # Create MCP server
        @contextlib.asynccontextmanager
        async def lifespan(_app: FastMCP, _auth_provider=auth_provider):
            try:
                yield _AppContext(auth_provider=_auth_provider)
            finally:
                pass

        mcp = FastMCP(
            f'{entry.name} (via OpenAPI MCP Gateway)',
            auth_server_provider=auth_provider,
            auth=auth_settings,
            lifespan=lifespan,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

        # Register callback route for OAuth
        if auth_provider:
            _auth_provider = auth_provider

            @mcp.custom_route('/auth/callback', methods=['GET'])
            async def upstream_callback_handler(request, _provider=_auth_provider):
                code = request.query_params.get('code')
                state = request.query_params.get('state')
                if not code or not state:
                    raise HTTPException(400, 'Missing code or state parameter')
                redirect_uri = await _provider.handle_upstream_callback(code, state)
                return RedirectResponse(status_code=302, url=redirect_uri)

        # Register tools
        generator = ToolGenerator(
            mcp=mcp,
            base_url=base_url,
            auth_resolver=auth_resolver,
            timeout=entry.timeout,
        )
        generator.register_operations(operations)

        self._servers.append(
            _ServerBundle(
                name=entry.name,
                mount_path=entry.mount_path,
                mcp=mcp,
                spec=spec,
                auth_provider=auth_provider,
                auth_settings=auth_settings,
            )
        )
        logger.info(
            'Registered server "%s": mount=%s base_url=%s tools=%d auth=%s',
            entry.name,
            entry.mount_path,
            base_url,
            len(operations),
            entry.auth.type,
        )

    async def _run_shutdown_hooks(self) -> None:
        """Run every flow-registered shutdown hook, swallowing per-hook errors."""
        for hook in self._shutdown_hooks:
            try:
                await hook()
            except Exception:
                logger.exception('Shutdown hook raised an exception')

    def _run_shutdown_hooks_blocking(self) -> None:
        """Synchronous wrapper around ``_run_shutdown_hooks`` for the stdio code path."""
        if not self._shutdown_hooks:
            return

        try:
            asyncio.run(self._run_shutdown_hooks())
        except RuntimeError:
            # Loop already running (unlikely on stdio shutdown) — best-effort.
            logger.warning('Could not run shutdown hooks: event loop already running')

    def _build_app(self, transport: str) -> FastAPI:
        """Internal: assemble CORS, OAuth, discovery routes, health check, and MCP mounts."""
        config = self._config

        @contextlib.asynccontextmanager
        async def lifespan(app: FastAPI):
            async with contextlib.AsyncExitStack() as stack:
                for handle in self._servers:
                    await stack.enter_async_context(handle.mcp.session_manager.run())
                yield
            await self._run_shutdown_hooks()
            await self._store.close()

        app = FastAPI(
            title='OpenAPI MCP Gateway',
            debug=config.debug,
            lifespan=lifespan,
            docs_url='/docs' if config.enable_docs else None,
            redoc_url='/redoc' if config.enable_docs else None,
            openapi_url='/openapi.json' if config.enable_docs else None,
        )

        # CORS
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors.allow_origins,
            allow_methods=config.cors.allow_methods,
            allow_headers=config.cors.allow_headers,
            expose_headers=config.cors.expose_headers,
        )

        # Register OAuth routes for each provider with path prefix
        for handle in self._servers:
            if not handle.auth_provider or not handle.auth_settings:
                continue

            oauth_routes = create_auth_routes(
                provider=handle.auth_provider,
                issuer_url=handle.auth_settings.issuer_url,
                service_documentation_url=handle.auth_settings.service_documentation_url,
                client_registration_options=handle.auth_settings.client_registration_options,
                revocation_options=handle.auth_settings.revocation_options,
            )
            for route in oauth_routes:
                prefixed_path = f'{handle.mount_path.rstrip("/")}{route.path}'
                app.router.add_route(
                    path=prefixed_path,
                    endpoint=route.endpoint,
                    methods=route.methods,
                    name=route.name,
                )

            # Register protected resource metadata routes (RFC 9728)
            issuer = str(handle.auth_settings.issuer_url).rstrip('/')
            pr_routes = create_protected_resource_routes(
                resource_url=pydantic.AnyHttpUrl(f'{issuer}/mcp'),
                authorization_servers=[handle.auth_settings.issuer_url],
                scopes_supported=handle.auth_settings.client_registration_options.valid_scopes
                if handle.auth_settings.client_registration_options
                else None,
            )
            for route in pr_routes:
                app.router.add_route(
                    path=route.path,
                    endpoint=route.endpoint,
                    methods=route.methods,
                    name=route.name,
                )

        # Well-known discovery endpoints (RFC 8414 / RFC 9728)
        server_lookup: dict[str, _ServerBundle] = {h.name: h for h in self._servers}

        @app.get('/.well-known/oauth-authorization-server/{server_name}')
        @app.options('/.well-known/oauth-authorization-server/{server_name}')
        @app.get('/.well-known/oauth-authorization-server/{server_name}/mcp')
        @app.options('/.well-known/oauth-authorization-server/{server_name}/mcp')
        async def oauth_authorization_server_discovery(request: Request, server_name: str):
            handle = server_lookup.get(server_name)
            if not handle or not handle.auth_settings:
                return JSONResponse(
                    status_code=404,
                    content={'error': f'Server not found: {server_name}'},
                )
            metadata = build_metadata(
                issuer_url=handle.auth_settings.issuer_url,
                service_documentation_url=handle.auth_settings.service_documentation_url,
                client_registration_options=handle.auth_settings.client_registration_options
                or ClientRegistrationOptions(),
                revocation_options=handle.auth_settings.revocation_options or RevocationOptions(),
            )
            handler = MetadataHandler(metadata)
            return await handler.handle(request)

        @app.get('/.well-known/oauth-protected-resource/{server_name}')
        @app.options('/.well-known/oauth-protected-resource/{server_name}')
        @app.get('/.well-known/oauth-protected-resource/{server_name}/mcp')
        @app.options('/.well-known/oauth-protected-resource/{server_name}/mcp')
        async def oauth_protected_resource_discovery(request: Request, server_name: str):
            handle = server_lookup.get(server_name)
            if not handle or not handle.auth_settings:
                return JSONResponse(
                    status_code=404,
                    content={'error': f'Server not found: {server_name}'},
                )
            issuer = str(handle.auth_settings.issuer_url).rstrip('/')
            metadata = ProtectedResourceMetadata(
                resource=pydantic.AnyHttpUrl(f'{issuer}/mcp'),
                authorization_servers=[pydantic.AnyHttpUrl(issuer)],
                scopes_supported=handle.auth_settings.client_registration_options.valid_scopes
                if handle.auth_settings.client_registration_options
                else None,
            )
            handler = ProtectedResourceMetadataHandler(metadata)
            return await handler.handle(request)

        @app.get('/healthz')
        async def healthz():
            return {
                'status': 'ok',
                'servers': [
                    {
                        'name': h.name,
                        'path': h.mount_path,
                        'title': h.spec.title,
                        'auth': 'oauth2' if h.auth_provider else 'static',
                    }
                    for h in self._servers
                ],
            }

        # Mount MCP apps AFTER registering OAuth routes
        for handle in self._servers:
            mcp_app = handle.mcp.sse_app() if transport == 'sse' else handle.mcp.streamable_http_app()
            app.mount(handle.mount_path, mcp_app)

        return app
