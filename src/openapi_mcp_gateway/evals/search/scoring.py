import typing

from .metrics import NDCG, Metric, Recall, ReciprocalRank


# What every run reports. nDCG@10 is the headline, as in BEIR.
# Recall@20 is whether the answer is on the first page search_operations returns, which holds 20.
METRICS: tuple[Metric, ...] = (NDCG(10), Recall(20), ReciprocalRank(10), Recall(1))
METRIC_NAMES = tuple(metric.name for metric in METRICS)

# How deep a ranking must go for every metric above.
DEPTH = max(metric.cutoff for metric in METRICS)


def score_all(ranking: typing.Sequence[str], judgements: typing.Mapping[str, int]) -> dict[str, float]:
    """Score ``ranking`` on every metric in ``METRICS``, keyed by metric name."""
    return {metric.name: metric.score(ranking, judgements) for metric in METRICS}
