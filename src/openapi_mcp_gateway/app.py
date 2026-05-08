import contextlib
import logging
import typing

import pydantic
from fastapi import FastAPI
from mcp.server.auth.handlers.metadata import MetadataHandler, ProtectedResourceMetadataHandler
from mcp.server.auth.routes import build_metadata, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import ProtectedResourceMetadata
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth.flows import AuthorizationCodeProvider
from .openapi import OpenAPISpec
from .settings import GatewayConfig
from .stores.base import TokenStore


logger = logging.getLogger(__name__)


class _ServerBundle(typing.NamedTuple):
    """Runtime objects for one registered MCP server, mounted onto the gateway FastAPI app."""

    name: str
    mount_path: str
    mcp: FastMCP
    spec: OpenAPISpec
    auth_provider: AuthorizationCodeProvider | None
    auth_settings: AuthSettings | None = None


def build_app(
    servers: list[_ServerBundle],
    config: GatewayConfig,
    store: TokenStore,
    on_shutdown: typing.Callable[[], typing.Awaitable[None]],
    transport: str,
) -> FastAPI:
    """Assemble the gateway FastAPI app with CORS, OAuth, discovery, health, and MCP mounts."""

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with contextlib.AsyncExitStack() as stack:
            for handle in servers:
                await stack.enter_async_context(handle.mcp.session_manager.run())
            yield
        await on_shutdown()
        await store.close()

    app = FastAPI(
        title='OpenAPI MCP Gateway',
        debug=config.debug,
        lifespan=lifespan,
        docs_url='/docs' if config.enable_docs else None,
        redoc_url='/redoc' if config.enable_docs else None,
        openapi_url='/openapi.json' if config.enable_docs else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors.allow_origins,
        allow_methods=config.cors.allow_methods,
        allow_headers=config.cors.allow_headers,
        expose_headers=config.cors.expose_headers,
    )

    _register_oauth_routes(app, servers)
    _register_well_known_routes(app, servers)
    _register_health_route(app, servers)

    for handle in servers:
        mcp_app = handle.mcp.sse_app() if transport == 'sse' else handle.mcp.streamable_http_app()
        app.mount(handle.mount_path, mcp_app)

    return app


def _register_oauth_routes(app: FastAPI, servers: list[_ServerBundle]) -> None:
    """Mount MCP-side OAuth endpoints and RFC 9728 protected-resource routes per server."""
    for handle in servers:
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


def _register_well_known_routes(app: FastAPI, servers: list[_ServerBundle]) -> None:
    """Register RFC 8414 / 9728 ``.well-known`` discovery endpoints per server."""
    server_lookup: dict[str, _ServerBundle] = {h.name: h for h in servers}

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
            client_registration_options=handle.auth_settings.client_registration_options or ClientRegistrationOptions(),
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


def _register_health_route(app: FastAPI, servers: list[_ServerBundle]) -> None:
    """Register ``/healthz`` reporting per-server name, mount path, and auth mode."""

    @app.get('/healthz')
    async def healthz():
        return {
            'status': 'ok',
            'servers': [
                {
                    'name': handle.name,
                    'path': handle.mount_path,
                    'title': handle.spec.title,
                    'auth': 'oauth2' if handle.auth_provider else 'static',
                }
                for handle in servers
            ],
        }
