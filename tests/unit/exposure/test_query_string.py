import typing

import pytest

from openapi_mcp_gateway.exposure import (
    FormQuerySerializer,
    QuerySerializer,
    UndeclaredQuerySerializer,
    encode_query_string,
)


class TestFormExploded:
    """``form`` with ``explode: true``, the query default."""

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
        assert _query_string(FormQuerySerializer(), value, explode=True) == expected


class TestFormJoined:
    """``form`` with ``explode: false``."""

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
        assert _query_string(FormQuerySerializer(), value, explode=False) == expected

    def test_comma_inside_an_item_stays_apart_from_the_separators(self):
        """An item's own comma is encoded, so only the commas between items are literal."""
        query_string = _query_string(FormQuerySerializer(), ['New York, NY', 'Paris'], explode=False)
        assert query_string == 'opt_fields=New%20York%2C%20NY,Paris'

    def test_nested_value_is_one_json_item(self):
        """A value nested inside an array, which ``form`` leaves undefined, is one item of compact JSON."""
        query_string = _query_string(FormQuerySerializer(), [{'x': 1, 'y': 2}], explode=False)
        assert query_string == 'opt_fields=%7B%22x%22%3A1%2C%22y%22%3A2%7D'

    def test_name_is_encoded(self):
        """A name with brackets, as Klaviyo's sparse fieldsets have, is percent-encoded."""
        query_string = _query_string(FormQuerySerializer(), ['a', 'b'], explode=False, name='fields[account]')
        assert query_string == 'fields%5Baccount%5D=a,b'


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
        assert _query_string(UndeclaredQuerySerializer(), value, explode=True, name='created') == expected


def _query_string(serializer: QuerySerializer, value: typing.Any, *, explode: bool, name: str = 'opt_fields') -> str:
    """The query string ``serializer`` sends ``value`` as, under the parameter ``name``."""
    return encode_query_string(serializer.pairs(name, value, explode=explode))
