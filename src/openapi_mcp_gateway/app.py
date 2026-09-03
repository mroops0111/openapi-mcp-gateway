import contextlib
import logging
import typing

from fastapi import FastAPI
from mcp.server import MCPServer
from mcp.server.auth.handlers.metadata import MetadataHandler, ProtectedResourceMetadataHandler
from mcp.server.auth.routes import build_metadata, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import ProtectedResourceMetadata
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth.flows import AuthorizationCodeProvider
from .openapi import ExposedTool, OpenAPISpec
from .settings import GatewayConfig
from .stores.base import TokenStore


logger = logging.getLogger(__name__)


_TRANSPORT_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


class _ServerBundle(typing.NamedTuple):
    """Runtime objects for one registered MCP server, mounted onto the gateway FastAPI app."""

    name: str
    mount_path: str
    mcp: MCPServer
    spec: OpenAPISpec
    auth_provider: AuthorizationCodeProvider | None
    auth_settings: AuthSettings | None = None
    token_verifier: typing.Any | None = None
    protected_resource: ProtectedResourceMetadata | None = None
    # Captured at registration for the --dry-run summary.
    base_url: str = ''
    auth_summary: str = 'none'
    exposure: str = 'static'
    tools: tuple[ExposedTool, ...] = ()
    resource_names: tuple[str, ...] = ()


def build_mcp_asgi_app(mcp: MCPServer, transport: str) -> typing.Any:
    """Build the Starlette ASGI app for ``mcp`` under ``transport``.

    Centralises the ``sse`` vs ``streamable-http`` choice,
    and the ``transport_security`` argument.
    mcp v2 moved that argument off the ``MCPServer`` constructor onto these factory methods.
    """
    if transport == 'sse':
        return mcp.sse_app(transport_security=_TRANSPORT_SECURITY)
    return mcp.streamable_http_app(transport_security=_TRANSPORT_SECURITY)


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
            for bundle in servers:
                await stack.enter_async_context(bundle.mcp.session_manager.run())
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

    register_auth_routes(app, servers)
    _register_health_route(app, servers)

    for bundle in servers:
        app.mount(bundle.mount_path, build_mcp_asgi_app(bundle.mcp, transport))

    return app


def register_auth_routes(app: FastAPI, servers: list[_ServerBundle]) -> None:
    """Register the OAuth and ``.well-known`` discovery routes an embedder needs for ``servers``.

    ``mount`` adds these alongside the MCP sub-apps for a working OAuth flow.
    Register before mounting, so the explicit OAuth paths win over each catch-all ``mount_path``.
    """
    # Per-server OAuth endpoints and RFC 9728 protected-resource routes.
    # Only a bundle whose flow made the gateway the authorization server gets the former.
    # Advertising /authorize and /token for a delegating server would point clients at endpoints this app does not serve.
    for bundle in servers:
        if not bundle.auth_provider or not bundle.auth_settings:
            continue

        oauth_routes = create_auth_routes(
            provider=bundle.auth_provider,
            issuer_url=bundle.auth_settings.issuer_url,
            service_documentation_url=bundle.auth_settings.service_documentation_url,
            client_registration_options=bundle.auth_settings.client_registration_options,
            revocation_options=bundle.auth_settings.revocation_options,
        )
        for route in oauth_routes:
            prefixed_path = f'{bundle.mount_path.rstrip("/")}{route.path}'
            app.router.add_route(
                path=prefixed_path,
                endpoint=route.endpoint,
                methods=route.methods,
                name=route.name,
            )

        advertised = _protected_resource(bundle)
        pr_routes = create_protected_resource_routes(
            resource_url=advertised.resource,
            authorization_servers=advertised.authorization_servers,
            scopes_supported=advertised.scopes_supported,
        )
        for route in pr_routes:
            app.router.add_route(
                path=route.path,
                endpoint=route.endpoint,
                methods=route.methods,
                name=route.name,
            )

    # RFC 8414 / 9728 ``.well-known`` discovery endpoints, resolved per server name at request time.
    server_lookup: dict[str, _ServerBundle] = {bundle.name: bundle for bundle in servers}

    @app.get('/.well-known/oauth-authorization-server/{server_name}')
    @app.options('/.well-known/oauth-authorization-server/{server_name}')
    @app.get('/.well-known/oauth-authorization-server/{server_name}/mcp')
    @app.options('/.well-known/oauth-authorization-server/{server_name}/mcp')
    async def oauth_authorization_server_discovery(request: Request, server_name: str):
        bundle = server_lookup.get(server_name)
        if not bundle or not bundle.auth_settings:
            return JSONResponse(
                status_code=404,
                content={'error': f'Server not found: {server_name}'},
            )
        if not bundle.auth_provider:
            # This server delegates to an external issuer, so the gateway has no metadata to publish.
            # The protected-resource document names that issuer, and the client discovers it there.
            return JSONResponse(
                status_code=404,
                content={
                    'error': f'Server "{server_name}" is not an authorization server. '
                    'Read its protected resource metadata for the issuer that is.'
                },
            )
        metadata = build_metadata(
            issuer_url=bundle.auth_settings.issuer_url,
            service_documentation_url=bundle.auth_settings.service_documentation_url,
            client_registration_options=bundle.auth_settings.client_registration_options or ClientRegistrationOptions(),
            revocation_options=bundle.auth_settings.revocation_options or RevocationOptions(),
        )
        handler = MetadataHandler(metadata)
        return await handler.handle(request)

    @app.get('/.well-known/oauth-protected-resource/{server_name}')
    @app.options('/.well-known/oauth-protected-resource/{server_name}')
    @app.get('/.well-known/oauth-protected-resource/{server_name}/mcp')
    @app.options('/.well-known/oauth-protected-resource/{server_name}/mcp')
    async def oauth_protected_resource_discovery(request: Request, server_name: str):
        bundle = server_lookup.get(server_name)
        if not bundle or not bundle.auth_settings:
            return JSONResponse(
                status_code=404,
                content={'error': f'Server not found: {server_name}'},
            )
        handler = ProtectedResourceMetadataHandler(_protected_resource(bundle))
        return await handler.handle(request)


def _register_health_route(app: FastAPI, servers: list[_ServerBundle]) -> None:
    """Register ``/healthz`` reporting per-server name, mount path, and auth mode."""

    @app.get('/healthz')
    async def healthz():
        return {
            'status': 'ok',
            'servers': [
                {
                    'name': bundle.name,
                    'path': bundle.mount_path,
                    'title': bundle.spec.title,
                    # A verifier protects the endpoint just as a provider does,
                    # so a delegating server must not report itself as unprotected.
                    'auth': 'oauth2' if (bundle.auth_provider or bundle.token_verifier) else 'static',
                }
                for bundle in servers
            ],
        }


def _protected_resource(bundle: _ServerBundle) -> ProtectedResourceMetadata:
    """Return the RFC 9728 document this server publishes.

    A flow that delegates to an external issuer supplies its own, since only it knows which issuer to name.
    Otherwise the gateway is both issuer and resource, so the document follows from the settings it built for itself.

    Callers reach this only for a bundle carrying ``auth_settings``,
    which the surrounding checks have already established.
    """
    if bundle.protected_resource is not None:
        return bundle.protected_resource

    assert bundle.auth_settings is not None  # noqa: S101
    issuer = str(bundle.auth_settings.issuer_url).rstrip('/')
    registration = bundle.auth_settings.client_registration_options
    return ProtectedResourceMetadata.model_validate(
        {
            'resource': f'{issuer}/mcp',
            'authorization_servers': [issuer],
            'scopes_supported': registration.valid_scopes if registration else None,
        }
    )
