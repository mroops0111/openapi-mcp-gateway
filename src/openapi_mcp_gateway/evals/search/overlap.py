"""How many of an answer's own words a query repeats, which is what tells a literal query from a paraphrase.

A style label written by hand can be wrong in the direction that flatters a lexical ranker,
a paraphrase that still says the operation's words, so the label is checked against this number.
Words are compared after the same tokenizing and stemming search applies, since that is what a match means.
"""

import typing

from ...exposure._bm25 import tokenize


# A paraphrase or indirect query may repeat at most this share of its answer's terms.
# Past it, the query is closer to literal than its label says.
MAX_NON_LITERAL_OVERLAP = 0.5


def overlap(query: str, answer_text: str) -> float:
    """The share of ``answer_text``'s distinct terms that ``query`` also contains, from ``0`` to ``1``."""
    answer_terms = set(tokenize(answer_text))
    if not answer_terms:
        return 0.0
    return len(answer_terms.intersection(tokenize(query))) / len(answer_terms)


def answer_text(name: str, summary: str) -> str:
    """The words an answer is known by: its name, and its summary when the spec has one."""
    return f'{name} {summary}'


def best_overlap(query: str, answers: typing.Iterable[str], summaries: typing.Mapping[str, str]) -> float:
    """The highest overlap of ``query`` with any of its ``answers``, since repeating one answer's words is enough."""
    return max((overlap(query, answer_text(name, summaries.get(name, ''))) for name in answers), default=0.0)
