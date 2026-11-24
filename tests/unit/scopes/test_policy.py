import logging

import pytest

from openapi_mcp_gateway.openapi import OperationInfo
from openapi_mcp_gateway.scopes import (
    McpUnrestrictedScopePolicy,
    UnreachableRequirement,
    UnrestrictedRequirement,
    UpstreamSpecSecurityScopePolicy,
    UpstreamUnrestrictedScopePolicy,
    build_mcp_scope_policy,
    build_upstream_scope_policy,
)
from tests.operation import PETSTORE_OPERATIONS, operation
from tests.scope import (
    CREATE_PET,
    DELETE_PET,
    DELETE_PET_UPSTREAM_REFUSAL,
    LIST_PETS,
    SCHEMES,
    SECURED_CEILING,
    SECURED_OPERATIONS,
    configured_policy,
    operation_named,
    tag_scopes,
)


# A repository API whose broad scope implies the narrow one, as GitHub's ``repo`` implies ``public_repo``.
LIST_REPOS = operation('listRepos', 'get', '/repos', security=[{'oauth': ['public_repo']}])
DELETE_REPO = operation('deleteRepo', 'delete', '/repos/{id}', security=[{'oauth': ['repo']}])


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

    def test_the_declared_scopes_are_every_scope_the_operations_name(self):
        """Every alternative counts, since each is a way to meet its operation."""
        operations = [
            LIST_PETS,
            DELETE_PET,
            operation('getPet', security=[{'oauth': ['read:pets']}, {'oauth': ['admin']}]),
            operation('health', security=[{}]),
        ]
        assert UpstreamSpecSecurityScopePolicy(SCHEMES, {}).declared_scopes(operations) == [
            'read:pets',
            'write:pets',
            'admin',
        ]

    def test_the_refusal_names_the_scopes_and_both_remedies(self):
        requirement = UpstreamSpecSecurityScopePolicy(SCHEMES, {}).requirement_for(DELETE_PET)
        assert requirement.refusal == DELETE_PET_UPSTREAM_REFUSAL


class TestSpecSecuritySignInScopes:
    """A sign-in asks the upstream for what the operations it opens declare, within the ceiling."""

    @pytest.mark.parametrize(
        ('opened_operations', 'requested'),
        [
            ([LIST_PETS], ['read:pets']),
            ([CREATE_PET], ['write:pets']),
            ([DELETE_PET], ['read:pets', 'write:pets']),
            ([], []),
        ],
    )
    def test_each_sign_in_asks_for_what_its_operations_declare(self, opened_operations, requested):
        assert _requested_scopes(SECURED_CEILING, opened_operations) == requested

    def test_the_request_keeps_the_ceilings_order(self):
        assert _requested_scopes(['write:pets', 'read:pets'], [LIST_PETS, CREATE_PET]) == ['write:pets', 'read:pets']

    @pytest.mark.parametrize('opened_operations', [[LIST_PETS], []])
    def test_a_required_scope_is_asked_for_at_every_sign_in_and_first(self, opened_operations):
        """``offline_access`` serves the sign-in itself, so no operation would ever ask for it."""
        requested = _requested_scopes(SECURED_CEILING, opened_operations, required_scopes=['offline_access'])
        assert requested == ['offline_access', *_requested_scopes(SECURED_CEILING, opened_operations)]

    def test_a_ceiling_scope_no_operation_declares_is_not_asked_for(self):
        """Only ``required_scopes`` says a scope serves every sign-in, so the ceiling alone never does."""
        assert _requested_scopes([*SECURED_CEILING, 'offline_access'], [LIST_PETS]) == ['read:pets']

    def test_a_scope_a_required_one_implies_is_not_asked_for_again(self):
        scope_implies = {'admin': ['read:pets']}
        requested = _requested_scopes(SECURED_CEILING, [LIST_PETS], scope_implies, required_scopes=['admin'])
        assert requested == ['admin']

    def test_a_declared_scope_outside_the_ceiling_is_asked_for_through_one_implying_it(self):
        scope_implies = {'admin': ['read:pets', 'write:pets']}
        assert _requested_scopes(['admin'], [LIST_PETS], scope_implies) == ['admin']
        assert _requested_scopes(['admin'], [], scope_implies) == []

    def test_a_declared_scope_nothing_in_the_ceiling_covers_is_not_asked_for(self):
        assert _requested_scopes(['read:pets'], [CREATE_PET]) == []

    @pytest.mark.parametrize(
        ('ceiling', 'opened_operations', 'requested'),
        [
            pytest.param(['public_repo', 'repo'], [LIST_REPOS], ['public_repo'], id='the narrow scope alone'),
            pytest.param(['public_repo', 'repo'], [LIST_REPOS, DELETE_REPO], ['repo'], id='the broad one covers both'),
            pytest.param(['public_repo'], [LIST_REPOS, DELETE_REPO], ['public_repo'], id='the listed narrow one'),
        ],
    )
    def test_a_scope_a_broader_chosen_one_implies_is_not_asked_for(self, ceiling, opened_operations, requested):
        """``repo`` covers ``public_repo``, so asking for both would only widen the consent screen."""
        spec_security = UpstreamSpecSecurityScopePolicy(SCHEMES, {'repo': ['public_repo']})
        assert spec_security.requested_scopes(ceiling, [], opened_operations) == requested

    @pytest.mark.parametrize('ceiling', [['admin', 'repo'], ['repo', 'admin']])
    def test_the_narrowest_implying_scope_is_asked_for_whatever_the_order(self, ceiling):
        spec_security = UpstreamSpecSecurityScopePolicy(
            SCHEMES, {'admin': ['public_repo', 'read:org'], 'repo': ['public_repo']}
        )
        assert spec_security.requested_scopes(ceiling, [], [LIST_REPOS]) == ['repo']


