import collections
import math
import re
import typing


# BM25 term-frequency saturation and document-length normalisation, at the values most implementations use.
_BM25_K1 = 1.2
_BM25_B = 0.75

# A word is a run of capitals not followed by lower case (an acronym), a capitalised or lower-case run, or digits.
# This splits camelCase, snake_case, and URL paths alike, so `pullsMerge`, `pulls_merge` and `/pulls/merge` agree.
_WORD_PATTERN = re.compile(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+')

# Words too common in queries and operation text to tell operations apart.
_STOPWORDS = frozenset(
    {'a', 'all', 'an', 'and', 'are', 'as', 'by', 'for', 'from', 'in', 'is', 'it', 'of', 'on', 'or', 'the', 'to', 'with'}
)

# Endings that look plural but are not, so stemming leaves them alone.
_NON_PLURAL_ENDINGS = ('ss', 'us', 'is')


class OperationSearchIndex:
    """Rank operations against a free-text query with BM25.

    Each document is one operation's searchable text, keyed by operation name.
    Ties keep the order the documents were given in, so results are stable across calls.
    """

    def __init__(self, documents: typing.Mapping[str, str]):
        self._names = list(documents)
        self._term_counts = [collections.Counter(tokenize(text)) for text in documents.values()]
        self._lengths = [sum(counts.values()) for counts in self._term_counts]
        self._average_length = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        document_frequency: collections.Counter[str] = collections.Counter()
        for counts in self._term_counts:
            document_frequency.update(counts.keys())
        total = len(self._names)
        self._idf = {
            term: math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }

    def search(self, query: str, candidates: typing.Container[str] | None = None) -> list[str]:
        """Return the names of operations matching ``query``, best first.

        An operation that shares no term with the query is left out.
        ``candidates``, when given, limits the result to those names.
        """
        terms = [term for term in dict.fromkeys(tokenize(query)) if term in self._idf]
        if not terms:
            return []
        scored: list[tuple[float, int]] = []
        for position, (name, counts) in enumerate(zip(self._names, self._term_counts, strict=True)):
            if candidates is not None and name not in candidates:
                continue
            score = self._score(terms, counts, self._lengths[position])
            if score > 0:
                scored.append((-score, position))
        scored.sort()
        return [self._names[position] for _, position in scored]

    def _score(self, terms: list[str], counts: collections.Counter[str], length: int) -> float:
        length_norm = 1 - _BM25_B + _BM25_B * length / self._average_length
        score = 0.0
        for term in terms:
            frequency = counts.get(term, 0)
            if frequency:
                score += self._idf[term] * frequency * (_BM25_K1 + 1) / (frequency + _BM25_K1 * length_norm)
        return score


def tokenize(text: str) -> list[str]:
    """Split ``text`` into lower-case, singular search terms, without stopwords."""
    terms = []
    for word in _WORD_PATTERN.findall(text):
        term = _singular(word.lower())
        if term not in _STOPWORDS:
            terms.append(term)
    return terms


def _singular(word: str) -> str:
    """Strip a plural ``s``, so `pull` matches `pulls`; short words and endings such as `status` stay whole."""
    if len(word) > 3 and word.endswith('s') and not word.endswith(_NON_PLURAL_ENDINGS):
        return word[:-1]
    return word
