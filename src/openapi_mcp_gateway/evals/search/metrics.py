"""Ranking metrics over graded relevance judgements, computed per query.

Grades follow TREC: ``2`` for the operation that does what was asked, ``1`` for one that would also get there,
and anything unjudged is ``0``.
nDCG uses every grade, with linear gain as ``trec_eval`` computes it.
MRR and recall count only grade ``2``, the answer itself, so a lesser operation ranked first is not a hit.
"""

import math
import typing


# The grade that marks the answer itself, as opposed to an acceptable lesser one.
ANSWER_GRADE = 2


class Metric(typing.NamedTuple):
    """A metric's reported name and its cutoff."""

    name: str
    cutoff: int


# What every run reports. nDCG@10 is the headline, as in BEIR.
# Recall@20 is whether the answer is on the first page search_operations returns, which holds 20.
NDCG_AT_10 = Metric('ndcg@10', 10)
RECALL_AT_20 = Metric('recall@20', 20)
MRR_AT_10 = Metric('mrr@10', 10)
RECALL_AT_1 = Metric('recall@1', 1)
METRICS = (NDCG_AT_10, RECALL_AT_20, MRR_AT_10, RECALL_AT_1)

# How deep a ranking must go for every metric above.
DEPTH = max(metric.cutoff for metric in METRICS)


def score(metric: Metric, ranking: typing.Sequence[str], judgements: typing.Mapping[str, int]) -> float:
    """Score one query's ``ranking`` against its ``judgements``, a grade per relevant operation name."""
    if metric.name.startswith('ndcg'):
        return ndcg(ranking, judgements, metric.cutoff)
    if metric.name.startswith('mrr'):
        return reciprocal_rank(ranking, judgements, metric.cutoff)
    if metric.name.startswith('recall'):
        return recall(ranking, judgements, metric.cutoff)
    raise ValueError(f'Unknown metric: {metric.name}')


def ndcg(ranking: typing.Sequence[str], judgements: typing.Mapping[str, int], cutoff: int) -> float:
    """Normalised discounted cumulative gain at ``cutoff``, or ``0`` when nothing is judged relevant."""
    ideal = _dcg(sorted(judgements.values(), reverse=True)[:cutoff])
    if ideal == 0:
        return 0.0
    return _dcg([judgements.get(name, 0) for name in ranking[:cutoff]]) / ideal


def reciprocal_rank(ranking: typing.Sequence[str], judgements: typing.Mapping[str, int], cutoff: int) -> float:
    """One over the rank of the first answer within ``cutoff``, or ``0`` when none is there."""
    for rank, name in enumerate(ranking[:cutoff], start=1):
        if judgements.get(name, 0) >= ANSWER_GRADE:
            return 1 / rank
    return 0.0


def recall(ranking: typing.Sequence[str], judgements: typing.Mapping[str, int], cutoff: int) -> float:
    """The share of answers ranked within ``cutoff``."""
    answers = {name for name, grade in judgements.items() if grade >= ANSWER_GRADE}
    if not answers:
        return 0.0
    return len(answers.intersection(ranking[:cutoff])) / len(answers)


def _dcg(grades: typing.Iterable[int]) -> float:
    return sum(grade / math.log2(rank + 1) for rank, grade in enumerate(grades, start=1))
