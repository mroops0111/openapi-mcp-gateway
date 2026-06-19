from fastapi import FastAPI

from openapi_mcp_gateway.auth.detector import DetectedOAuthFlow
from openapi_mcp_gateway.fastapi import (
    ToolMetadata,
    collect_marked_routes,
    filter_marked_operations,
    get_tool_metadata,
    infer_auth_from_declared_flows,
    mcp_tool,
    override_with_metadata,
)
from openapi_mcp_gateway.openapi import OperationInfo, parse_spec


def _build_app() -> FastAPI:
    """Tiny FastAPI app used across the tests in this module."""
    app = FastAPI()

    @app.get('/users/{user_id}')
    @mcp_tool(name='lookup_user', description='Find one user.')
    def get_user(user_id: str):
        return {'user_id': user_id}

    @app.get('/users')
    @mcp_tool()
    def list_users():
        return []

    @app.get('/admin/reset')
    def reset():
        return {'ok': True}

    @app.get('/health')
    @mcp_tool(expose=False)
    def health():
        return {'ok': True}

    return app


class TestMcpToolDecorator:
    """``@mcp_tool`` attaches per-route override metadata."""

    def test_default_metadata_marks_route_exposed(self):
        """Calling ``@mcp_tool()`` without arguments exposes the route with no overrides."""

        @mcp_tool()
        def handler():
            return None

        meta = get_tool_metadata(handler)
        assert meta is not None
        assert meta.expose is True
        assert meta.name is None
        assert meta.description is None

    def test_overrides_recorded(self):
        """``name`` / ``description`` arguments survive on the function attribute."""

        @mcp_tool(name='lookup_user', description='Find one user.')
        def handler():
            return None

        meta = get_tool_metadata(handler)
        assert meta == ToolMetadata(name='lookup_user', description='Find one user.', expose=True)

    def test_expose_false_recorded(self):
        """``expose=False`` is preserved so callers can opt out without removing the decorator."""

        @mcp_tool(expose=False)
        def handler():
            return None

        meta = get_tool_metadata(handler)
        assert meta is not None
        assert meta.expose is False


class TestCollectMarkedRoutes:
    """``collect_marked_routes`` returns FastAPI routes that opt in to MCP exposure."""

    def test_returns_only_exposed_routes(self):
        """Routes without ``@mcp_tool`` and ``expose=False`` ones are dropped."""
        app = _build_app()
        selections = collect_marked_routes(app)
        keys = {(selection.method, selection.path) for selection in selections}
        assert keys == {('get', '/users/{user_id}'), ('get', '/users')}

    def test_metadata_attached_per_selection(self):
        """The returned ``RouteSelection`` carries the decorator's metadata."""
        app = _build_app()
        selections = {(s.method, s.path): s for s in collect_marked_routes(app)}
        named = selections[('get', '/users/{user_id}')]
        assert named.metadata.name == 'lookup_user'
        assert named.metadata.description == 'Find one user.'


class TestFilterMarkedOperations:
    """``filter_marked_operations`` keeps only the operations covered by selections."""

    def test_drops_operations_outside_selection(self):
        """Routes without ``@mcp_tool`` (or with ``expose=False``) are excluded from the pairing."""
        app = _build_app()
        spec = parse_spec(app.openapi())
        selections = collect_marked_routes(app)

        paired = filter_marked_operations(spec.operations, selections)
        keys = {(operation.method, operation.path) for operation, _ in paired}
        assert keys == {('get', '/users/{user_id}'), ('get', '/users')}

    def test_pairs_each_operation_with_its_metadata(self):
        """Returned pairs carry the decorator's metadata for direct override application."""
        app = _build_app()
        spec = parse_spec(app.openapi())
        selections = collect_marked_routes(app)

        paired = filter_marked_operations(spec.operations, selections)
        by_path: dict[str, ToolMetadata] = {operation.path: metadata for operation, metadata in paired}
        assert by_path['/users/{user_id}'].name == 'lookup_user'
        assert by_path['/users/{user_id}'].description == 'Find one user.'


class TestOverrideWithMetadata:
    """``override_with_metadata`` rewrites operation_id / description from decorator state."""

    def test_applies_name_and_description(self):
        """``metadata.name`` rewrites ``operation_id``; ``description`` replaces the OpenAPI text."""
        app = _build_app()
        spec = parse_spec(app.openapi())
        selections = collect_marked_routes(app)
        paired = filter_marked_operations(spec.operations, selections)

        overridden: dict[tuple[str, str], OperationInfo] = {
            (operation.method, operation.path): override_with_metadata(operation, metadata)
            for operation, metadata in paired
        }
        assert overridden[('get', '/users/{user_id}')].operation_id == 'lookup_user'
        assert overridden[('get', '/users/{user_id}')].description == 'Find one user.'

    def test_no_overrides_returns_same_instance(self):
        """When metadata has no overrides, the operation passes through untouched."""
        app = _build_app()
        spec = parse_spec(app.openapi())
        selections = collect_marked_routes(app)
        paired = filter_marked_operations(spec.operations, selections)

        plain_operation, plain_metadata = next(
            (operation, metadata) for operation, metadata in paired if operation.path == '/users'
        )
        # The "/users" route has @mcp_tool() with no overrides, so the helper should be a no-op.
        result = override_with_metadata(plain_operation, plain_metadata)
        assert result is plain_operation


def test_get_tool_metadata_returns_none_for_undecorated():
    """Plain functions without ``@mcp_tool`` produce ``None``."""

    def handler():
        return None

    assert get_tool_metadata(handler) is None


class TestInferAuthFromDeclaredFlows:
    """``infer_auth_from_declared_flows`` defaults FastAPI integration to passthrough.

    The gateway is mounted onto the same app it exposes, so gateway and upstream share the OAuth audience.
    Forwarding the MCP client's ``Authorization`` header verbatim does not violate RFC 8707.
    For third-party APIs the user passes an explicit ``auth=AuthConfig(...)`` instead.
    """

    def test_no_declared_flows_defaults_to_none(self):
        """Apps without any declared OAuth flow do not need authentication wiring."""
        config = infer_auth_from_declared_flows([])
        assert config.type == 'none'
        assert config.flow is None

    def test_declared_flows_default_to_explicit_passthrough(self):
        """Declared flows opt into passthrough explicitly, not via a silent factory fallback."""
        declared = [DetectedOAuthFlow(flow_type='authorization_code', token_url='https://x/token')]
        config = infer_auth_from_declared_flows(declared)
        assert config.type == 'oauth2'
        assert config.flow == 'passthrough'
