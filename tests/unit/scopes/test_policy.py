import logging

import pytest

from openapi_mcp_gateway.scopes import (
    McpUnrestrictedScopePolicy,
    UnreachableRequirement,
    UnrestrictedRequirement,
    UpstreamSpecSecurityScopePolicy,
    UpstreamUnrestrictedScopePolicy,
    build_mcp_scope_policy,
    build_upstream_scope_policy,
)
from tests.operations import PETSTORE_OPERATIONS, operation
from tests.scopes import (
    DELETE_PET,
    DELETE_PET_UPSTREAM_REFUSAL,
    SCHEMES,
    configured_policy,
    operation_named,
    tag_scopes,
)


# A petstore whose operations declare upstream scopes, with tags for the MCP scopes beside them.
LIST_PETS = operation('listPets', 'get', '/pets', security=[{'oauth': ['read:pets']}])


class TestConfiguredMcpScopePolicy:
    """``auth.mcp.operation_scopes`` resolved against a server's operations."""

    def test_derived_and_manual_scopes_add_up(self):
        """An operation a manual scope also covers opens to either scope."""
        policy = configured_policy(auto='tags', manual={'admin': {'description': 'Admin', 'operations': ['DELETE *']}})
        requirement = policy.requirement_for(operation_named('deletePet'))
        assert requirement.challenge == ('admin', 'pets:delete')

    @pytest.mark.parametrize(
        ('unscoped', 'expected'), [('deny', UnreachableRequirement), ('allow', UnrestrictedRequirement)]
    )
    def test_unscoped_decides_an_operation_no_scope_covers(self, unscoped, expected):
        """An untagged operation under ``auto: tags`` is unscoped, like any other no scope covers."""
        policy = configured_policy(auto='tags', unscoped=unscoped)
        assert isinstance(policy.requirement_for(operation_named('adminListPets')), expected)

    def test_defined_mcp_scopes_carry_descriptions(self):
        """Every scope is listed with what a person approving it is shown."""
        policy = configured_policy(
            auto='tags', manual={'admin': {'description': 'Manage pets', 'operations': ['admin*']}}
        )
        assert policy.mcp_defined_scopes == {
            'pets:read': 'Read pets',
            'pets:write': 'Create and change pets',
            'pets:delete': 'Delete pets',
            'admin': 'Manage pets',
        }

    def test_a_manual_scope_cannot_reuse_a_derived_name(self):
        """The two definitions would otherwise merge into one scope without anyone noticing."""
        with pytest.raises(ValueError, match=r'reuses names .* \(pets:read\)'):
            configured_policy(auto='tags', manual={'pets:read': {'description': 'Read', 'operations': ['GET *']}})

    def test_a_pattern_matching_nothing_is_warned(self, caplog):
        """A typo in a pattern only ever shortens what a scope opens, so it is reported at startup."""
        with caplog.at_level(logging.WARNING):
            configured_policy(manual={'admin': {'description': 'Admin', 'operations': ['DELETE /pest/*']}})
        assert 'DELETE /pest/*' in caplog.text

    @pytest.mark.parametrize(('unscoped', 'level'), [('deny', logging.WARNING), ('allow', logging.INFO)])
    def test_unscoped_operations_are_counted_at_startup(self, caplog, unscoped, level):
        """Under ``deny`` they vanish from every client, so the count is a warning there."""
        with caplog.at_level(logging.INFO):
            configured_policy(auto='tags', unscoped=unscoped)
        [record] = [record for record in caplog.records if 'no scope covers' in record.message]
        assert record.levelno == level
        assert '2 operation(s)' in record.message
        assert 'createPetWithOwner, adminListPets' in record.message

    def test_lists_vary_by_token(self):
        """``tools/list`` is cached per token once scopes filter it."""
        assert configured_policy(auto='tags').list_cache_scope == 'private'

    @pytest.mark.parametrize(('unscoped', 'opens'), [('deny', False), ('allow', True)])
    def test_unscoped_decides_whether_uncovered_operations_are_open(self, unscoped, opens):
        assert configured_policy(auto='tags', unscoped=unscoped).opens_unscoped_operations is opens


class TestUnrestrictedMcpScopePolicy:
    """A server without ``auth.mcp.operation_scopes`` restricts nothing."""

    def test_every_operation_is_open(self):
        policy = build_mcp_scope_policy(None, PETSTORE_OPERATIONS, 'petstore')
        assert isinstance(policy, McpUnrestrictedScopePolicy)
        assert all(
            isinstance(policy.requirement_for(candidate), UnrestrictedRequirement) for candidate in PETSTORE_OPERATIONS
        )
        assert policy.mcp_defined_scopes == {}
        assert policy.list_cache_scope == 'public'
        assert policy.opens_unscoped_operations is True


