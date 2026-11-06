import base64
import binascii
import dataclasses
import hashlib
import json
import typing

from mcp.types import CallToolResult

from ._search import OperationSearchIndex


# How many operations one search page carries.
_PAGE_SIZE = 20

# The longest description a search result shows, in characters.
# The full text stays one get_operation call away.
_BRIEF_DESCRIPTION_LIMIT = 200

# How much a matching term counts in each searchable field.
# The summary says in plain words what the operation does, and the name says it in identifier form.
# A long description also mentions what the operation does not do, and the path and tags repeat its resource.
# Tried on everyday requests against GitHub's spec, these weights ranked the intended operation in the top three most often.
_FIELD_WEIGHTS = {'name': 2.0, 'summary': 3.0, 'description': 0.5, 'path': 0.5, 'tags': 0.5}


@dataclasses.dataclass(frozen=True)
class CatalogOperation:
    """One operation behind the dynamic meta-tools, carrying what search, description, and dispatch need."""

    name: str
    summary: str
    description: str
    path: str
    tags: list[str]
    input_schema: dict[str, typing.Any]
    callable_: typing.Callable[..., typing.Awaitable[CallToolResult]]

    def search_fields(self) -> dict[str, str]:
        """Return the text search ranks this operation by, keyed by field name."""
        return {
            'name': self.name,
            'summary': self.summary,
            'description': self.description,
            'path': self.path,
            'tags': ' '.join(self.tags),
        }


class OperationCatalog:
    """The operations of one dynamically exposed server, searchable by query and tag, a page at a time.

    Everything derived from the operations, such as the search index and tag counts, is built once here,
    since the operations never change after registration.
    Tags match without regard to case, and each is shown in the spelling it first appears in.
    """

    def __init__(self, operations: typing.Iterable[CatalogOperation]):
        self._operations = {operation.name: operation for operation in operations}
        self._index = OperationSearchIndex(
            {name: operation.search_fields() for name, operation in self._operations.items()}, _FIELD_WEIGHTS
        )
        # Keyed by casefolded tag. The inner dict is an ordered set of operation names.
        self._names_by_tag: dict[str, dict[str, None]] = {}
        self._tag_spellings: dict[str, str] = {}
        for name, operation in self._operations.items():
            for tag in operation.tags:
                key = tag.casefold()
                self._tag_spellings.setdefault(key, tag)
                self._names_by_tag.setdefault(key, {})[name] = None
        self._tag_counts = [
            {'name': self._tag_spellings[key], 'operations': len(self._names_by_tag[key])}
            for key in sorted(self._names_by_tag, key=lambda key: self._tag_spellings[key])
        ]

    def __len__(self) -> int:
        return len(self._operations)

    def get(self, name: str) -> CatalogOperation | None:
        return self._operations.get(name)

    def search(self, query: str | None, tags: list[str] | None, cursor: str | None) -> dict[str, typing.Any]:
        """Return one page of the operations matching ``query`` and carrying any of ``tags``.

        An omitted or blank ``query``, and omitted or empty ``tags``, filter nothing.
        Listing everything, the first page also carries the tag counts,
        so the model can narrow down rather than page through all of them.
        Raises ``ValueError`` for a cursor this catalog did not issue for the same query and tags.
        """
        query = (query or '').strip()
        tags = tags or []
        names = self._matching(query, tags)
        fingerprint = _fingerprint(query, tags)
        offset = _PageCursor.decode(cursor, fingerprint, len(names)).offset if cursor else 0
        page = names[offset : offset + _PAGE_SIZE]
        payload: dict[str, typing.Any] = {
            'operations': [{'name': name, 'description': _brief(self._operations[name].description)} for name in page]
        }
        if offset + _PAGE_SIZE < len(names):
            payload['nextCursor'] = _PageCursor(offset + _PAGE_SIZE, fingerprint).encode()
        if not query and not tags and offset == 0 and self._tag_counts:
            payload['tags'] = self._tag_counts
        return payload

    def _matching(self, query: str, tags: list[str]) -> list[str]:
        candidates = {name for tag in tags for name in self._names_by_tag.get(tag.casefold(), {})} if tags else None
        if query:
            return self._index.search(query, candidates)
        if candidates is None:
            return list(self._operations)
        return [name for name in self._operations if name in candidates]


@dataclasses.dataclass(frozen=True)
class _PageCursor:
    """Where the next page starts, bound to the query and tags whose result it pages through."""

    offset: int
    fingerprint: str

    def encode(self) -> str:
        payload = json.dumps({'offset': self.offset, 'fingerprint': self.fingerprint}, separators=(',', ':'))
        return base64.urlsafe_b64encode(payload.encode()).decode()

    @classmethod
    def decode(cls, cursor: str, fingerprint: str, total: int) -> '_PageCursor':
        """Read ``cursor`` back, rejecting one that is malformed, past the end, or issued for another search."""
        try:
            payload = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
            decoded = cls(offset=payload['offset'], fingerprint=payload['fingerprint'])
        except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, KeyError):
            decoded = None
        if decoded is not None and decoded.fingerprint != fingerprint:
            raise ValueError(
                f'Cursor {cursor!r} belongs to a different query or tags. '
                'Repeat the search that returned it, or search again without a cursor.'
            )
        if decoded is None or type(decoded.offset) is not int or not 0 < decoded.offset < total:
            raise ValueError(f'Invalid cursor: {cursor!r}. Pass the nextCursor of the previous page.')
        return decoded


def _fingerprint(query: str, tags: list[str]) -> str:
    """Identify a search by its query and its tags, ignoring tag order and case."""
    normalised = json.dumps([query, sorted({tag.casefold() for tag in tags})])
    return hashlib.sha256(normalised.encode()).hexdigest()[:16]


def _brief(description: str) -> str:
    """Cut ``description`` to its first paragraph on one line, at most ``_BRIEF_DESCRIPTION_LIMIT`` characters."""
    first_paragraph = ' '.join(description.strip().split('\n\n', 1)[0].split())
    if len(first_paragraph) <= _BRIEF_DESCRIPTION_LIMIT:
        return first_paragraph
    return first_paragraph[: _BRIEF_DESCRIPTION_LIMIT - 1].rstrip() + '…'
