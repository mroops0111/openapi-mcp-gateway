import typing

from .report import Comparison, Report
from .scoring import METRIC_NAMES


def to_markdown(report: Report) -> str:
    """Render ``report`` as the Markdown tables a results page or a PR description quotes.

    The tables show every system's means over specs, the reference system by spec and by style, and the comparisons.
    """
    sections = [
        f'Search benchmark v{report.benchmark_version}, {report.split} split, '
        f'openapi-mcp-gateway {report.gateway_version}. Means over specs.',
        _table(
            ['System', *METRIC_NAMES],
            [[name, *(summary.macro[metric] for metric in METRIC_NAMES)] for name, summary in report.systems.items()],
        ),
    ]
    if report.reference_system is not None:
        summary = report.systems[report.reference_system]
        sections += [
            f'By spec, `{report.reference_system}`:',
            _table(
                ['Spec', 'Queries', *METRIC_NAMES],
                [
                    [spec, spec_summary.query_count, *(spec_summary.overall[metric] for metric in METRIC_NAMES)]
                    for spec, spec_summary in summary.specs.items()
                ],
            ),
            f'By style, `{report.reference_system}`, means over specs:',
            _table(
                ['Style', *METRIC_NAMES],
                [
                    [style, *(scores[metric] for metric in METRIC_NAMES)]
                    for style, scores in summary.macro_by_style.items()
                ],
            ),
        ]
    if report.comparisons:
        sections += ['Paired bootstrap, 95% interval:', _comparison_table(report.comparisons)]
    return '\n\n'.join(sections) + '\n'


def _comparison_table(comparisons: typing.Sequence[Comparison]) -> str:
    return _table(
        ['System', 'Against', 'Metric', 'Difference', 'Interval', 'Significant'],
        [
            [
                comparison.system,
                comparison.against,
                comparison.metric,
                f'{comparison.difference:+.3f}',
                f'[{comparison.interval_low:+.3f}, {comparison.interval_high:+.3f}]',
                'yes' if comparison.significant else 'no',
            ]
            for comparison in comparisons
        ],
    )


def _table(header: typing.Sequence[str], rows: typing.Sequence[typing.Sequence[typing.Any]]) -> str:
    lines = ['| ' + ' | '.join(header) + ' |', '| ' + ' | '.join('---' for _ in header) + ' |']
    lines += ['| ' + ' | '.join(_cell(value) for value in row) + ' |' for row in rows]
    return '\n'.join(lines)


def _cell(value: typing.Any) -> str:
    return f'{value:.3f}' if isinstance(value, float) else str(value)
