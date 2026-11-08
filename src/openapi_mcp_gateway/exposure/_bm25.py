import collections
import functools
import math
import re
import typing

import snowballstemmer


# BM25 term-frequency saturation and document-length normalisation, at the values most implementations use.
_BM25_K1 = 1.2
_BM25_B = 0.75

# A chunk is a run of letters and digits in any script, so punctuation, `_`, `/`, and `{}` all separate chunks.
_CHUNK_PATTERN = re.compile(r'[^\W_]+')

# How many distinct words keep their stem cached, bounding memory against an endless variety of queries.
_STEM_CACHE_SIZE = 65536

# Words too common in queries and operation text to tell operations apart, compared before stemming.
_STOPWORDS = frozenset(
    {'a', 'all', 'an', 'and', 'are', 'as', 'by', 'for', 'from', 'in', 'is', 'it', 'of', 'on', 'or', 'the', 'to', 'with'}
)


class BM25FIndex:
    """Rank documents against a free-text query with BM25F.

    A document is a mapping of field name to text, keyed by document name.
    Each field's term frequency is normalised by that field's own average length and scaled by its weight,
    so a long description cannot outweigh a short name merely by being long.
    A field missing from ``field_weights`` is not searched.
    Ties keep the order the documents were given in, so results are stable across calls.
    """

    def __init__(
        self, documents: typing.Mapping[str, typing.Mapping[str, str]], field_weights: typing.Mapping[str, float]
    ):
        self._names = list(documents)
        self._field_weights = dict(field_weights)
        self._term_counts = {
            field: [collections.Counter(tokenize(fields.get(field, ''))) for fields in documents.values()]
            for field in self._field_weights
        }
        self._average_lengths = {
            field: (sum(sum(counts.values()) for counts in per_document) / len(per_document)) if per_document else 0.0
            for field, per_document in self._term_counts.items()
        }
        document_frequency: collections.Counter[str] = collections.Counter()
        for position in range(len(self._names)):
            document_frequency.update(
                {term for per_document in self._term_counts.values() for term in per_document[position]}
            )
        total = len(self._names)
        self._idf = {
            term: math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }

    def search(self, query: str, candidates: typing.Container[str] | None = None) -> list[str]:
        """Return the names of documents matching ``query``, best first.

        A document that shares no term with the query is left out.
        ``candidates``, when given, limits the result to those names.
        """
        terms = [term for term in dict.fromkeys(tokenize(query)) if term in self._idf]
        if not terms:
            return []
        scored: list[tuple[float, int]] = []
        for position, name in enumerate(self._names):
            if candidates is not None and name not in candidates:
                continue
            score = sum(self._score_term(term, position) for term in terms)
            if score > 0:
                scored.append((-score, position))
        scored.sort()
        return [self._names[position] for _, position in scored]

    def _score_term(self, term: str, position: int) -> float:
        weighted_frequency = 0.0
        for field, weight in self._field_weights.items():
            counts = self._term_counts[field][position]
            frequency = counts.get(term, 0)
            if frequency:
                length_ratio = sum(counts.values()) / self._average_lengths[field]
                weighted_frequency += weight * frequency / (1 - _BM25_B + _BM25_B * length_ratio)
        return self._idf[term] * weighted_frequency * (_BM25_K1 + 1) / (weighted_frequency + _BM25_K1)


def tokenize(text: str) -> list[str]:
    """Split ``text`` into stemmed, lower-case search terms, without stopwords.

    camelCase, snake_case, and URL paths split alike, so `pullsMerge`, `pulls_merge`, and `/pulls/merge` agree.
    """
    words = [word.lower() for chunk in _CHUNK_PATTERN.findall(text) for word in _split_case(chunk)]
    return [_stem(word) for word in words if word not in _STOPWORDS]


def _split_case(chunk: str) -> list[str]:
    """Split ``chunk`` where letter case or the letter-digit kind changes.

    An acronym ends before the capital that starts the next word, so `SSHKey` is `SSH` and `Key`.
    An acronym followed by a lone `s` keeps it as its plural, so `getUserIDs` ends in `IDs`.
    """
    words: list[str] = []
    start = 0
    for index in range(1, len(chunk)):
        previous, current = chunk[index - 1], chunk[index]
        following = chunk[index + 1 : index + 2]
        if (
            previous.isdigit() != current.isdigit()
            or (previous.islower() and current.isupper())
            or (
                previous.isupper()
                and current.isupper()
                and following.islower()
                and not _is_acronym_plural(chunk, index + 1)
            )
        ):
            words.append(chunk[start:index])
            start = index
    words.append(chunk[start:])
    return words


def _is_acronym_plural(chunk: str, index: int) -> bool:
    """True iff ``chunk[index]`` is an `s` pluralising the acronym before it.

    Such an `s` ends the chunk or precedes the capital that starts the next word.
    """
    return chunk[index] == 's' and (index + 1 == len(chunk) or chunk[index + 1].isupper())


@functools.lru_cache(maxsize=_STEM_CACHE_SIZE)
def _stem(word: str) -> str:
    """Stem ``word`` with the Snowball English stemmer, caching each result.

    An API's vocabulary is small and repeats across operations, so the cache serves most words.
    A stemmer keeps state while it works, so each call takes a fresh one, which costs far less than stemming.
    """
    return snowballstemmer.stemmer('english').stemWord(word)
