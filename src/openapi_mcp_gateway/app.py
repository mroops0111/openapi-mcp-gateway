import contextlib
import logging
import typing
import urllib.parse

from fastapi import FastAPI
from mcp.server import MCPServer
from mcp.server.auth.handlers.metadata import MetadataHandler, ProtectedResourceMetadataHandler
from mcp.server.auth.provider import OAuthAuthorizationServerProvider
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    cors_middleware,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import AuthSettings
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth.assembly import MCP_ENDPOINT_PATH
from .openapi import ExposedTool, OpenAPISpec
from .scopes import RequestTarget, ScopeChallengeMiddleware, ScopeRegistry
from .settings import DNSRebindingProtectionConfig, GatewayConfig
from .stores.base import TokenStore


logger = logging.getLogger(__name__)


class _ServerBundle(typing.NamedTuple):
    """One registered MCP server, holding the objects the gateway mounts and what those objects would serve.

    The second half is captured at registration because it cannot be recovered afterwards.
    A tool name comes out of naming and shaping, a base URL may have come from the spec rather than the config,
    and an OAuth flow may have been resolved rather than declared.

    No credential is held here.
    ``AuthConfig`` carries the bearer token and the upstream client secret,
    and this document gets piped, pasted into issues and rendered in a browser, so none should be added.
    """

    # Mounted onto the FastAPI app.
    name: str
    mount_path: str
    mcp: MCPServer
    spec: OpenAPISpec
    auth_provider: OAuthAuthorizationServerProvider[typing.Any, typing.Any, typing.Any] | None
    # What each tool or operation requires of a token's scopes, and how a call names the one it reaches.
    scope_registry: ScopeRegistry
    tool_call_target: RequestTarget
    auth_settings: AuthSettings | None = None
    # The RFC 8414 document, set wherever the gateway is this server's authorization server.
    authorization_server_metadata: OAuthMetadata | None = None
    token_verifier: typing.Any | None = None
    protected_resource: ProtectedResourceMetadata | None = None

    # Read back by ``describe`` and by the dry-run table.
    base_url: str = ''
    exposure: str = 'static'
    instructions: str | None = None
    tools: tuple[ExposedTool, ...] = ()
    resource_names: tuple[str, ...] = ()
    auth_type: str = 'none'
    auth_flow: str | None = None
    auth_api_key_header: str | None = None

    # Prose for the dry-run table only.
    # ``describe`` reports the structured fields above instead,
    # so that a caller never has to read a sentence to find a value.
    upstream_auth_summary: str = 'none'
    mcp_auth_summary: str = 'open'
    policy_summary: str = 'no filter'

    def describe(self) -> dict[str, typing.Any]:
        """Return what this server would serve, as plain JSON-serialisable data.

        Only the registration-time facts, never the live objects, so the result needs no custom encoder.
        ``Gateway.describe`` is the public entry point.
        """
        return {
            'name': self.name,
            'mount_path': self.mount_path,
            'base_url': self.base_url,
            # Shaped like ``auth`` in the config, with each value as resolved rather than as written.
            'auth': {
                'upstream': {
                    'type': self.auth_type,
                    'flow': self.auth_flow,
                    'api_key_header': self.auth_api_key_header,
                },
                'mcp': self._describe_mcp_auth(),
            },
            'exposure': self.exposure,
            'instructions': self.instructions,
            'tools': [
                {
                    'name': tool.name,
                    'method': tool.method,
                    'path': tool.path,
                    'shaping': tool.shaping,
                    'description': tool.description,
                    'input_schema': tool.input_schema,
                }
                for tool in self.tools
            ],
            'resources': list(self.resource_names),
        }

    def _describe_mcp_auth(self) -> dict[str, typing.Any]:
        """Who issues this endpoint's tokens and what they must carry, as its discovery documents state it.

        Every value is ``None`` where nothing checks a token.
        """
        if self.auth_settings is None:
            return {'issuer': None, 'grant_types': None, 'required_scopes': None}
        metadata = self.authorization_server_metadata
        return {
            'issuer': str(_protected_resource(self).authorization_servers[0]),
            'grant_types': metadata.grant_types_supported if metadata else None,
            'required_scopes': list(self.auth_settings.required_scopes or []),
        }


def build_mcp_asgi_app(
    mcp: MCPServer, transport: str, protection: DNSRebindingProtectionConfig, host: str | None
) -> typing.Any:
    """Build the Starlette ASGI app for ``mcp`` under ``transport``.

    Centralises the ``sse`` vs ``streamable-http`` choice, and the ``transport_security`` argument.
    mcp v2 moved that argument off the ``MCPServer`` constructor onto these factory methods.
    ``host`` is the address the gateway binds, or ``None`` when it is mounted into an app someone else serves.
    """
    transport_security = _transport_security(protection, host)
    # The SDK reads ``host`` only when ``transport_security`` is ``None``, which never happens for a mounted gateway,
    # so the empty string standing in for its missing bind is never read.
    if transport == 'sse':
        return mcp.sse_app(transport_security=transport_security, host=host or '')
    # Named rather than left to the SDK default, since every token is bound to this path.
    return mcp.streamable_http_app(
        streamable_http_path=MCP_ENDPOINT_PATH, transport_security=transport_security, host=host or ''
    )


