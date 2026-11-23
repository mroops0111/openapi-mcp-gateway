import collections
import statistics
import typing

import pydantic

from openapi_mcp_gateway.exposure._bm25 import tokenize

from ..gateway_connection import list_operations
from .dataset import SPLITS, STYLES, Benchmark, Query, Style
from .metrics import answers_in
from .runner import corpus_server


# A paraphrase or indirect query may repeat at most this share of its answer's terms.
# Past it, the query is closer to literal than its label says.
MAX_NON_LITERAL_OVERLAP = 0.5


class FlaggedQuery(pydantic.BaseModel):
    """A query whose overlap with its answer contradicts its style label."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    query: Query
    overlap: float


class StyleReport(pydantic.BaseModel):
    """Every query's overlap with its answer, and the queries whose label it contradicts."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    # Style, then the overlap of each query labelled with it.
    overlaps_by_style: dict[Style, list[float]]
    flagged_queries: list[FlaggedQuery]

    def mean_overlaps(self) -> dict[Style, float]:
        return {style: statistics.fmean(overlaps) for style, overlaps in self.overlaps_by_style.items() if overlaps}


def term_overlap(query: str, answer_text: str) -> float:
    """The share of ``answer_text``'s distinct terms that ``query`` also contains, from ``0`` to ``1``.

    Terms are compared after the tokenizing and stemming search applies, since that is what a match means.
    """
    answer_terms = set(tokenize(answer_text))
    if not answer_terms:
        return 0.0
    return len(answer_terms.intersection(tokenize(query))) / len(answer_terms)


def best_term_overlap(query: str, answers: typing.Iterable[str], summaries: typing.Mapping[str, str]) -> float:
    """The highest overlap of ``query`` with any of its ``answers``, since repeating one answer's words is enough.

    An answer is known by its name, and by its summary when the spec has one.
    """
    return max((term_overlap(query, f'{name} {summaries.get(name, "")}') for name in answers), default=0.0)


def contradicts_style(style: Style, overlap: float) -> bool:
    """Whether a query of ``style`` repeats more of its answer than the style allows, which only a literal one may."""
    return style != 'literal' and overlap > MAX_NON_LITERAL_OVERLAP


def check_styles(benchmark: Benchmark) -> StyleReport:
    """Measure every query's overlap with its answers, flagging a non-literal one above the limit.

    A style label written by hand can be wrong in the direction that flatters a lexical ranker,
    a paraphrase that still says the operation's words, so the label is checked against the overlap.
    """
    summaries_by_spec = {
        spec: {
            listing.name: listing.summary
            for listing in list_operations(corpus_server(benchmark.manifest, benchmark.root, spec))
        }
        for spec in benchmark.specs
    }
    overlaps_by_style: dict[Style, list[float]] = collections.defaultdict(list)
    flagged_queries: list[FlaggedQuery] = []
    for spec, spec_dataset in benchmark.specs.items():
        for split in SPLITS:
            for judged_query in spec_dataset.judged_queries(split):
                query = judged_query.query
                overlap = best_term_overlap(query.text, answers_in(judged_query.judgements), summaries_by_spec[spec])
                overlaps_by_style[query.style].append(overlap)
                if contradicts_style(query.style, overlap):
                    flagged_queries.append(FlaggedQuery(query=query, overlap=overlap))
    return StyleReport(
        overlaps_by_style={style: overlaps_by_style[style] for style in STYLES}, flagged_queries=flagged_queries
    )
