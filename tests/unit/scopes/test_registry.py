import pytest

from openapi_mcp_gateway.scopes import (
    McpUnrestrictedScopePolicy,
    ScopeRegistry,
    TokenGrant,
    UpstreamSpecSecurityScopePolicy,
)
from tests.scope import (
    CREATE_PET,
    DELETE_PET,
    DELETE_PET_UPSTREAM_REFUSAL,
    SCHEMES,
    SECURED_CEILING,
    SECURED_OPERATIONS,
    configured_policy,
    operation_named,
    secured_registry,
    tag_scopes,
)


class TestScopeRegistry:
    """Requirements looked up by the name a tool or operation is exposed as, or the URI a resource is read at."""

    def test_a_registered_name_carries_its_requirement(self):
        scope_registry = ScopeRegistry(configured_policy(auto='tags'))
        scope_registry.register('delete_pet', operation_named('deletePet'))
        assert scope_registry.refusal('delete_pet', _mcp_grant({'pets:delete'})) is None
        assert scope_registry.refusal('delete_pet', _mcp_grant({'pets:write'})) is not None

    def test_an_unregistered_name_requires_nothing(self):
        """A dynamic meta-tool is never registered, since the operations behind it are what scopes guard."""
        scope_registry = ScopeRegistry(configured_policy(auto='tags'))
        assert scope_registry.refusal('search_operations', _mcp_grant(set())) is None

    @pytest.mark.parametrize('uri', ['petstore://pets/{petId}', 'petstore://pets/7'])
    def test_a_resource_template_covers_every_uri_it_expands_to(self, uri):
        """The template is what a listing names and the concrete URI what a read names, and both require the same."""
        scope_registry = ScopeRegistry(configured_policy(auto='tags'))
        scope_registry.register_resource('petstore://pets/{petId}', operation_named('getPetById'))
        assert scope_registry.refusal(uri, _mcp_grant({'pets:read'})) is None
        assert scope_registry.refusal(uri, _mcp_grant({'pets:write'})) is not None

    @pytest.mark.parametrize(('grantable', 'listed'), [({'pets:read', 'pets:delete'}, True), ({'pets:read'}, False)])
    def test_lists_show_what_the_client_may_step_up_for(self, grantable, listed):
        """A tool hidden until the token holds its scope could never be called, so the step-up would never start."""
        scope_registry = ScopeRegistry(configured_policy(auto='tags'))
        scope_registry.register('delete_pet', operation_named('deletePet'))
        assert scope_registry.lists('delete_pet', _mcp_grant({'pets:read'}, grantable)) is listed


class TestScopeRegistryWithUpstreamScopes:
    """A name needs the MCP scopes and the upstream grant both, and a refusal names the MCP scopes first."""

    @pytest.mark.parametrize(
        ('mcp_scopes', 'upstream_scopes', 'refused_for'),
        [
            (set(), set(), 'This operation needs one of these scopes: pets:delete.'),
            ({'pets:delete'}, {'read:pets'}, DELETE_PET_UPSTREAM_REFUSAL),
            ({'pets:delete'}, {'admin'}, None),
        ],
    )
    def test_the_mcp_scopes_are_refused_before_the_upstream_grant(self, mcp_scopes, upstream_scopes, refused_for):
        """Only the MCP scopes can be stepped up for, so a client hears about them first."""
        scope_registry = ScopeRegistry(
            tag_scopes(DELETE_PET),
            UpstreamSpecSecurityScopePolicy(SCHEMES, {'admin': ['read:pets', 'write:pets']}),
        )
        scope_registry.register('delete_pet', DELETE_PET)
        grant = TokenGrant(frozenset(mcp_scopes), frozenset(upstream_scopes), frozenset(mcp_scopes))
        refusal = scope_registry.refusal('delete_pet', grant)
        assert refusal == refused_for

    @pytest.mark.parametrize(
        ('mcp_scopes', 'upstream_scopes', 'listed'),
        [
            (set(), set(), True),
            ({'pets:delete'}, {'read:pets'}, False),
            ({'pets:delete'}, {'read:pets', 'write:pets'}, True),
        ],
        ids=['mcp scopes still to step up for', 'mcp scopes held, grant short', 'both held'],
    )
    def test_lists_ignore_the_upstream_grant_until_the_mcp_scopes_are_held(self, mcp_scopes, upstream_scopes, listed):
        """The sign-in a step-up starts asks the upstream again, so only a short grant behind held scopes is final."""
        scope_registry = ScopeRegistry(tag_scopes(DELETE_PET), UpstreamSpecSecurityScopePolicy(SCHEMES, {}))
        scope_registry.register('delete_pet', DELETE_PET)
        grant = TokenGrant(frozenset(mcp_scopes), frozenset(upstream_scopes), frozenset({'pets:delete'}))
        assert scope_registry.lists('delete_pet', grant) is listed

    def test_the_lists_are_cached_per_token_once_the_grant_is_enforced(self):
        scope_registry = ScopeRegistry(McpUnrestrictedScopePolicy(), UpstreamSpecSecurityScopePolicy(SCHEMES, {}))
        assert scope_registry.list_cache_scope == 'private'


def _mcp_grant(mcp_scopes: set[str], grantable: set[str] | None = None) -> TokenGrant:
    """A grant carrying ``mcp_scopes`` and nothing from the upstream, whose client may be granted ``grantable``.

    The client may be granted only ``mcp_scopes`` when ``grantable`` is ``None``.
    """
    return TokenGrant(frozenset(mcp_scopes), frozenset(), frozenset(mcp_scopes if grantable is None else grantable))


class TestUpstreamSignInScopes:
    """A sign-in asks the upstream for what the registered operations its MCP scopes open need."""

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
        assert secured_registry().upstream_sign_in_scopes(SECURED_CEILING, [], mcp_scopes) == requested

    def test_without_mcp_scopes_every_registered_operation_counts(self):
        scope_registry = ScopeRegistry(McpUnrestrictedScopePolicy(), UpstreamSpecSecurityScopePolicy(SCHEMES, {}))
        scope_registry.register('delete_pet', DELETE_PET)
        assert scope_registry.upstream_sign_in_scopes(SECURED_CEILING, [], []) == SECURED_CEILING

    def test_without_an_enforced_grant_every_sign_in_asks_for_the_whole_ceiling(self):
        scope_registry = ScopeRegistry(tag_scopes(DELETE_PET))
        scope_registry.register('delete_pet', DELETE_PET)
        assert scope_registry.upstream_sign_in_scopes(SECURED_CEILING, [], []) == SECURED_CEILING

    def test_an_operation_registered_after_a_sign_in_counts_at_the_next_one(self):
        scope_registry = ScopeRegistry(tag_scopes(*SECURED_OPERATIONS), UpstreamSpecSecurityScopePolicy(SCHEMES, {}))
        scope_registry.register('delete_pet', DELETE_PET)
        assert scope_registry.upstream_sign_in_scopes(SECURED_CEILING, [], ['pets:write']) == []
        scope_registry.register('create_pet', CREATE_PET)
        assert scope_registry.upstream_sign_in_scopes(SECURED_CEILING, [], ['pets:write']) == ['write:pets']

    def test_a_scope_the_server_does_not_define_changes_nothing(self):
        scope_registry = secured_registry()
        assert scope_registry.upstream_sign_in_scopes(SECURED_CEILING, [], ['pets:read', 'unknown']) == ['read:pets']
