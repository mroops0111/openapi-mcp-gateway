import logging

import pytest

from openapi_mcp_gateway.exposure import DynamicOperationTarget
from openapi_mcp_gateway.openapi import OperationInfo
from openapi_mcp_gateway.scopes import (
    AnyScope,
    OperationAccess,
    ScopeCatalog,
    TagMethodDerivation,
    ToolNameTarget,
    Unreachable,
    Unrestricted,
    UnrestrictedPolicy,
    build_mcp_scope_policy,
)
from openapi_mcp_gateway.settings import ScopesConfig
from tests.operations import PETSTORE_OPERATIONS, operation


class TestScopeRequirements:
    """Each requirement decides a token by its scopes and says what would satisfy it."""

    @pytest.mark.parametrize(
        ('granted', 'met'),
        [
            ({'issues:write'}, True),
            ({'issues:triage', 'other'}, True),
            ({'issues:read'}, False),
            (set(), False),
        ],
    )
    def test_any_scope_is_met_by_any_one(self, granted, met):
        """Holding any one of the covering scopes is enough, since covering scopes add up."""
        assert AnyScope(['issues:write', 'issues:triage']).is_met(granted) is met

    def test_any_scope_challenges_with_every_alternative(self):
        """The challenge names every scope that would satisfy the call, in a stable order, once each."""
        requirement = AnyScope(['issues:write', 'issues:triage', 'issues:write'])
        assert requirement.challenge == ('issues:triage', 'issues:write')
        assert 'issues:triage, issues:write' in requirement.refusal

    def test_any_scope_needs_a_scope(self):
        """A requirement nothing could meet is ``Unreachable``, never an empty ``AnyScope``."""
        with pytest.raises(ValueError, match='at least one'):
            AnyScope([])

    @pytest.mark.parametrize(('requirement', 'met'), [(Unrestricted(), True), (Unreachable(), False)])
    def test_unscoped_requirements_ignore_the_token(self, requirement, met):
        """Neither looks at the scopes, and neither names a scope to ask for."""
        assert requirement.is_met({'anything'}) is met
        assert requirement.is_met(set()) is met
        assert requirement.challenge == ()


class TestTagMethodDerivation:
    """``auto: tags`` derives one scope per tag, named by what the method does."""

    @pytest.mark.parametrize(
        ('method', 'expected'),
        [
            ('get', 'pets:read'),
            ('HEAD', 'pets:read'),
            ('post', 'pets:write'),
            ('put', 'pets:write'),
            ('patch', 'pets:write'),
            ('delete', 'pets:delete'),
        ],
    )
    def test_method_names_the_access(self, method, expected):
        """Reads, writes and deletes become separate scopes, so a delete can be withheld from a writer."""
        [derived] = TagMethodDerivation().derive(operation(method=method))
        assert derived.name == expected

    def test_each_tag_derives_its_own_scope(self):
        """An operation under two tags opens to either tag's scope."""
        derived = TagMethodDerivation().derive(operation(tags=('pets', 'Store Admin')))
        assert [scope.name for scope in derived] == ['pets:read', 'store-admin:read']
        assert [scope.description for scope in derived] == ['Read pets', 'Read Store Admin']

    @pytest.mark.parametrize('unscopable', [operation(tags=()), operation(method='options')])
    def test_untagged_or_unknown_methods_derive_nothing(self, unscopable):
        """Such an operation falls to ``unscoped`` instead."""
        assert list(TagMethodDerivation().derive(unscopable)) == []