class TestSpecSecuritySignInAlternatives:
    """OpenAPI lets any one security requirement authorize an operation, so a sign-in asks for one of them."""

    @pytest.mark.parametrize(
        ('security', 'ceiling', 'scope_implies', 'requested'),
        [
            pytest.param(
                [{'oauth': ['read:pets']}, {'oauth': ['admin']}],
                ['read:pets', 'admin'],
                {},
                ['read:pets'],
                id='the requirement asking least',
            ),
            pytest.param(
                [{'oauth': ['admin']}, {'oauth': ['read:pets']}],
                ['admin', 'read:pets'],
                {'admin': ['read:pets', 'write:pets']},
                ['read:pets'],
                id='what a scope implies counts against it',
            ),
            pytest.param(
                [{'oauth': ['read:pets']}, {'oauth': ['admin']}],
                ['admin'],
                {},
                ['admin'],
                id='a requirement the ceiling cannot meet is passed over',
            ),
            pytest.param(
                [{'oauth': ['b']}, {'oauth': ['a']}],
                ['a', 'b'],
                {},
                ['b'],
                id='the first the spec lists among equals',
            ),
            pytest.param(
                [{'oauth': ['read:pets', 'write:pets']}, {'oauth': ['admin']}],
                ['read:pets', 'write:pets', 'admin'],
                {},
                ['admin'],
                id='a requirement needing two scopes asks more than one needing one',
            ),
            pytest.param(
                [{}, {'oauth': ['read:pets']}],
                ['read:pets'],
                {},
                [],
                id='an empty requirement needs no scope',
            ),
            pytest.param(
                [{'oauth': ['read:pets'], 'basic': []}],
                ['read:pets'],
                {},
                [],
                id='a requirement naming another credential cannot be met',
            ),
        ],
    )
    def test_each_operation_asks_for_one_requirement(self, security, ceiling, scope_implies, requested):
        spec_security = UpstreamSpecSecurityScopePolicy(SCHEMES, scope_implies)
        assert spec_security.requested_scopes(ceiling, [], [operation(security=security)]) == requested

    def test_a_requirement_the_required_scopes_meet_asks_for_nothing_more(self):
        spec_security = UpstreamSpecSecurityScopePolicy(SCHEMES, {})
        get_pet = operation(security=[{'oauth': ['admin']}, {'oauth': ['read:pets']}])
        assert spec_security.requested_scopes(['admin', 'read:pets'], ['read:pets'], [get_pet]) == ['read:pets']


class TestUnreachableOperations:
    """An operation no sign-in can open is left out with a warning, since the spec may declare it wrongly."""

    def test_an_operation_the_ceiling_cannot_meet_is_left_out(self, caplog):
        with caplog.at_level(logging.WARNING):
            kept = UpstreamSpecSecurityScopePolicy(SCHEMES, {}).drop_unreachable_operations(
                SECURED_OPERATIONS, ['read:pets'], [], 'petstore'
            )
        assert kept == [LIST_PETS]
        assert '(createPet needs write:pets, deletePet needs write:pets)' in caplog.text

    @pytest.mark.parametrize(
        ('ceiling', 'required_scopes', 'scope_implies'),
        [
            pytest.param(['read:pets', 'write:pets'], [], {}, id='the ceiling'),
            pytest.param(['read:pets'], ['write:pets'], {}, id='a required scope'),
            pytest.param(['admin'], [], {'admin': ['read:pets', 'write:pets']}, id='a scope implying them'),
        ],
    )
    def test_an_operation_some_sign_in_can_open_is_kept(self, caplog, ceiling, required_scopes, scope_implies):
        with caplog.at_level(logging.WARNING):
            kept = UpstreamSpecSecurityScopePolicy(SCHEMES, scope_implies).drop_unreachable_operations(
                SECURED_OPERATIONS, ceiling, required_scopes, 'petstore'
            )
        assert kept == list(SECURED_OPERATIONS)
        assert not caplog.messages

    def test_an_operation_needing_another_credential_is_left_out(self):
        login = operation('login', security=[{'basic': []}])
        policy = UpstreamSpecSecurityScopePolicy(SCHEMES, {})
        assert policy.drop_unreachable_operations([login, LIST_PETS], ['read:pets'], [], 'petstore') == [LIST_PETS]


