import logging

import pytest

from openapi_mcp_gateway.auth.flows.authorization_code import UpstreamTokenResponse
from openapi_mcp_gateway.scopes import (
    OperationAccess,
    ScopeCatalog,
    SpecSecurityScopes,
    TokenGrant,
    UnenforcedUpstreamScopes,
    UnrestrictedPolicy,
    build_upstream_scope_policy,
)
from openapi_mcp_gateway.settings import OperationScopesConfig
from tests.operations import operation


# The security schemes of a spec that takes an OAuth token, an OpenID Connect token, or HTTP basic.
SCHEMES = {
    'oauth': {'type': 'oauth2'},
    'oidc': {'type': 'openIdConnect'},
    'basic': {'type': 'http', 'scheme': 'basic'},
}

# A petstore whose operations declare upstream scopes, with tags for the MCP scopes beside them.
LIST_PETS = operation('listPets', 'get', '/pets', security=[{'oauth': ['read:pets']}])
DELETE_PET = operation('deletePet', 'delete', '/pets/{petId}', security=[{'oauth': ['read:pets', 'write:pets']}])

# Why a token whose upstream grant lacks what ``deletePet`` declares is refused.
DELETE_PET_UPSTREAM_REFUSAL = (
    'The upstream token lacks the scopes deletePet needs (read:pets write:pets). '
    'Add them to auth.upstream.scopes and sign in again, or correct its security under operations.deletePet.security.'
)


class TestSpecSecurityScopes:
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
        requirement = SpecSecurityScopes(SCHEMES, {}).requirement_for(operation(security=security))
        assert requirement.is_met(frozenset(granted)) is met

    def test_a_spec_without_an_oauth_scheme_restricts_nothing(self):
        """Such a spec describes some other credential, so it says nothing about the token the gateway holds."""
        spec_security = SpecSecurityScopes({'basic': SCHEMES['basic']}, {})
        assert spec_security.requirement_for(operation(security=[{'basic': []}])).is_met(frozenset())

    def test_a_scope_holds_what_it_implies_transitively(self):
        spec_security = SpecSecurityScopes(SCHEMES, {'repo': ['public_repo'], 'public_repo': ['repo:read']})
        assert spec_security.held_scopes(['repo']) == {'repo', 'public_repo', 'repo:read'}

    def test_the_requested_scopes_are_every_scope_the_operations_name(self):
        """Every alternative is requested, since the gateway cannot tell which one the user will be granted."""
        operations = [
            LIST_PETS,
            DELETE_PET,
            operation('getPet', security=[{'oauth': ['read:pets']}, {'oauth': ['admin']}]),
            operation('health', security=[{}]),
        ]
        assert SpecSecurityScopes(SCHEMES, {}).requested_scopes(operations) == ['read:pets', 'write:pets', 'admin']

    def test_the_refusal_names_the_scopes_and_both_remedies(self):
        requirement = SpecSecurityScopes(SCHEMES, {}).requirement_for(DELETE_PET)
        assert requirement.refusal == DELETE_PET_UPSTREAM_REFUSAL


class TestUnrequestedScopeWarning:
    """Startup warns about the clients that can reach operations the requested upstream scopes cannot open."""

    def test_each_client_is_warned_about_what_it_can_reach(self, caplog):
        """A reader never reaches ``deletePet``, so only the deleter hears about the scope it needs."""
        mcp_policy = ScopeCatalog(OperationScopesConfig(auto='tags'), [LIST_PETS, DELETE_PET], 'petstore')
        with caplog.at_level(logging.WARNING):
            SpecSecurityScopes(SCHEMES, {}).warn_on_unrequested_scopes(
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
            SpecSecurityScopes(SCHEMES, {}).warn_on_unrequested_scopes(
                operations, [], UnrestrictedPolicy(), {}, 'petstore'
            )
        assert '"petstore": every client can reach 2 operation(s)' in caplog.text
        assert '(listPets needs read:pets, login needs a non-OAuth credential)' in caplog.text


class TestUpstreamScopePolicy:
    """Only ``auth.upstream.enforce_scopes`` makes the upstream grant restrict anything."""

    def test_without_enforcement_the_grant_restricts_nothing(self):
        policy = build_upstream_scope_policy(False, SpecSecurityScopes(SCHEMES, {}), 'petstore')
        assert isinstance(policy, UnenforcedUpstreamScopes)

    def test_enforcing_a_spec_without_an_oauth_scheme_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            build_upstream_scope_policy(True, SpecSecurityScopes({'basic': SCHEMES['basic']}, {}), 'petstore')
        assert 'the spec declares no OAuth security scheme' in caplog.text


class TestOperationAccessWithUpstreamScopes:
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
        access = OperationAccess(
            ScopeCatalog(OperationScopesConfig(auto='tags'), [DELETE_PET], 'petstore'),
            SpecSecurityScopes(SCHEMES, {'admin': ['read:pets', 'write:pets']}),
        )
        access.register('delete_pet', DELETE_PET)
        refusal = access.refusal('delete_pet', TokenGrant(frozenset(mcp_scopes), frozenset(upstream_scopes)))
        assert refusal == refused_for

    def test_the_lists_are_cached_per_token_once_the_grant_is_enforced(self):
        access = OperationAccess(UnrestrictedPolicy(), SpecSecurityScopes(SCHEMES, {}))
        assert access.list_cache_scope == 'private'


class TestGrantedScopes:
    """The upstream token response's ``scope`` is read as RFC 6749 states it."""

    @pytest.mark.parametrize(
        ('scope', 'granted'),
        [
            pytest.param('read:pets write:pets', ['read:pets', 'write:pets'], id='space-delimited'),
            pytest.param(None, ['previous'], id='omitted means unchanged'),
            pytest.param('', [], id='empty grants nothing'),
            pytest.param('read:user,repo', ['read:user,repo'], id='a comma belongs to the scope'),
        ],
    )
    def test_the_granted_scope_is_parsed_per_rfc_6749(self, scope, granted):
        response = UpstreamTokenResponse(access_token='token', refresh_token=None, expires_in=3600, scope=scope)
        assert response.granted_scopes(['previous']) == granted
