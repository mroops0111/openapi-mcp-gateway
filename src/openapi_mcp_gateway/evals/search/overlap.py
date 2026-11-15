import collections
import dataclasses
import statistics
import typing

from ...exposure._bm25 import tokenize
from .dataset import SPLITS, STYLES, Benchmark, Query
from .metrics import answers_of


# A paraphrase or indirect query may repeat at most this share of its answer's terms.
# Past it, the query is closer to literal than its label says.
MAX_NON_LITERAL_OVERLAP = 0.5


@dataclasses.dataclass(frozen=True)
class StyleReport:
    """Every query's overlap with its answer, and the queries whose label it contradicts."""

    # Style, then the overlap of each query labelled with it.
    overlaps: dict[str, list[float]]
    # Each mislabelled query with its overlap.
    flagged: list[tuple[Query, float]]

    def mean_overlaps(self) -> dict[str, float]:
        return {style: statistics.fmean(values) for style, values in self.overlaps.items() if values}


def overlap(query: str, answer_text: str) -> float:
    """The share of ``answer_text``'s distinct terms that ``query`` also contains, from ``0`` to ``1``.

    Terms are compared after the tokenizing and stemming search applies, since that is what a match means.
    """
    answer_terms = set(tokenize(answer_text))
    if not answer_terms:
        return 0.0
    return len(answer_terms.intersection(tokenize(query))) / len(answer_terms)


def best_overlap(query: str, answers: typing.Iterable[str], summaries: typing.Mapping[str, str]) -> float:
    """The highest overlap of ``query`` with any of its ``answers``, since repeating one answer's words is enough.

    An answer is known by its name, and by its summary when the spec has one.
    """
    return max((overlap(query, f'{name} {summaries.get(name, "")}') for name in answers), default=0.0)


def check_styles(benchmark: Benchmark, summaries_by_spec: typing.Mapping[str, typing.Mapping[str, str]]) -> StyleReport:
    """Measure every query's overlap with its answers, flagging a non-literal one above the limit.

    A style label written by hand can be wrong in the direction that flatters a lexical ranker,
    a paraphrase that still says the operation's words, so the label is checked against the overlap.
    """
    overlaps: dict[str, list[float]] = collections.defaultdict(list)
    flagged: list[tuple[Query, float]] = []
    for spec, spec_set in benchmark.specs.items():
        for split in SPLITS:
            for query, judgements in spec_set.split(split):
                share = best_overlap(query.text, answers_of(judgements), summaries_by_spec[spec])
                overlaps[query.style].append(share)
                if query.style != 'literal' and share > MAX_NON_LITERAL_OVERLAP:
                    flagged.append((query, share))
    return StyleReport(overlaps={style: overlaps[style] for style in STYLES}, flagged=flagged)
