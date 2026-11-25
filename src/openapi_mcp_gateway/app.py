import contextlib
import logging
import typing
import urllib.parse

from fastapi import FastAPI
from mcp.server import MCPServer
from mcp.server.auth.handlers.metadata import MetadataHandler, ProtectedResourceMetadataHandler
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    cors_middleware,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth.assembly import MCP_ENDPOINT_PATH, ServerAuthSetup
from .openapi import ExposedTool, OpenAPISpec
from .scopes import RequestTarget, ScopeChallengeMiddleware, ScopeRegistry
from .settings import DNSRebindingProtectionConfig, GatewayConfig
from .stores.base import TokenStore


logger = logging.getLogger(__name__)


class ServerBundle(typing.NamedTuple):
    """One registered MCP server, holding the objects the gateway mounts and what those objects would serve.

    The second half is captured at registration because it cannot be recovered afterwards.
    A tool name comes out of naming and shaping, a base URL may have come from the spec rather than the config,
    and an OAuth flow may have been resolved rather than declared.

    ``describe`` reports no credential.
    ``auth`` holds the resolver that carries the upstream credential,
    and the document gets piped, pasted into issues and rendered in a browser, so only named fields leave it.
    """

    # Mounted onto the FastAPI app.
    name: str
    mount_path: str
    mcp: MCPServer
    spec: OpenAPISpec
    # Both halves of the server's auth, as its strategies built them.
    auth: ServerAuthSetup
    # What each tool or operation requires of a token's scopes, and how a call names the one it reaches.
    scope_registry: ScopeRegistry
    tool_call_target: RequestTarget

    # Read back by ``describe`` and by the dry-run table.
    base_url: str = ''
    exposure: str = 'static'
    instructions: str | None = None
    tools: tuple[ExposedTool, ...] = ()
    resource_names: tuple[str, ...] = ()
    auth_type: str = 'none'

    # Prose for the dry-run table only.
    # ``describe`` reports structured fields instead, so that a caller never has to read a sentence to find a value.
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
                'upstream': {'type': self.auth_type, **self.auth.upstream_description.resolved_settings},
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
        settings, protected_resource = self.auth.settings, self.auth.protected_resource
        if settings is None or protected_resource is None:
            return {'issuer': None, 'grant_types': None, 'required_scopes': None}
        metadata = self.auth.authorization_server_metadata
        return {
            'issuer': str(protected_resource.authorization_servers[0]),
            'grant_types': metadata.grant_types_supported if metadata else None,
            'required_scopes': list(settings.required_scopes or []),
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
    servers: list[ServerBundle],
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
    servers: list[ServerBundle],
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


def register_auth_routes(app: FastAPI, servers: list[ServerBundle]) -> None:
    """Register the OAuth and ``.well-known`` discovery routes an embedder needs for ``servers``.

    ``mount`` adds these alongside the MCP sub-apps for a working OAuth flow.
    Register before mounting, so the explicit OAuth paths win over each catch-all ``mount_path``.
    """
    # Per-server OAuth endpoints and RFC 9728 protected-resource routes,
    # only for a bundle whose flow made the gateway the authorization server.
    # For a delegating server,
    # advertising /authorize and /token would point clients at endpoints this app does not serve.
    for bundle in servers:
        if bundle.auth.authorization_server_metadata is None:
            continue
        mount = bundle.mount_path.rstrip('/')
        app.router.add_route(
            path=f'{mount}/.well-known/oauth-authorization-server',
            endpoint=_build_metadata_endpoint(bundle.auth.authorization_server_metadata),
            methods=['GET', 'OPTIONS'],
        )
        if bundle.auth.provider is not None and bundle.auth.settings is not None:
            # The SDK serves the provider's OAuth endpoints, and its metadata route is replaced by the one above.
            oauth_routes = create_auth_routes(
                provider=bundle.auth.provider,
                issuer_url=bundle.auth.settings.issuer_url,
                service_documentation_url=bundle.auth.settings.service_documentation_url,
                client_registration_options=bundle.auth.settings.client_registration_options,
                revocation_options=bundle.auth.settings.revocation_options,
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

    # RFC 8414 / 9728 ``.well-known`` discovery endpoints, one route for each path a server may be asked for at.
    # Explicit paths rather than a catch-all, so an app embedding the gateway keeps its own discovery documents.
    for path, bundle in _index_servers_by_path(servers).items():
        suffix = f'/{path}' if path else ''
        app.router.add_route(
            path=f'/.well-known/oauth-authorization-server{suffix}',
            endpoint=_build_authorization_server_discovery(bundle),
            methods=['GET', 'OPTIONS'],
        )
        app.router.add_route(
            path=f'/.well-known/oauth-protected-resource{suffix}',
            endpoint=_build_protected_resource_endpoint(_protected_resource(bundle)),
            methods=['GET', 'OPTIONS'],
        )


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


def _guard_scopes(mcp_app: Starlette, bundle: ServerBundle) -> None:
    """Refuse a call the token's scopes do not cover with a ``403`` scope challenge, before the SDK reads the body.

    Appended rather than added, since ``add_middleware`` puts it first,
    and it needs the SDK's authentication middleware to have run.
    """
    resource_metadata_url = (
        str(build_resource_metadata_url(bundle.auth.settings.resource_server_url))
        if bundle.auth.settings and bundle.auth.settings.resource_server_url
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


def _register_health_route(app: FastAPI, servers: list[ServerBundle]) -> None:
    """Register ``/healthz`` reporting per-server name, mount path, and how each side of its auth works."""

    @app.get('/healthz')
    async def healthz():
        return {
            'status': 'ok',
            'servers': [
                {
                    'name': bundle.name,
                    'path': bundle.mount_path,
                    'title': bundle.spec.title,
                    # The same labels as the dry-run table, so an operator reads one vocabulary everywhere.
                    'auth': {'mcp': bundle.auth.mcp_summary, 'upstream': bundle.auth.upstream_description.summary},
                }
                for bundle in servers
            ],
        }


def _build_metadata_endpoint(metadata: OAuthMetadata) -> typing.Callable[..., typing.Any]:
    """The route serving ``metadata`` as an RFC 8414 document, open to cross-origin reads."""
    return cors_middleware(MetadataHandler(metadata).handle, ['GET', 'OPTIONS'])


def _build_protected_resource_endpoint(document: ProtectedResourceMetadata) -> typing.Callable[..., typing.Any]:
    """The route serving ``document`` as an RFC 9728 document, open to cross-origin reads."""
    return cors_middleware(ProtectedResourceMetadataHandler(document).handle, ['GET', 'OPTIONS'])


def _build_authorization_server_discovery(bundle: ServerBundle) -> typing.Callable[..., typing.Any]:
    """The route serving ``bundle``'s RFC 8414 document, or explaining why a delegating server has none."""
    if bundle.auth.authorization_server_metadata is not None:
        return _build_metadata_endpoint(bundle.auth.authorization_server_metadata)

    async def explain_external_issuer(_request: Request) -> JSONResponse:
        # This server delegates to an external issuer, so the gateway has no metadata to publish.
        # The protected-resource document names that issuer, and the client discovers it there.
        return JSONResponse(
            status_code=404,
            content={
                'error': f'Server "{bundle.name}" is not an authorization server. '
                'Read its protected resource metadata for the issuer that is.'
            },
        )

    return explain_external_issuer


def _index_servers_by_path(servers: list[ServerBundle]) -> dict[str, ServerBundle]:
    """Each server under every path its well-known documents may be asked for at, without surrounding slashes.

    RFC 8414 and RFC 9728 insert the well-known prefix before the path of the issuer or resource URL,
    which carries any path in the gateway's ``url``.
    Some clients insert it into the MCP endpoint URL rather than the issuer, so each path is also indexed with it.
    The bare mount path and the server's name follow, for a proxy that strips the gateway's path,
    and for the URLs served before discovery followed the mount path.
    Every server's issuer and resource paths are indexed before any fallback,
    so a fallback never takes another server's real path.
    Otherwise an earlier server wins where two would share one.
    """
    discoverable = [
        bundle
        for bundle in servers
        if bundle.auth.settings is not None and bundle.auth.settings.resource_server_url is not None
    ]
    published = [(bundle, (_find_issuer_path(bundle),)) for bundle in discoverable]
    fallbacks = [(bundle, (bundle.mount_path, f'/{bundle.name}')) for bundle in discoverable]
    index: dict[str, ServerBundle] = {}
    for bundle, paths in [*published, *fallbacks]:
        for path in paths:
            for candidate in (path, f'{path.rstrip("/")}{MCP_ENDPOINT_PATH}'):
                index.setdefault(candidate.strip('/'), bundle)
    return index


def _find_issuer_path(bundle: ServerBundle) -> str:
    """The path of ``bundle``'s issuer URL, which its resource URL extends with the MCP endpoint path."""
    assert bundle.auth.settings is not None and bundle.auth.settings.resource_server_url is not None  # noqa: S101
    resource_path = urllib.parse.urlparse(str(bundle.auth.settings.resource_server_url)).path
    return resource_path.removesuffix(MCP_ENDPOINT_PATH)


def _protected_resource(bundle: ServerBundle) -> ProtectedResourceMetadata:
    """The RFC 9728 document ``bundle``'s auth built, which it holds wherever it checks tokens.

    Callers reach this only for a bundle that checks tokens, since every other one publishes no discovery document.
    """
    assert bundle.auth.protected_resource is not None  # noqa: S101
    return bundle.auth.protected_resource
