import logging

import pytest

from openapi_mcp_gateway.scopes import (
    TagMethodDerivation,
)
from tests.operation import operation


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

    def test_a_tag_that_makes_no_scope_name_derives_nothing(self, caplog):
        """Such a scope could never be granted, so the tag is skipped and warned about once."""
        derivation = TagMethodDerivation()
        with caplog.at_level(logging.WARNING):
            derived = [
                *derivation.derive(operation(tags=('Usuários', 'pets'))),
                *derivation.derive(operation(tags=('Usuários',))),
            ]
        assert [scope.name for scope in derived] == ['pets:read']
        assert caplog.text.count("Tag 'Usuários'") == 1

    @pytest.mark.parametrize('unscopable', [operation(tags=()), operation(method='options')])
    def test_untagged_or_unknown_methods_derive_nothing(self, unscopable):
        """Such an operation falls to ``unscoped`` instead."""
        assert list(TagMethodDerivation().derive(unscopable)) == []