def mount_mcp_apps(
    app: FastAPI,
    servers: list[_ServerBundle],
    transport: str,
    protection: DNSRebindingProtectionConfig,
    host: str | None,
) -> list[StreamableHTTPSessionManager]:
    """Mount each server's MCP app onto ``app`` and return the session managers its lifespan must run.

    Starlette runs only the root app's lifespan, so a mounted MCP app never starts its own session manager.
    """
    for bundle in servers:
        mcp_app = build_mcp_asgi_app(bundle.mcp, transport, protection, host)
        _guard_scopes(mcp_app, bundle)
        app.mount(bundle.mount_path, mcp_app)
    # Only ``streamable_http_app`` creates a session manager, and reading it before then raises.
    # An SSE connection runs its own session inside the request, so there is nothing to start.
    if transport == 'sse':
        return []
    return [bundle.mcp.session_manager for bundle in servers]


@contextlib.asynccontextmanager
async def serve_lifespan(
    session_managers: list[StreamableHTTPSessionManager],
    store: TokenStore,
    on_shutdown: typing.Callable[[], typing.Awaitable[None]],
) -> typing.AsyncIterator[None]:
    """Run ``session_managers`` for as long as the app serves, then run the shutdown hooks and close the store."""
    async with contextlib.AsyncExitStack() as stack:
        for session_manager in session_managers:
            await stack.enter_async_context(session_manager.run())
        yield
    await on_shutdown()
    await store.close()


def build_app(
    servers: list[_ServerBundle],
    config: GatewayConfig,
    store: TokenStore,
    on_shutdown: typing.Callable[[], typing.Awaitable[None]],
    transport: str,
    host: str,
) -> FastAPI:
    """Assemble the gateway FastAPI app with CORS, OAuth, discovery, health, and MCP mounts."""
    # Filled once the MCP apps are mounted below, which is before the lifespan runs.
    session_managers: list[StreamableHTTPSessionManager] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with serve_lifespan(session_managers, store, on_shutdown):
            yield

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

    session_managers.extend(mount_mcp_apps(app, servers, transport, config.dns_rebinding_protection, host))

    return app


