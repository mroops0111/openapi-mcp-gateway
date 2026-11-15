import typing

from .scoring import METRIC_NAMES


def to_markdown(report: dict[str, typing.Any]) -> str:
    """Render ``report`` as the Markdown tables a results page or a PR description quotes.

    The tables show every system's means over specs, the gated system by spec and by style, and the comparisons.
    """
    systems: dict[str, typing.Any] = report['systems']
    benchmark = report['benchmark']
    sections = [
        f'Search benchmark v{benchmark["version"]}, {benchmark["split"]} split, '
        f'openapi-mcp-gateway {report["gateway_version"]}. Means over specs.',
        _table(
            ['System', *METRIC_NAMES],
            [[name, *(summary['macro'][m] for m in METRIC_NAMES)] for name, summary in systems.items()],
        ),
    ]
    reference = report['reference']
    if reference is not None:
        summary = systems[reference]
        sections += [
            f'By spec, `{reference}`:',
            _table(
                ['Spec', 'Queries', *METRIC_NAMES],
                [[spec, s['queries'], *(s['all'][m] for m in METRIC_NAMES)] for spec, s in summary['specs'].items()],
            ),
            f'By style, `{reference}`, means over specs:',
            _table(
                ['Style', *METRIC_NAMES],
                [[style, *(scores[m] for m in METRIC_NAMES)] for style, scores in summary['macro_styles'].items()],
            ),
        ]
    if report['comparisons']:
        sections += ['Paired bootstrap, 95% interval:', _comparison_table(report['comparisons'])]
    return '\n\n'.join(sections) + '\n'


def _comparison_table(rows: typing.Sequence[dict[str, typing.Any]]) -> str:
    return _table(
        ['System', 'Against', 'Metric', 'Difference', 'Interval', 'Significant'],
        [
            [
                row['system'],
                row['against'],
                row['metric'],
                f'{row["difference"]:+.3f}',
                f'[{row["interval"][0]:+.3f}, {row["interval"][1]:+.3f}]',
                'yes' if row['significant'] else 'no',
            ]
            for row in rows
        ],
    )


def _table(header: typing.Sequence[str], rows: typing.Sequence[typing.Sequence[typing.Any]]) -> str:
    lines = ['| ' + ' | '.join(header) + ' |', '| ' + ' | '.join('---' for _ in header) + ' |']
    lines += ['| ' + ' | '.join(_cell(value) for value in row) + ' |' for row in rows]
    return '\n'.join(lines)


def _cell(value: typing.Any) -> str:
    return f'{value:.3f}' if isinstance(value, float) else str(value)