class TestScopeCatalog:
    """``auth.scopes`` resolved against a server's operations."""

    def test_derived_and_manual_scopes_add_up(self):
        """An operation a manual scope also covers opens to either scope."""
        catalog = _catalog(auto='tags', manual={'admin': {'description': 'Admin', 'operations': ['DELETE *']}})
        requirement = catalog.requirement_for(_operation_named('deletePet'))
        assert requirement.challenge == ('admin', 'pets:delete')

    @pytest.mark.parametrize(('unscoped', 'expected'), [('deny', Unreachable), ('allow', Unrestricted)])
    def test_unscoped_decides_an_operation_no_scope_covers(self, unscoped, expected):
        """An untagged operation under ``auto: tags`` is unscoped, like any other no scope covers."""
        catalog = _catalog(auto='tags', unscoped=unscoped)
        assert isinstance(catalog.requirement_for(_operation_named('adminListPets')), expected)

    def test_defined_mcp_scopes_carry_descriptions(self):
        """Every scope is listed with what a person approving it is shown."""
        catalog = _catalog(auto='tags', manual={'admin': {'description': 'Manage pets', 'operations': ['admin*']}})
        assert catalog.defined_mcp_scopes == {
            'pets:read': 'Read pets',
            'pets:write': 'Create and change pets',
            'pets:delete': 'Delete pets',
            'admin': 'Manage pets',
        }

    def test_a_manual_scope_cannot_reuse_a_derived_name(self):
        """The two definitions would otherwise merge into one scope without anyone noticing."""
        with pytest.raises(ValueError, match=r'reuses names .* \(pets:read\)'):
            _catalog(auto='tags', manual={'pets:read': {'description': 'Read', 'operations': ['GET *']}})

    def test_a_pattern_matching_nothing_is_warned(self, caplog):
        """A typo in a pattern only ever shortens what a scope opens, so it is reported at startup."""
        with caplog.at_level(logging.WARNING):
            _catalog(manual={'admin': {'description': 'Admin', 'operations': ['DELETE /pest/*']}})
        assert 'DELETE /pest/*' in caplog.text

    @pytest.mark.parametrize(('unscoped', 'level'), [('deny', logging.WARNING), ('allow', logging.INFO)])
    def test_unscoped_operations_are_counted_at_startup(self, caplog, unscoped, level):
        """Under ``deny`` they vanish from every client, so the count is a warning there."""
        with caplog.at_level(logging.INFO):
            _catalog(auto='tags', unscoped=unscoped)
        [record] = [record for record in caplog.records if 'no scope covers' in record.message]
        assert record.levelno == level
        assert '2 operation(s)' in record.message
        assert 'createPetWithOwner, adminListPets' in record.message

    def test_lists_vary_by_token(self):
        """``tools/list`` is cached per token once scopes filter it."""
        assert _catalog(auto='tags').list_cache_scope == 'private'


class TestUnrestrictedPolicy:
    """A server without ``auth.scopes`` restricts nothing."""

    def test_every_operation_is_open(self):
        policy = build_mcp_scope_policy(None, PETSTORE_OPERATIONS, 'petstore')
        assert isinstance(policy, UnrestrictedPolicy)
        assert all(isinstance(policy.requirement_for(candidate), Unrestricted) for candidate in PETSTORE_OPERATIONS)
        assert policy.defined_mcp_scopes == {}
        assert policy.list_cache_scope == 'public'


class TestOperationAccess:
    """Requirements looked up by the name a tool or operation is exposed as."""

    def test_a_registered_name_carries_its_requirement(self):
        access = OperationAccess(_catalog(auto='tags'))
        access.register('delete_pet', _operation_named('deletePet'))
        assert access.permits('delete_pet', frozenset({'pets:delete'}))
        assert not access.permits('delete_pet', frozenset({'pets:write'}))

    def test_an_unregistered_name_requires_nothing(self):
        """A dynamic meta-tool is never registered, since the operations behind it are what scopes guard."""
        access = OperationAccess(_catalog(auto='tags'))
        assert access.permits('search_operations', frozenset())


class TestCallTargets:
    """Which name a 2026-07-28 ``tools/call`` reaches, read from its headers alone."""

    def test_static_exposure_reads_the_tool_name(self):
        assert ToolNameTarget().resolve({'mcp-name': 'delete_pet'}) == 'delete_pet'

    @pytest.mark.parametrize(
        ('headers', 'expected'),
        [
            ({'mcp-name': 'call_operation', 'mcp-param-operation': 'delete_pet'}, 'delete_pet'),
            ({'mcp-name': 'search_operations'}, None),
            ({'mcp-name': 'call_operation'}, None),
        ],
    )
    def test_dynamic_exposure_reads_the_operation_call_operation_names(self, headers, expected):
        """Only ``call_operation`` reaches a single operation, named in its ``Mcp-Param-Operation`` header."""
        assert DynamicOperationTarget().resolve(headers) == expected


def _catalog(**config: object) -> ScopeCatalog:
    """A catalog over the petstore operations, configured as ``auth.scopes`` would be."""
    return ScopeCatalog(ScopesConfig.model_validate(config), PETSTORE_OPERATIONS, 'petstore')


def _operation_named(operation_id: str) -> OperationInfo:
    """The petstore operation with ``operation_id``."""
    return next(candidate for candidate in PETSTORE_OPERATIONS if candidate.operation_id == operation_id)
