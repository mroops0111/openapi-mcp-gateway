import abc
import dataclasses
import json
import typing
import urllib.parse


# The serializers the gateway supports, by style and explode, each added by its subclass as it is defined.
# A style missing here is one the gateway cannot send, so the parser leaves its parameters out.
_QUERY_SERIALIZERS: dict[tuple[str, bool], 'QuerySerializer'] = {}


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
    """How one query parameter's value becomes the pairs of a query string (OpenAPI 3.1.1 §4.8.12.4).

    A subclass that declares ``style`` and ``explode`` in its own body serves parameters of that shape,
    and one that declares neither, such as :class:`UndeclaredQuerySerializer`, serves none.
    An empty array or object sends nothing, and neither does a ``None`` item, as RFC 6570 leaves them undefined.
    """

    style: typing.ClassVar[str]
    explode: typing.ClassVar[bool]

    def __init_subclass__(cls, **kwargs: typing.Any) -> None:
        super().__init_subclass__(**kwargs)
        # Read from the subclass's own body, so a subclass of a supported serializer does not replace it.
        if 'style' in cls.__dict__:
            _QUERY_SERIALIZERS[(cls.style, cls.explode)] = cls()

    @abc.abstractmethod
    def pairs(self, name: str, value: typing.Any) -> list[QueryPair]:
        """Return the pairs ``value`` is sent as under the parameter ``name``."""


class FormExplodedQuerySerializer(QuerySerializer):
    """The ``form`` style with ``explode: true``.

    An array repeats ``name`` for each item, and an object spreads into its own property names.
    """

    style = 'form'
    explode = True

    def pairs(self, name: str, value: typing.Any) -> list[QueryPair]:
        if isinstance(value, list):
            return [QueryPair(name, _encoded_text(item)) for item in value if item is not None]
        if isinstance(value, dict):
            return [QueryPair(key, _encoded_text(item)) for key, item in value.items() if item is not None]
        return [QueryPair(name, _encoded_text(value))]


class FormJoinedQuerySerializer(QuerySerializer):
    """The ``form`` style with ``explode: false``.

    An array becomes one value of comma-separated items, and an object one value of alternating names and values.
    Each item is encoded before the join, so a comma inside an item stays apart from the commas between them.
    """

    style = 'form'
    explode = False

    def pairs(self, name: str, value: typing.Any) -> list[QueryPair]:
        if isinstance(value, list):
            items = [item for item in value if item is not None]
        elif isinstance(value, dict):
            items = [text for key, item in value.items() if item is not None for text in (key, item)]
        else:
            return [QueryPair(name, _encoded_text(value))]
        return [QueryPair(name, ','.join(_encoded_text(item) for item in items))] if items else []


class UndeclaredQuerySerializer(FormExplodedQuerySerializer):
    """A key a ``request`` expression adds that no query parameter of the spec declares.

    An array repeats the key as ``form`` does.
    An object is sent whole as JSON under the key, since nothing declares how to spread it,
    and spreading it would lose the key.
    """

    def pairs(self, name: str, value: typing.Any) -> list[QueryPair]:
        if isinstance(value, dict):
            return [QueryPair(name, _encoded_text(value))] if value else []
        return super().pairs(name, value)


def query_serializer(style: str, explode: bool) -> QuerySerializer | None:
    """Return the serializer for a query parameter's ``style`` and ``explode``, or ``None`` when none is supported."""
    return _QUERY_SERIALIZERS.get((style, explode))


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
