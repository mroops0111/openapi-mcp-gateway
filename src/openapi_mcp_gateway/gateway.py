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

from .auth.detector import DetectedOAuthFlow, detect_primary_oauth_flow
from .auth.provider import GatewayOAuthProvider
from .auth.resolver import AuthResolver, NullAuthResolver, OAuthAuthResolver, StaticAuthResolver
from .generator import ToolGenerator
from .openapi import OpenAPISpec, load_spec, parse_spec
from .policy import filter_operations
from .settings import GatewayConfig, PolicyConfig, ServerConfig
from .stores import create_store


logger = logging.getLogger(__name__)


@dataclasses.dataclass
class _AppContext:
    """Lifespan context passed to MCP tool functions."""

    auth_provider: GatewayOAuthProvider | None = None


class _ServerBundle(typing.NamedTuple):
    """Internal handle for a registered MCP server."""

    name: str
    mount_path: str
    mcp: FastMCP
    spec: OpenAPISpec
    auth_provider: GatewayOAuthProvider | None
    auth_settings: AuthSettings | None = None


class Gateway:
    """Multi-server MCP gateway.

    Usage:
        gateway = Gateway()
        gateway.add_server(name="petstore", spec="petstore.json")
        gateway.run()

    Or with a config file:
        config = GatewayConfig.from_yaml("servers.yml")
        gateway = Gateway.from_config(config)
        gateway.run()
    """

    def __init__(self, config: GatewayConfig | None = None):
        self._config = config or GatewayConfig()
        self._servers: list[_ServerBundle] = []
        store_cfg = self._config.store
        self._store = create_store(
            store_type=store_cfg.type,
            url=store_cfg.redis_url,
            prefix=store_cfg.key_prefix,
        )
        logger.debug('Token store initialised: type=%s prefix=%s', store_cfg.type, store_cfg.key_prefix)

    @classmethod
    def from_config(cls, config: GatewayConfig) -> 'Gateway':
        """Build a gateway from a GatewayConfig, registering all servers."""
        gw = cls(config=config)
        for entry in config.servers:
            gw._add_server_from_entry(entry)
        return gw

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
        """Add an MCP server from an OpenAPI spec."""
        from .settings import AuthConfig

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
        """Start the gateway server."""
        transport = transport or self._config.transport
        host = host or self._config.host
        port = port or self._config.port

        if transport == 'stdio':
            if len(self._servers) != 1:
                raise ValueError('stdio transport only supports a single server')
            logger.info('Starting MCP server "%s" over stdio', self._servers[0].name)
            self._servers[0].mcp.run(transport='stdio')
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
        """Mount all MCP servers onto an existing FastAPI application."""
        transport = transport or self._config.transport
        for handle in self._servers:
            mcp_app = handle.mcp.sse_app() if transport == 'sse' else handle.mcp.streamable_http_app()
            app.mount(handle.mount_path, mcp_app)

    def _add_server_from_entry(self, entry: ServerConfig) -> None:
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
        auth_provider: GatewayOAuthProvider | None = None
        auth_resolver: AuthResolver
        auth_settings: AuthSettings | None = None

        if entry.auth.type == 'oauth2':
            auth_provider, auth_resolver, auth_settings = self._setup_oauth(entry, spec)
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

    def _setup_oauth(
        self, entry: ServerConfig, spec: OpenAPISpec
    ) -> tuple[GatewayOAuthProvider, AuthResolver, AuthSettings]:
        """Set up OAuth for a server entry."""
        client_id = entry.auth.resolve_client_id()
        client_secret = entry.auth.resolve_client_secret()

        if not client_id or not client_secret:
            raise ValueError(
                f'Server "{entry.name}": OAuth2 requires client_id and client_secret. '
                'Set them directly or use ${ENV_VAR} syntax.'
            )

        # Detect OAuth flow from spec, fall back to explicit config URLs
        detected = detect_primary_oauth_flow(spec)

        if detected and not detected.authorization_url:
            raise ValueError(
                f'Server "{entry.name}": only authorization_code flow is supported for MCP OAuth. '
                'The detected flow has no authorization_url.'
            )

        if not detected:
            # No securitySchemes in spec — require explicit URLs from config
            if not entry.auth.authorization_url or not entry.auth.token_url:
                raise ValueError(
                    f'Server "{entry.name}": auth type is oauth2 but no OAuth2 flow found in the OpenAPI spec. '
                    'Provide authorization_url and token_url in auth config, or add a securitySchemes '
                    'section to the spec.'
                )
            detected = DetectedOAuthFlow(
                flow_type='authorization_code',
                authorization_url=entry.auth.authorization_url,
                token_url=entry.auth.token_url,
            )
        else:
            # Config URLs override spec-detected URLs if provided
            if entry.auth.authorization_url:
                detected.authorization_url = entry.auth.authorization_url
            if entry.auth.token_url:
                detected.token_url = entry.auth.token_url

        # Build callback URL
        gateway_url = self._config.url.rstrip('/')
        callback_url = f'{gateway_url}{entry.mount_path}/auth/callback'

        provider = GatewayOAuthProvider(
            store=self._store,
            upstream_auth_url=typing.cast(str, detected.authorization_url),
            upstream_token_url=detected.token_url,
            client_id=client_id,
            client_secret=client_secret,
            callback_url=callback_url,
            scopes=entry.auth.scopes,
            prefix=entry.name,
        )

        server_url = pydantic.AnyHttpUrl(f'{gateway_url}{entry.mount_path}')
        auth_settings = AuthSettings(
            issuer_url=server_url,
            resource_server_url=server_url,
            revocation_options=RevocationOptions(enabled=True),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=['api'],
                default_scopes=['api'],
            ),
            required_scopes=['api'],
        )

        logger.debug(
            'OAuth set up for "%s": authorize=%s token=%s scopes=%s',
            entry.name,
            detected.authorization_url,
            detected.token_url,
            entry.auth.scopes,
        )

        resolver = OAuthAuthResolver(provider)
        return provider, resolver, auth_settings

    def _build_app(self, transport: str) -> FastAPI:
        config = self._config

        @contextlib.asynccontextmanager
        async def lifespan(app: FastAPI):
            async with contextlib.AsyncExitStack() as stack:
                for handle in self._servers:
                    await stack.enter_async_context(handle.mcp.session_manager.run())
                yield
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