class TestSignInWarnings:
    """Startup warns about a ceiling scope no sign-in asks for, and about a sign-in that asks for nothing."""

    def test_a_ceiling_scope_no_operation_declares_is_warned_about(self, caplog):
        with caplog.at_level(logging.WARNING):
            UpstreamSpecSecurityScopePolicy(SCHEMES, {}).warn_on_unasked_ceiling_scopes(
                SECURED_OPERATIONS, [*SECURED_CEILING, 'offline_access'], 'petstore'
            )
        assert caplog.messages == [
            'Server "petstore": no operation declares what auth.upstream.scopes offline_access cover, '
            'so no sign-in asks for them. Move a scope every sign-in needs to auth.upstream.required_scopes, '
            'or declare it under operations.<id>.security.'
        ]

    def test_a_ceiling_scope_implying_a_declared_one_is_not_warned_about(self, caplog):
        with caplog.at_level(logging.WARNING):
            UpstreamSpecSecurityScopePolicy(SCHEMES, {'admin': ['read:pets']}).warn_on_unasked_ceiling_scopes(
                SECURED_OPERATIONS, ['admin'], 'petstore'
            )
        assert not caplog.messages

    @pytest.mark.parametrize(
        ('operations', 'required_scopes', 'warned'),
        [
            pytest.param([operation('health', security=[{}])], [], True, id='nothing declared'),
            pytest.param([operation('health', security=[{}])], ['offline_access'], False, id='a required scope'),
            pytest.param([LIST_PETS], [], False, id='an open operation declares a scope'),
        ],
    )
    def test_a_sign_in_granted_no_mcp_scope_asking_for_nothing_is_warned_about(
        self, caplog, operations, required_scopes, warned
    ):
        """A wider grant asks for at least as much, so the grant of no MCP scope is the one to check."""
        with caplog.at_level(logging.WARNING):
            UpstreamSpecSecurityScopePolicy(SCHEMES, {}).warn_on_empty_sign_in(
                operations, SECURED_CEILING, required_scopes, McpUnrestrictedScopePolicy(), 'petstore'
            )
        assert ('its authorize request carries no scope parameter' in caplog.text) is warned

    def test_a_sign_in_whose_mcp_scopes_open_nothing_is_warned_about(self, caplog):
        """Tag-derived scopes close every operation to a grant of none."""
        with caplog.at_level(logging.WARNING):
            UpstreamSpecSecurityScopePolicy(SCHEMES, {}).warn_on_empty_sign_in(
                SECURED_OPERATIONS, SECURED_CEILING, [], tag_scopes(*SECURED_OPERATIONS), 'petstore'
            )
        assert 'its authorize request carries no scope parameter' in caplog.text


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
        policy = build_upstream_scope_policy(False, UpstreamSpecSecurityScopePolicy(SCHEMES, {}))
        assert isinstance(policy, UpstreamUnrestrictedScopePolicy)

    def test_enforcing_a_spec_without_an_oauth_scheme_restricts_nothing(self, caplog):
        """The lists stay public too, since no operation depends on the grant."""
        spec_security = UpstreamSpecSecurityScopePolicy({'basic': SCHEMES['basic']}, {})
        assert isinstance(build_upstream_scope_policy(True, spec_security), UpstreamUnrestrictedScopePolicy)
        with caplog.at_level(logging.WARNING):
            spec_security.warn_on_missing_oauth_scheme('petstore')
        assert 'the spec declares no OAuth security scheme' in caplog.text

    def test_a_spec_with_an_oauth_scheme_is_not_warned_about(self, caplog):
        with caplog.at_level(logging.WARNING):
            UpstreamSpecSecurityScopePolicy(SCHEMES, {}).warn_on_missing_oauth_scheme('petstore')
        assert not caplog.messages

    def test_without_enforcement_every_sign_in_asks_for_the_whole_ceiling(self):
        """The spec's ``security`` is not trusted then, so it cannot narrow a sign-in."""
        policy = build_upstream_scope_policy(False, UpstreamSpecSecurityScopePolicy(SCHEMES, {}))
        assert policy.requested_scopes(SECURED_CEILING, ['offline_access'], [LIST_PETS]) == [
            'offline_access',
            *SECURED_CEILING,
        ]


def _requested_scopes(
    ceiling: list[str],
    opened_operations: list[OperationInfo],
    scope_implies: dict[str, list[str]] | None = None,
    required_scopes: list[str] | None = None,
) -> list[str]:
    """What a sign-in to the secured petstore opening ``opened_operations`` asks for, beyond ``required_scopes``."""
    spec_security = UpstreamSpecSecurityScopePolicy(SCHEMES, scope_implies or {})
    return spec_security.requested_scopes(ceiling, required_scopes or [], opened_operations)
