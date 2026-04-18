"""Gateway: orchestrates multiple MCP servers from OpenAPI specs."""

import contextlib
import dataclasses
import typing

import pydantic
import uvicorn
from fastapi import FastAPI
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from .auth.detector import detect_primary_oauth_flow
from .auth.provider import GatewayOAuthProvider
from .auth.resolver import AuthResolver, NullAuthResolver, OAuthAuthResolver, StaticAuthResolver
from .generator import ToolGenerator
from .openapi import OpenAPISpec, load_spec, parse_spec
from .policy import filter_operations
from .settings import GatewayConfig, PolicyConfig, ServerConfig
from .stores import create_store


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

    # ── Public API ─────────────────────────────────────────────

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
            self._servers[0].mcp.run(transport='stdio')
            return

        app = self._build_app(transport=transport)
        uvicorn.run(app, host=host, port=port)

    def mount(self, app: FastAPI, transport: str | None = None) -> None:
        """Mount all MCP servers onto an existing FastAPI application."""
        transport = transport or self._config.transport
        for handle in self._servers:
            mcp_app = handle.mcp.sse_app() if transport == 'sse' else handle.mcp.streamable_http_app()
            app.mount(handle.mount_path, mcp_app)

    # ── Internals ──────────────────────────────────────────────

    def _add_server_from_entry(self, entry: ServerConfig) -> None:
        raw = load_spec(entry.spec)
        spec = parse_spec(raw)

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
            )
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
                'Set them directly or via client_id_env / client_secret_env.'
            )

        # Detect OAuth flow from spec
        detected = detect_primary_oauth_flow(spec)
        if not detected:
            raise ValueError(
                f'Server "{entry.name}": auth type is oauth2 but no OAuth2 flow found in the OpenAPI spec\'s '
                'securitySchemes. Add an oauth2 security scheme or use a different auth type.'
            )

        if not detected.authorization_url:
            raise ValueError(
                f'Server "{entry.name}": only authorization_code flow is supported for MCP OAuth. '
                'The detected flow has no authorization_url.'
            )

        # Build callback URL
        gateway_url = self._config.url.rstrip('/')
        callback_url = f'{gateway_url}{entry.mount_path}/auth/callback'

        scopes = entry.auth.scopes or list(detected.scopes.keys())

        provider = GatewayOAuthProvider(
            store=self._store,
            upstream_auth_url=detected.authorization_url,
            upstream_token_url=detected.token_url,
            client_id=client_id,
            client_secret=client_secret,
            callback_url=callback_url,
            scopes=scopes,
            prefix=entry.name,
        )

        server_url = pydantic.AnyHttpUrl(f'{gateway_url}{entry.mount_path}')
        auth_settings = AuthSettings(
            issuer_url=server_url,
            resource_server_url=server_url,
            revocation_options=RevocationOptions(enabled=True),
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=scopes or ['api'],
                default_scopes=scopes or ['api'],
            ),
            required_scopes=scopes or ['api'],
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

        # ── .well-known discovery endpoints (RFC 8414 / RFC 9728) ──
        server_lookup: dict[str, _ServerBundle] = {h.name: h for h in self._servers}

        @app.get('/.well-known/oauth-authorization-server/{server_name}')
        @app.options('/.well-known/oauth-authorization-server/{server_name}')
        @app.get('/.well-known/oauth-authorization-server/{server_name}/mcp')
        @app.options('/.well-known/oauth-authorization-server/{server_name}/mcp')
        async def oauth_authorization_server_discovery(request: Request, server_name: str):
            handle = server_lookup.get(server_name)
            if not handle:
                return JSONResponse(
                    status_code=404,
                    content={'error': f'Server not found: {server_name}'},
                )
            base = config.url.rstrip('/')
            issuer = f'{base}{handle.mount_path}'
            return JSONResponse(
                {
                    'issuer': issuer,
                    'authorization_endpoint': f'{issuer}/authorize',
                    'token_endpoint': f'{issuer}/token',
                    'registration_endpoint': f'{issuer}/register',
                    'response_types_supported': ['code'],
                    'grant_types_supported': ['authorization_code', 'refresh_token'],
                    'token_endpoint_auth_methods_supported': ['client_secret_post'],
                    'revocation_endpoint': f'{issuer}/revoke',
                }
            )

        @app.get('/.well-known/oauth-protected-resource/{server_name}')
        @app.options('/.well-known/oauth-protected-resource/{server_name}')
        @app.get('/.well-known/oauth-protected-resource/{server_name}/mcp')
        @app.options('/.well-known/oauth-protected-resource/{server_name}/mcp')
        async def oauth_protected_resource_discovery(request: Request, server_name: str):
            handle = server_lookup.get(server_name)
            if not handle:
                return JSONResponse(
                    status_code=404,
                    content={'error': f'Server not found: {server_name}'},
                )
            base = config.url.rstrip('/')
            issuer = f'{base}{handle.mount_path}'
            return JSONResponse(
                {
                    'resource': f'{issuer}/mcp',
                    'authorization_servers': [issuer],
                }
            )

        # ── Health check ──
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

        # ── Mount MCP apps ──
        for handle in self._servers:
            mcp_app = handle.mcp.sse_app() if transport == 'sse' else handle.mcp.streamable_http_app()
            app.mount(handle.mount_path, mcp_app)

        return app
