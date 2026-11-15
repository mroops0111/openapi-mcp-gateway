import collections
import importlib.metadata
import statistics
import typing

import pydantic

from ..significance import Difference, macro_mean, paired_bootstrap
from .dataset import STYLES, Benchmark, Split, Style
from .runner import QueryResult, Results
from .scoring import METRIC_NAMES


# The metrics a comparison between two systems is reported on.
COMPARED_METRICS = ('ndcg@10', 'recall@20')

# How many of a query's top operations a report keeps, enough to see what outranked the answer.
KEPT_RANKING_LENGTH = 10

# Report values are rounded to this many places, more than any table shows and few enough to diff.
DECIMAL_PLACES = 4

# A score as a report keeps it.
RoundedScore = typing.Annotated[float, pydantic.AfterValidator(lambda value: round(value, DECIMAL_PLACES))]
# Metric name to its score.
Scores = dict[str, RoundedScore]


class SpecSummary(pydantic.BaseModel):
    """One system's mean scores on one spec, overall and per style."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    query_count: int
    overall: Scores
    by_style: dict[Style, Scores]


class SystemSummary(pydantic.BaseModel):
    """One system's mean scores, macro-averaged over specs overall and per style, and for each spec."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    macro: Scores
    macro_by_style: dict[Style, Scores]
    specs: dict[str, SpecSummary]


class Comparison(pydantic.BaseModel):
    """How far ``system`` scores above ``against`` on one metric, with its 95% bootstrap interval."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    system: str
    against: str
    metric: str
    difference: RoundedScore
    interval_low: RoundedScore
    interval_high: RoundedScore
    significant: bool


class QueryRecord(pydantic.BaseModel):
    """One query's scores under one system, and the operations it ranked first."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    spec: str
    query_id: str
    style: Style
    scores: Scores
    top_operations: list[str]


class Report(pydantic.BaseModel):
    """Everything one run found, written as JSON by ``search run --output``."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    benchmark_version: int
    split: Split
    gateway_version: str
    # The gated system, when it ran, which every comparison is made from.
    reference_system: str | None
    systems: dict[str, SystemSummary]
    comparisons: list[Comparison]
    queries: dict[str, list[QueryRecord]]


def build_report(benchmark: Benchmark, split: Split, results: Results) -> Report:
    """Summarize ``results``, and compare the gated system with every other one that ran."""
    gated_system = benchmark.manifest.gate.system
    reference_system = gated_system if gated_system in results else None
    return Report(
        benchmark_version=benchmark.manifest.version,
        split=split,
        gateway_version=importlib.metadata.version('openapi-mcp-gateway'),
        reference_system=reference_system,
        systems={name: summarize(system_results) for name, system_results in results.items()},
        comparisons=_comparisons(results, reference_system) if reference_system else [],
        queries={
            name: [_query_record(result) for result in system_results] for name, system_results in results.items()
        },
    )


def summarize(results: typing.Sequence[QueryResult]) -> SystemSummary:
    """Mean scores per spec, per style within each spec, and macro-averaged over specs, overall and per style."""
    results_by_spec: dict[str, list[QueryResult]] = collections.defaultdict(list)
    for result in results:
        results_by_spec[result.spec].append(result)
    specs = {spec: _spec_summary(spec_results) for spec, spec_results in results_by_spec.items()}
    macro = {
        metric: macro_mean(
            {spec: [result.scores[metric] for result in spec_results] for spec, spec_results in results_by_spec.items()}
        )
        for metric in METRIC_NAMES
    }
    macro_by_style: dict[Style, dict[str, float]] = {
        style: {
            metric: statistics.fmean(
                summary.by_style[style][metric] for summary in specs.values() if style in summary.by_style
            )
            for metric in METRIC_NAMES
        }
        for style in STYLES
        if any(style in summary.by_style for summary in specs.values())
    }
    return SystemSummary(macro=macro, macro_by_style=macro_by_style, specs=specs)


def compare(first: typing.Sequence[QueryResult], second: typing.Sequence[QueryResult], metric: str) -> Difference:
    """How far ``first`` scores above ``second`` on ``metric``, over the queries both ran, stratified by spec."""
    second_scores = {(result.spec, result.query_id): result.scores[metric] for result in second}
    differences_by_spec: dict[str, list[float]] = collections.defaultdict(list)
    for result in first:
        key = (result.spec, result.query_id)
        if key in second_scores:
            differences_by_spec[result.spec].append(result.scores[metric] - second_scores[key])
    return paired_bootstrap(differences_by_spec)


def _comparisons(results: Results, reference_system: str) -> list[Comparison]:
    comparisons = []
    for name, system_results in results.items():
        if name == reference_system:
            continue
        for metric in COMPARED_METRICS:
            difference = compare(results[reference_system], system_results, metric)
            comparisons.append(
                Comparison(
                    system=reference_system,
                    against=name,
                    metric=metric,
                    difference=difference.mean,
                    interval_low=difference.low,
                    interval_high=difference.high,
                    significant=difference.significant,
                )
            )
    return comparisons


def _spec_summary(results: typing.Sequence[QueryResult]) -> SpecSummary:
    by_style: dict[Style, dict[str, float]] = {
        style: _mean_scores(styled_results)
        for style in STYLES
        if (styled_results := [result for result in results if result.style == style])
    }
    return SpecSummary(query_count=len(results), overall=_mean_scores(results), by_style=by_style)


def _query_record(result: QueryResult) -> QueryRecord:
    return QueryRecord(
        spec=result.spec,
        query_id=result.query_id,
        style=result.style,
        scores=result.scores,
        top_operations=result.ranking[:KEPT_RANKING_LENGTH],
    )


def _mean_scores(results: typing.Sequence[QueryResult]) -> dict[str, float]:
    return {metric: statistics.fmean(result.scores[metric] for result in results) for metric in METRIC_NAMES}