class TestSpecSecurityUpstreamScopePolicy:
    """Each requirement is checked against the one credential the gateway holds upstream, an OAuth token."""

    @pytest.mark.parametrize(
        ('security', 'granted', 'met'),
        [
            pytest.param([], set(), True, id='empty security needs nothing'),
            pytest.param([{}, {'oauth': ['a']}], set(), True, id='an empty requirement allows anyone'),
            pytest.param([{'oauth': []}], set(), True, id='an OAuth requirement without scopes needs only the token'),
            pytest.param([{'oauth': ['a', 'b']}], {'a'}, False, id='scopes in one requirement are all needed'),
            pytest.param([{'oauth': ['a', 'b']}], {'a', 'b'}, True, id='holding them all meets the requirement'),
            pytest.param([{'oauth': ['a']}, {'oauth': ['b']}], {'b'}, True, id='any one requirement is enough'),
            pytest.param([{'oidc': ['a']}], {'a'}, True, id='OpenID Connect names OAuth scopes too'),
            pytest.param([{'basic': []}], {'a'}, False, id='another credential cannot be met'),
            pytest.param([{'basic': []}, {'oauth': ['a']}], {'a'}, True, id='the OAuth alternative still opens it'),
            pytest.param([{'oauth': ['a'], 'basic': []}], {'a'}, False, id='a requirement needing both cannot be met'),
        ],
    )
    def test_a_requirement_follows_the_openapi_semantics(self, security, granted, met):
        requirement = UpstreamSpecSecurityScopePolicy(SCHEMES, {}).requirement_for(operation(security=security))
        assert requirement.is_met(frozenset(granted)) is met

    def test_a_spec_without_an_oauth_scheme_restricts_nothing(self):
        """Such a spec describes some other credential, so it says nothing about the token the gateway holds."""
        spec_security = UpstreamSpecSecurityScopePolicy({'basic': SCHEMES['basic']}, {})
        assert spec_security.requirement_for(operation(security=[{'basic': []}])).is_met(frozenset())

    def test_a_scope_holds_what_it_implies_transitively(self):
        spec_security = UpstreamSpecSecurityScopePolicy(
            SCHEMES, {'repo': ['public_repo'], 'public_repo': ['repo:read']}
        )
        assert spec_security.held_scopes(['repo']) == {'repo', 'public_repo', 'repo:read'}

    def test_the_requested_scopes_are_every_scope_the_operations_name(self):
        """Every alternative is requested, since the gateway cannot tell which one the user will be granted."""
        operations = [
            LIST_PETS,
            DELETE_PET,
            operation('getPet', security=[{'oauth': ['read:pets']}, {'oauth': ['admin']}]),
            operation('health', security=[{}]),
        ]
        assert UpstreamSpecSecurityScopePolicy(SCHEMES, {}).requested_scopes(operations) == [
            'read:pets',
            'write:pets',
            'admin',
        ]

    def test_a_scope_a_broader_requested_one_implies_is_not_requested(self):
        """``repo`` covers ``public_repo``, so asking for both would only widen the consent screen."""
        operations = [
            operation('listRepos', security=[{'oauth': ['public_repo']}]),
            operation('deleteRepo', security=[{'oauth': ['repo']}]),
        ]
        spec_security = UpstreamSpecSecurityScopePolicy(SCHEMES, {'repo': ['public_repo']})
        assert spec_security.requested_scopes(operations) == ['repo']

    def test_the_refusal_names_the_scopes_and_both_remedies(self):
        requirement = UpstreamSpecSecurityScopePolicy(SCHEMES, {}).requirement_for(DELETE_PET)
        assert requirement.refusal == DELETE_PET_UPSTREAM_REFUSAL


class TestUnrequestedScopeWarning:
    """Startup warns about the clients that can reach operations the requested upstream scopes cannot open."""

    def test_each_client_is_warned_about_what_it_can_reach(self, caplog):
        """A reader never reaches ``deletePet``, so only the deleter hears about the scope it needs."""
        mcp_policy = tag_scopes(LIST_PETS, DELETE_PET)
        with caplog.at_level(logging.WARNING):
            UpstreamSpecSecurityScopePolicy(SCHEMES, {}).warn_on_unrequested_scopes(
                [LIST_PETS, DELETE_PET],
                ['read:pets'],
                mcp_policy,
                {'reader': ['pets:read'], 'deleter': ['pets:read', 'pets:delete']},
                'petstore',
            )
        assert caplog.messages == [
            'Server "petstore": "deleter" can reach 1 operation(s) the requested upstream scopes cannot open '
            '(deletePet needs write:pets). '
            'Add the scopes to auth.upstream.scopes, or correct an operation under operations.<id>.security.'
        ]

    def test_without_clients_every_operation_counts(self, caplog):
        operations = [LIST_PETS, operation('login', security=[{'basic': []}])]
        with caplog.at_level(logging.WARNING):
            UpstreamSpecSecurityScopePolicy(SCHEMES, {}).warn_on_unrequested_scopes(
                operations, [], McpUnrestrictedScopePolicy(), {}, 'petstore'
            )
        assert '"petstore": every client can reach 2 operation(s)' in caplog.text
        assert '(listPets needs read:pets, login needs a non-OAuth credential)' in caplog.text


class TestUpstreamScopePolicy:
    """Only ``auth.upstream.enforce_scopes`` makes the upstream grant restrict anything."""

    def test_without_enforcement_the_grant_restricts_nothing(self):
        policy = build_upstream_scope_policy(False, UpstreamSpecSecurityScopePolicy(SCHEMES, {}), 'petstore')
        assert isinstance(policy, UpstreamUnrestrictedScopePolicy)

    def test_enforcing_a_spec_without_an_oauth_scheme_restricts_nothing(self, caplog):
        """The lists stay public too, since no operation depends on the grant."""
        with caplog.at_level(logging.WARNING):
            policy = build_upstream_scope_policy(
                True, UpstreamSpecSecurityScopePolicy({'basic': SCHEMES['basic']}, {}), 'petstore'
            )
        assert isinstance(policy, UpstreamUnrestrictedScopePolicy)
        assert 'the spec declares no OAuth security scheme' in caplog.text