def register_auth_routes(app: FastAPI, servers: list[_ServerBundle]) -> None:
    """Register the OAuth and ``.well-known`` discovery routes an embedder needs for ``servers``.

    ``mount`` adds these alongside the MCP sub-apps for a working OAuth flow.
    Register before mounting, so the explicit OAuth paths win over each catch-all ``mount_path``.
    """
    # Per-server OAuth endpoints and RFC 9728 protected-resource routes,
    # only for a bundle whose flow made the gateway the authorization server.
    # For a delegating server,
    # advertising /authorize and /token would point clients at endpoints this app does not serve.
    for bundle in servers:
        if bundle.authorization_server_metadata is None:
            continue
        mount = bundle.mount_path.rstrip('/')
        app.router.add_route(
            path=f'{mount}/.well-known/oauth-authorization-server',
            endpoint=_build_metadata_endpoint(bundle.authorization_server_metadata),
            methods=['GET', 'OPTIONS'],
        )
        if bundle.auth_provider is not None and bundle.auth_settings is not None:
            # The SDK serves the provider's OAuth endpoints, and its metadata route is replaced by the one above.
            oauth_routes = create_auth_routes(
                provider=bundle.auth_provider,
                issuer_url=bundle.auth_settings.issuer_url,
                service_documentation_url=bundle.auth_settings.service_documentation_url,
                client_registration_options=bundle.auth_settings.client_registration_options,
                revocation_options=bundle.auth_settings.revocation_options,
            )
            for route in oauth_routes:
                if route.path == '/.well-known/oauth-authorization-server':
                    continue
                app.router.add_route(
                    path=f'{mount}{route.path}', endpoint=route.endpoint, methods=route.methods, name=route.name
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

    # RFC 8414 / 9728 ``.well-known`` discovery endpoints, resolved per server at request time.
    servers_by_path = _index_servers_by_path(servers)

    @app.get('/.well-known/oauth-authorization-server')
    @app.options('/.well-known/oauth-authorization-server')
    @app.get('/.well-known/oauth-authorization-server/{server_path:path}')
    @app.options('/.well-known/oauth-authorization-server/{server_path:path}')
    async def oauth_authorization_server_discovery(request: Request, server_path: str = ''):
        bundle = servers_by_path.get(server_path.strip('/'))
        if not bundle or not bundle.auth_settings:
            return JSONResponse(
                status_code=404,
                content={'error': f'Server not found: {server_path}'},
            )
        if bundle.authorization_server_metadata is None:
            # This server delegates to an external issuer, so the gateway has no metadata to publish.
            # The protected-resource document names that issuer, and the client discovers it there.
            return JSONResponse(
                status_code=404,
                content={
                    'error': f'Server "{bundle.name}" is not an authorization server. '
                    'Read its protected resource metadata for the issuer that is.'
                },
            )
        handler = MetadataHandler(bundle.authorization_server_metadata)
        return await handler.handle(request)

    @app.get('/.well-known/oauth-protected-resource')
    @app.options('/.well-known/oauth-protected-resource')
    @app.get('/.well-known/oauth-protected-resource/{server_path:path}')
    @app.options('/.well-known/oauth-protected-resource/{server_path:path}')
    async def oauth_protected_resource_discovery(request: Request, server_path: str = ''):
        bundle = servers_by_path.get(server_path.strip('/'))
        if not bundle or not bundle.auth_settings:
            return JSONResponse(
                status_code=404,
                content={'error': f'Server not found: {server_path}'},
            )
        handler = ProtectedResourceMetadataHandler(_protected_resource(bundle))
        return await handler.handle(request)


def _transport_security(protection: DNSRebindingProtectionConfig, host: str | None) -> TransportSecuritySettings | None:
    """Settle the ``Host`` / ``Origin`` checks, where ``None`` leaves them to the SDK's default for ``host``."""
    if protection.allowed_hosts:
        return TransportSecuritySettings(
            allowed_hosts=protection.allowed_hosts,
            allowed_origins=protection.allowed_origins,
        )
    if host is None:
        # The host app is bound by someone else, so there is no bind to judge the exposure by.
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    # The SDK checks a loopback bind against the loopback names, and leaves any other bind unchecked,
    # since only the operator knows which names a proxy will put in ``Host``.
    return None


def _guard_scopes(mcp_app: Starlette, bundle: _ServerBundle) -> None:
    """Refuse a call the token's scopes do not cover with a ``403`` scope challenge, before the SDK reads the body.

    Appended rather than added, since ``add_middleware`` puts it first,
    and it needs the SDK's authentication middleware to have run.
    """
    resource_metadata_url = (
        str(build_resource_metadata_url(bundle.auth_settings.resource_server_url))
        if bundle.auth_settings and bundle.auth_settings.resource_server_url
        else None
    )
    mcp_app.user_middleware.append(
        Middleware(
            ScopeChallengeMiddleware,
            scope_registry=bundle.scope_registry,
            tool_call_target=bundle.tool_call_target,
            resource_metadata_url=resource_metadata_url,
        )
    )


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


def _build_metadata_endpoint(metadata: OAuthMetadata) -> typing.Callable[..., typing.Any]:
    """The route serving ``metadata`` as an RFC 8414 document, open to cross-origin reads."""
    return cors_middleware(MetadataHandler(metadata).handle, ['GET', 'OPTIONS'])


def _index_servers_by_path(servers: list[_ServerBundle]) -> dict[str, _ServerBundle]:
    """Each server under every path its well-known documents may be asked for at, without surrounding slashes.

    RFC 8414 and RFC 9728 insert the well-known prefix before the path of the issuer or resource URL,
    which carries any path in the gateway's ``url``.
    Some clients insert it into the MCP endpoint URL rather than the issuer, so each path is also indexed with it.
    The bare mount path and the server's name follow, for a proxy that strips the gateway's path,
    and for the URLs served before discovery followed the mount path.
    An earlier path wins where two servers would share one.
    """
    index: dict[str, _ServerBundle] = {}
    for bundle in servers:
        if bundle.auth_settings is None or bundle.auth_settings.resource_server_url is None:
            continue
        resource_path = urllib.parse.urlparse(str(bundle.auth_settings.resource_server_url)).path
        base = resource_path.removesuffix(MCP_ENDPOINT_PATH)
        for path in (base, bundle.mount_path, f'/{bundle.name}'):
            for candidate in (path, f'{path.rstrip("/")}{MCP_ENDPOINT_PATH}'):
                index.setdefault(candidate.strip('/'), bundle)
    return index


def _protected_resource(bundle: _ServerBundle) -> ProtectedResourceMetadata:
    """Return the RFC 9728 document this server publishes.

    A strategy that builds its own document supplies it, such as one delegating to an external issuer.
    Otherwise the gateway is both issuer and resource, so the document follows from the settings it built for itself.

    Callers reach this only for a bundle carrying ``authorization_server_metadata``,
    which ``ServerAuthSetup`` never holds without ``auth_settings``.
    """
    if bundle.protected_resource is not None:
        return bundle.protected_resource

    assert bundle.auth_settings is not None  # noqa: S101
    issuer = str(bundle.auth_settings.issuer_url).rstrip('/')
    registration = bundle.auth_settings.client_registration_options
    return ProtectedResourceMetadata.model_validate(
        {
            'resource': str(bundle.auth_settings.resource_server_url),
            'authorization_servers': [issuer],
            'scopes_supported': registration.valid_scopes if registration else None,
        }
    )
