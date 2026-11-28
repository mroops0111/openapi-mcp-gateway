import abc
import dataclasses
import json
import typing
import urllib.parse


@dataclasses.dataclass(frozen=True)
class QueryPair:
    """One ``name=value`` of a query string, whose value a serializer has already percent-encoded.

    The serializer encodes the value, so a delimiter it adds stays apart from the same character inside the data.
    """

    name: str
    encoded_value: str

    def encode(self) -> str:
        """Return the pair as it appears in a query string."""
        return f'{_percent_encode(self.name)}={self.encoded_value}'


class QuerySerializer(abc.ABC):
    """Strategy for how one query parameter's value becomes the pairs of a query string (OpenAPI 3.1.1 §4.8.12.4).

    Each subclass is one ``style``, and ``explode`` picks between the two shapes every style has.
    An empty array or object sends nothing, and neither does a ``None`` item, as RFC 6570 leaves them undefined.
    """

    @abc.abstractmethod
    def pairs(self, name: str, value: typing.Any, *, explode: bool) -> list[QueryPair]:
        """Return the pairs ``value`` is sent as under the parameter ``name``."""


class FormQuerySerializer(QuerySerializer):
    """The ``form`` style, the query default.

    With ``explode``, an array repeats ``name`` for each item, and an object spreads into its own property names.
    Without it, an array becomes one value of comma-separated items, and an object one of alternating names and values.
    Each item is encoded before the join, so a comma inside an item stays apart from the commas between them.
    """

    def pairs(self, name: str, value: typing.Any, *, explode: bool) -> list[QueryPair]:
        if isinstance(value, list):
            items = [item for item in value if item is not None]
            fields = [(name, item) for item in items]
        elif isinstance(value, dict):
            fields = [(key, item) for key, item in value.items() if item is not None]
            items = [text for field in fields for text in field]
        else:
            return [QueryPair(name, _encoded_text(value))]
        if explode:
            return [QueryPair(field_name, _encoded_text(item)) for field_name, item in fields]
        return [QueryPair(name, ','.join(_encoded_text(item) for item in items))] if items else []


class UndeclaredQuerySerializer(FormQuerySerializer):
    """A key a ``request`` expression adds that no query parameter of the spec declares.

    An array repeats the key as ``form`` does with ``explode``.
    An object is sent whole as JSON under the key, since nothing declares how to spread it,
    and spreading it would lose the key.
    """

    def pairs(self, name: str, value: typing.Any, *, explode: bool) -> list[QueryPair]:
        if isinstance(value, dict):
            return [QueryPair(name, _encoded_text(value))] if value else []
        return super().pairs(name, value, explode=explode)


def encode_query_string(pairs: typing.Iterable[QueryPair]) -> str:
    """Join ``pairs`` into a query string, without the leading ``?``."""
    return '&'.join(pair.encode() for pair in pairs)


def _encoded_text(value: typing.Any) -> str:
    """Render one query item as percent-encoded text, with JSON's ``true`` and ``false`` for booleans.

    The ``form`` style leaves a value nested inside an array or object undefined, so it is sent as compact JSON.
    """
    if isinstance(value, bool):
        text = 'true' if value else 'false'
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, separators=(',', ':'))
    else:
        text = str(value)
    return _percent_encode(text)


def _percent_encode(text: str) -> str:
    """Percent-encode every character RFC 3986 does not leave unreserved."""
    return urllib.parse.quote(text, safe='')
