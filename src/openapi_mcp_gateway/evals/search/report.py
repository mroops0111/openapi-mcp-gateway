import collections
import importlib.metadata
import statistics
import typing

from ..stats import Difference, macro_mean, paired_bootstrap
from .dataset import STYLES, Benchmark, Split
from .metrics import METRICS
from .runner import RANDOM, QueryResult, Results


# The metrics a comparison between two variants is reported on.
COMPARED_METRICS = ('ndcg@10', 'recall@20')

# How many of a query's top operations a results file keeps, enough to see what outranked the answer.
_KEPT_RANKING = 10


def summarize(results: typing.Sequence[QueryResult]) -> dict[str, typing.Any]:
    """Mean scores per spec, per style within each spec, and macro-averaged over specs, overall and per style."""
    by_spec: dict[str, list[QueryResult]] = collections.defaultdict(list)
    for result in results:
        by_spec[result.spec].append(result)
    specs = {
        spec: {
            'queries': len(spec_results),
            'all': _means(spec_results),
            'styles': {
                style: _means(styled)
                for style in STYLES
                if (styled := [result for result in spec_results if result.style == style])
            },
        }
        for spec, spec_results in by_spec.items()
    }
    return {
        'macro': {
            metric.name: macro_mean(
                {
                    spec: [result.scores[metric.name] for result in spec_results]
                    for spec, spec_results in by_spec.items()
                }
            )
            for metric in METRICS
        },
        'macro_styles': {
            style: {
                metric.name: statistics.fmean(
                    specs[spec]['styles'][style][metric.name] for spec in specs if style in specs[spec]['styles']
                )
                for metric in METRICS
            }
            for style in STYLES
            if any(style in summary['styles'] for summary in specs.values())
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


def build_report(benchmark: Benchmark, split: Split, results: Results) -> dict[str, typing.Any]:
    """Everything a run found, as plain data: summaries, comparisons against the gated variant, and each ranking."""
    reference = benchmark.manifest.gate.variant
    comparisons = []
    if reference in results:
        for name in results:
            if name == reference:
                continue
            for metric in COMPARED_METRICS:
                difference = compare(results[reference], results[name], metric)
                comparisons.append(
                    {
                        'variant': reference,
                        'against': name,
                        'metric': metric,
                        'difference': round(difference.mean, 4),
                        'interval': [round(difference.low, 4), round(difference.high, 4)],
                        'significant': difference.significant,
                    }
                )
    return {
        'benchmark': {'version': benchmark.manifest.version, 'split': split},
        'gateway_version': importlib.metadata.version('openapi-mcp-gateway'),
        'variants': {name: _rounded(summarize(variant_results)) for name, variant_results in results.items()},
        'comparisons': comparisons,
        'queries': {
            name: [
                {
                    'spec': result.spec,
                    'id': result.query_id,
                    'style': result.style,
                    'scores': {metric: round(value, 4) for metric, value in result.scores.items()},
                    'top': result.ranking[:_KEPT_RANKING],
                }
                for result in variant_results
            ]
            for name, variant_results in results.items()
        },
    }


def to_markdown(report: dict[str, typing.Any]) -> str:
    """Render a report as the tables a results page or a PR description quotes."""
    metrics = [metric.name for metric in METRICS]
    variants: dict[str, typing.Any] = report['variants']
    lines = [
        f'Search benchmark v{report["benchmark"]["version"]}, {report["benchmark"]["split"]} split, '
        f'openapi-mcp-gateway {report["gateway_version"]}. Means over specs.',
        '',
        _table(
            ['Variant', *metrics],
            [[name, *(summary['macro'][m] for m in metrics)] for name, summary in variants.items()],
        ),
    ]
    reference = next((name for name in variants if name != RANDOM), None)
    if reference is not None:
        summary = variants[reference]
        lines += [
            '',
            f'By spec, `{reference}`:',
            '',
            _table(
                ['Spec', 'Queries', *metrics],
                [[spec, s['queries'], *(s['all'][m] for m in metrics)] for spec, s in summary['specs'].items()],
            ),
            '',
            f'By style, `{reference}`, means over specs:',
            '',
            _table(
                ['Style', *metrics],
                [[style, *(scores[m] for m in metrics)] for style, scores in summary['macro_styles'].items()],
            ),
        ]
    if report['comparisons']:
        lines += [
            '',
            'Paired bootstrap, 95% interval:',
            '',
            _table(
                ['Variant', 'Against', 'Metric', 'Difference', 'Interval', 'Significant'],
                [
                    [
                        row['variant'],
                        row['against'],
                        row['metric'],
                        f'{row["difference"]:+.3f}',
                        f'[{row["interval"][0]:+.3f}, {row["interval"][1]:+.3f}]',
                        'yes' if row['significant'] else 'no',
                    ]
                    for row in report['comparisons']
                ],
            ),
        ]
    return '\n'.join(lines) + '\n'


def baseline_from(report: dict[str, typing.Any], benchmark: Benchmark) -> dict[str, typing.Any]:
    """The gated numbers of a report, in the form committed as the baseline."""
    gate = benchmark.manifest.gate
    summary = report['variants'][gate.variant]
    return {
        'version': benchmark.manifest.version,
        'variant': gate.variant,
        'split': gate.split,
        'gateway_version': report['gateway_version'],
        'macro': {metric: summary['macro'][metric] for metric in gate.metrics},
        'specs': {spec: {metric: s['all'][metric] for metric in gate.metrics} for spec, s in summary['specs'].items()},
    }


def regressions(report: dict[str, typing.Any], baseline: dict[str, typing.Any], tolerance: float) -> list[str]:
    """Each gated number that fell more than ``tolerance`` below the baseline, described in one line."""
    summary = report['variants'][baseline['variant']]
    current = {'macro': summary['macro'], **{spec: s['all'] for spec, s in summary['specs'].items()}}
    expected = {'macro': baseline['macro'], **baseline['specs']}
    failures = []
    for scope, metrics in expected.items():
        for metric, value in metrics.items():
            now = current.get(scope, {}).get(metric)
            if now is None:
                failures.append(f'{scope} {metric}: missing from this run')
            elif now < value - tolerance:
                failures.append(f'{scope} {metric}: {now:.4f}, down from {value:.4f}')
    return failures


def _means(results: typing.Sequence[QueryResult]) -> dict[str, float]:
    return {metric.name: statistics.fmean(result.scores[metric.name] for result in results) for metric in METRICS}


def _rounded(value: typing.Any) -> typing.Any:
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    return value


def _table(header: list[str], rows: list[list[typing.Any]]) -> str:
    def cell(value: typing.Any) -> str:
        return f'{value:.3f}' if isinstance(value, float) else str(value)

    lines = ['| ' + ' | '.join(header) + ' |', '| ' + ' | '.join('---' for _ in header) + ' |']
    lines += ['| ' + ' | '.join(cell(value) for value in row) + ' |' for row in rows]
    return '\n'.join(lines)
