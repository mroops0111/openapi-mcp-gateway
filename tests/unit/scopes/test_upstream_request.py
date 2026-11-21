import pytest

from openapi_mcp_gateway.scopes import (
    McpUnrestrictedScopePolicy,
    UpstreamDownscopedScopeRequest,
    UpstreamFixedScopeRequest,
    UpstreamSpecSecurityScopePolicy,
    build_upstream_scope_request,
)
from tests.operation import operation
from tests.scope import DELETE_PET, SCHEMES, tag_scopes


# A petstore whose operations declare upstream scopes, with tags for the MCP scopes beside them.
LIST_PETS = operation('listPets', 'get', '/pets', security=[{'oauth': ['read:pets']}])
CREATE_PET = operation('createPet', 'post', '/pets', security=[{'oauth': ['write:pets']}])
SECURED_OPERATIONS = (LIST_PETS, CREATE_PET, DELETE_PET)

# The most a petstore sign-in may ask the upstream for.
CEILING = ['read:pets', 'write:pets']


class TestDownscopedScopeRequest:
    """A sign-in asks the upstream for what the operations its MCP scopes open declare, within the ceiling."""

    @pytest.mark.parametrize(
        ('mcp_scopes', 'requested'),
        [
            (['pets:read'], ['read:pets']),
            (['pets:write'], ['write:pets']),
            (['pets:delete'], ['read:pets', 'write:pets']),
            ([], []),
        ],
    )
    def test_each_sign_in_asks_for_what_its_mcp_scopes_open(self, mcp_scopes, requested):
        assert _downscoped(CEILING).scopes_for(mcp_scopes) == requested

    def test_the_request_keeps_the_ceilings_order(self):
        assert _downscoped(['write:pets', 'read:pets']).scopes_for(['pets:read', 'pets:write']) == [
            'write:pets',
            'read:pets',
        ]

    def test_a_scope_no_operation_declares_is_asked_for_at_every_sign_in(self):
        """``offline_access`` serves the sign-in itself, so no MCP grant would ever ask for it."""
        request = _downscoped([*CEILING, 'offline_access'])
        assert request.scopes_for(['pets:read']) == ['read:pets', 'offline_access']

    def test_a_declared_scope_outside_the_ceiling_is_asked_for_through_one_implying_it(self):
        request = _downscoped(['admin'], scope_implies={'admin': ['read:pets', 'write:pets']})
        assert request.scopes_for(['pets:read']) == ['admin']
        assert request.scopes_for([]) == []

    def test_a_declared_scope_nothing_in_the_ceiling_covers_is_not_asked_for(self):
        assert _downscoped(['read:pets']).scopes_for(['pets:write']) == []

    def test_without_mcp_scopes_every_operation_counts(self):
        request = UpstreamDownscopedScopeRequest(
            CEILING, SECURED_OPERATIONS, McpUnrestrictedScopePolicy(), UpstreamSpecSecurityScopePolicy(SCHEMES, {})
        )
        assert request.scopes_for([]) == CEILING


class TestBuildUpstreamScopeRequest:
    """The spec's ``security`` decides a sign-in's upstream scopes only once ``enforce_scopes`` trusts it."""

    def test_without_enforce_scopes_every_sign_in_asks_for_the_whole_ceiling(self):
        request = _build(enforce_scopes=False)
        assert isinstance(request, UpstreamFixedScopeRequest)
        assert request.scopes_for(['pets:read']) == CEILING

    def test_enforce_scopes_follows_the_mcp_grant(self):
        assert _build(enforce_scopes=True).scopes_for(['pets:read']) == ['read:pets']


def _downscoped(
    ceiling: list[str], scope_implies: dict[str, list[str]] | None = None
) -> UpstreamDownscopedScopeRequest:
    """A request over the secured petstore with tag-derived MCP scopes, asking for at most ``ceiling``."""
    return UpstreamDownscopedScopeRequest(
        ceiling,
        SECURED_OPERATIONS,
        tag_scopes(*SECURED_OPERATIONS),
        UpstreamSpecSecurityScopePolicy(SCHEMES, scope_implies or {}),
    )


def _build(enforce_scopes: bool):
    """The request a secured petstore server with tag-derived MCP scopes builds."""
    return build_upstream_scope_request(
        enforce_scopes,
        CEILING,
        SECURED_OPERATIONS,
        tag_scopes(*SECURED_OPERATIONS),
        UpstreamSpecSecurityScopePolicy(SCHEMES, {}),
    )
