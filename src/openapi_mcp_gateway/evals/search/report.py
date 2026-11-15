import collections
import importlib.metadata
import statistics
import typing

from ..stats import Difference, macro_mean, paired_bootstrap
from .dataset import STYLES, Benchmark, Split
from .runner import QueryResult, Results
from .scoring import METRIC_NAMES


# The metrics a comparison between two systems is reported on.
COMPARED_METRICS = ('ndcg@10', 'recall@20')

# How many of a query's top operations a report keeps, enough to see what outranked the answer.
KEPT_RANKING = 10

# Report values are rounded to this many places, more than any table shows and few enough to diff.
PRECISION = 4


def build_report(benchmark: Benchmark, split: Split, results: Results) -> dict[str, typing.Any]:
    """Summarize ``results`` as plain data, and compare the gated system with every other one that ran."""
    reference = benchmark.manifest.gate.system
    return {
        'benchmark': {'version': benchmark.manifest.version, 'split': split},
        'gateway_version': importlib.metadata.version('openapi-mcp-gateway'),
        'reference': reference if reference in results else None,
        'systems': {name: _rounded(summarize(system_results)) for name, system_results in results.items()},
        'comparisons': _comparisons(results, reference),
        'queries': {
            name: [_query_record(result) for result in system_results] for name, system_results in results.items()
        },
    }


def summarize(results: typing.Sequence[QueryResult]) -> dict[str, typing.Any]:
    """Mean scores per spec, per style within each spec, and macro-averaged over specs, overall and per style."""
    by_spec: dict[str, list[QueryResult]] = collections.defaultdict(list)
    for result in results:
        by_spec[result.spec].append(result)
    specs = {spec: _spec_summary(spec_results) for spec, spec_results in by_spec.items()}
    return {
        'macro': {
            metric: macro_mean(
                {spec: [r.scores[metric] for r in spec_results] for spec, spec_results in by_spec.items()}
            )
            for metric in METRIC_NAMES
        },
        'macro_styles': {
            style: {
                metric: statistics.fmean(s['styles'][style][metric] for s in specs.values() if style in s['styles'])
                for metric in METRIC_NAMES
            }
            for style in STYLES
            if any(style in s['styles'] for s in specs.values())
        },
        'specs': specs,
    }


def compare(first: typing.Sequence[QueryResult], second: typing.Sequence[QueryResult], metric: str) -> Difference:
    """How far ``first`` scores above ``second`` on ``metric``, over the queries both ran, stratified by spec."""
    second_scores = {(result.spec, result.query_id): result.scores[metric] for result in second}
    differences: dict[str, list[float]] = collections.defaultdict(list)
    for result in first:
        key = (result.spec, result.query_id)
        if key in second_scores:
            differences[result.spec].append(result.scores[metric] - second_scores[key])
    return paired_bootstrap(differences)


def _comparisons(results: Results, reference: str) -> list[dict[str, typing.Any]]:
    if reference not in results:
        return []
    rows = []
    for name, system_results in results.items():
        if name == reference:
            continue
        for metric in COMPARED_METRICS:
            difference = compare(results[reference], system_results, metric)
            rows.append(
                {
                    'system': reference,
                    'against': name,
                    'metric': metric,
                    'difference': round(difference.mean, PRECISION),
                    'interval': [round(difference.low, PRECISION), round(difference.high, PRECISION)],
                    'significant': difference.significant,
                }
            )
    return rows


def _spec_summary(results: typing.Sequence[QueryResult]) -> dict[str, typing.Any]:
    return {
        'queries': len(results),
        'all': _means(results),
        'styles': {
            style: _means(styled)
            for style in STYLES
            if (styled := [result for result in results if result.style == style])
        },
    }


def _query_record(result: QueryResult) -> dict[str, typing.Any]:
    return {
        'spec': result.spec,
        'id': result.query_id,
        'style': result.style,
        'scores': {metric: round(value, PRECISION) for metric, value in result.scores.items()},
        'top': result.ranking[:KEPT_RANKING],
    }


def _means(results: typing.Sequence[QueryResult]) -> dict[str, float]:
    return {metric: statistics.fmean(result.scores[metric] for result in results) for metric in METRIC_NAMES}


def _rounded(value: typing.Any) -> typing.Any:
    if isinstance(value, float):
        return round(value, PRECISION)
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    return value
