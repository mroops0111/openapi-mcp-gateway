import typing

import pytest

from openapi_mcp_gateway.query_string import (
    UNDECLARED_QUERY_SERIALIZER,
    QuerySerializer,
    encode_query,
    query_serializer,
)


def _query(serializer: QuerySerializer | None, value: typing.Any, name: str = 'opt_fields') -> str:
    """The query string ``serializer`` sends ``value`` as, under the parameter ``name``."""
    assert serializer is not None
    return encode_query(serializer.pairs(name, value))


class TestFormExploded:
    """``form`` with ``explode: true``, the query default."""

    serializer = query_serializer('form', True)

    @pytest.mark.parametrize(
        ('value', 'expected'),
        [
            ('name', 'opt_fields=name'),
            (3, 'opt_fields=3'),
            (True, 'opt_fields=true'),
            (['name', 'due_on'], 'opt_fields=name&opt_fields=due_on'),
            ([True, None, 2], 'opt_fields=true&opt_fields=2'),
            ({'color': 'red', 'done': False, 'size': None}, 'color=red&done=false'),
            ([], ''),
            ({}, ''),
        ],
    )
    def test_value(self, value, expected):
        """An array repeats the name per item, an object spreads into its own keys, and ``None`` sends nothing."""
        assert _query(self.serializer, value) == expected


class TestFormJoined:
    """``form`` with ``explode: false``."""

    serializer = query_serializer('form', False)

    @pytest.mark.parametrize(
        ('value', 'expected'),
        [
            ('name', 'opt_fields=name'),
            (['name', 'assignee.name', None], 'opt_fields=name,assignee.name'),
            ({'color': 'red', 'done': True}, 'opt_fields=color,red,done,true'),
            ([], ''),
            ([None], ''),
            ({}, ''),
        ],
    )
    def test_value(self, value, expected):
        """An array or object is one value of comma-separated items, and an empty one sends nothing."""
        assert _query(self.serializer, value) == expected

    def test_comma_inside_an_item_stays_apart_from_the_separators(self):
        """An item's own comma is encoded, so only the commas between items are literal."""
        assert _query(self.serializer, ['New York, NY', 'Paris']) == 'opt_fields=New%20York%2C%20NY,Paris'

    def test_nested_value_is_one_json_item(self):
        """A value nested inside an array, which ``form`` leaves undefined, is one item of compact JSON."""
        assert _query(self.serializer, [{'x': 1, 'y': 2}]) == 'opt_fields=%7B%22x%22%3A1%2C%22y%22%3A2%7D'


class TestUndeclared:
    """A key a ``request`` expression adds that no query parameter declares."""

    @pytest.mark.parametrize(
        ('value', 'expected'),
        [
            (['a', 'b'], 'created=a&created=b'),
            ({'gt': 5}, 'created=%7B%22gt%22%3A5%7D'),
            ({}, ''),
        ],
    )
    def test_value(self, value, expected):
        """An array repeats the key, and an object keeps its key as one JSON value."""
        assert _query(UNDECLARED_QUERY_SERIALIZER, value, name='created') == expected


@pytest.mark.parametrize(('style', 'explode'), [('deepObject', True), ('pipeDelimited', False), ('simple', False)])
def test_unsupported_style_has_no_serializer(style, explode):
    """A style the gateway cannot send has no serializer, so the parser leaves its parameters out."""
    assert query_serializer(style, explode) is None


def test_name_is_encoded():
    """A name with brackets, as Klaviyo's sparse fieldsets have, is percent-encoded."""
    assert _query(query_serializer('form', False), ['a', 'b'], name='fields[account]') == 'fields%5Baccount%5D=a,b'
