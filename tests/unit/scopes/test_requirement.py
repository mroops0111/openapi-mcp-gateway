import collections.abc

import pytest

from openapi_mcp_gateway.scopes import (
    AnyScopeRequirement,
    ScopeRequirement,
    UnreachableRequirement,
    UnrestrictedRequirement,
)


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
        assert AnyScopeRequirement(['issues:write', 'issues:triage']).is_met(granted) is met

    def test_any_scope_challenges_with_every_alternative(self):
        """The challenge names every scope that would satisfy the call, in a stable order, once each."""
        requirement = AnyScopeRequirement(['issues:write', 'issues:triage', 'issues:write'])
        assert requirement.challenge == ('issues:triage', 'issues:write')
        assert 'issues:triage, issues:write' in requirement.refusal

    def test_any_scope_needs_a_scope(self):
        """A requirement nothing could meet is ``UnreachableRequirement``, never an empty ``AnyScopeRequirement``."""
        with pytest.raises(ValueError, match='at least one'):
            AnyScopeRequirement([])

    @pytest.mark.parametrize(
        ('requirement', 'met'), [(UnrestrictedRequirement(), True), (UnreachableRequirement(), False)]
    )
    def test_unscoped_requirements_ignore_the_token(self, requirement, met):
        """Neither looks at the scopes, and neither names a scope to ask for."""
        assert requirement.is_met({'anything'}) is met
        assert requirement.is_met(set()) is met
        assert requirement.challenge == ()

    @pytest.mark.parametrize(
        ('grantable', 'challenged'),
        [({'issues:triage', 'other'}, ('issues:triage',)), ({'other'}, ()), (None, ('issues:triage', 'issues:write'))],
    )
    def test_any_scope_challenges_with_each_grantable_alternative(self, grantable, challenged):
        """Any one alternative is enough, so each the client may be granted stands on its own."""
        requirement = AnyScopeRequirement(['issues:write', 'issues:triage'])
        assert requirement.challenge_within(grantable) == challenged

    @pytest.mark.parametrize(
        ('grantable', 'challenged'),
        [
            ({'products:read', 'products:write'}, ('products:read', 'products:write')),
            ({'products:read'}, ()),
            (None, ('products:read', 'products:write')),
        ],
    )
    def test_a_challenge_stands_only_when_every_scope_is_grantable_by_default(self, grantable, challenged):
        """A requirement that does not say otherwise needs its scopes together, so a part would not help."""
        assert _EveryScopeRequirement().challenge_within(grantable) == challenged


class _EveryScopeRequirement(ScopeRequirement):
    """A requirement for ``products:read`` and ``products:write`` together, whose challenge the base class narrows."""

    def is_met(self, granted: collections.abc.Set[str]) -> bool:
        return {'products:read', 'products:write'} <= granted

    @property
    def challenge(self) -> tuple[str, ...]:
        return ('products:read', 'products:write')

    @property
    def refusal(self) -> str:
        return 'This operation needs both products:read and products:write.'
