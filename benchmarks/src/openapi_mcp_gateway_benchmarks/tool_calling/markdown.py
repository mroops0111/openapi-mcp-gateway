import typing

from .report import AgentReport


def to_markdown(report: AgentReport) -> str:
    """Render ``report`` as the Markdown tables a results page or a PR description quotes.

    One table holds every scenario, variant, and model, and a second compares each variant with the baseline.
    A third lists each variant leaving tasks out of reach, which caps its success rate under every model.
    """
    sections = [
        f'Tool calling benchmark v{report.benchmark_version}, openapi-mcp-gateway {report.gateway_version}, '
        f'Pydantic AI {report.pydantic_ai_version}. '
        + ', '.join(f'`{name}` is {model}' for name, model in report.models.items())
        + '.',
        _table(
            [
                'Scenario',
                'Variant',
                'Model',
                'Runs',
                'Success',
                'Spread',
                'Input Tokens',
                'Tool Calls',
                'Tool Errors',
                'Extra Writes',
            ],
            [
                [
                    cell.scenario,
                    cell.variant,
                    cell.model,
                    f'{cell.run_count - cell.errored_count}/{cell.run_count}',
                    _percent(cell.success_rate),
                    _percent(cell.success_rate_spread),
                    f'{cell.mean_input_tokens:,.0f}',
                    f'{cell.mean_tool_calls:.1f}',
                    f'{cell.mean_tool_errors:.1f}',
                    f'{cell.mean_extra_writes:.1f}',
                ]
                for cell in report.cells
            ],
        ),
    ]
    if report.comparisons:
        sections += [
            'Against the baseline, paired by task, 95% bootstrap interval:',
            _table(
                ['Model', 'Variant', 'Measure', 'Baseline', 'Difference', 'Interval', 'Significant'],
                [
                    [
                        comparison.model,
                        comparison.variant,
                        comparison.measure,
                        _measure(comparison.measure, comparison.baseline_mean),
                        _measure(comparison.measure, comparison.difference, signed=True),
                        f'[{_measure(comparison.measure, comparison.interval_low, signed=True)}, '
                        f'{_measure(comparison.measure, comparison.interval_high, signed=True)}]',
                        'yes' if comparison.significant else 'no',
                    ]
                    for comparison in report.comparisons
                ],
            ),
        ]
    gaps = [entry for entry in report.coverage if entry.unreachable_tasks]
    if gaps:
        sections += [
            'Variants that serve no operation for some step of a task, so the task fails under every model:',
            _table(
                ['Scenario', 'Variant', 'Reachable Tasks', 'Out of Reach'],
                [
                    [
                        entry.scenario,
                        entry.variant,
                        f'{entry.reachable_task_count}/{entry.task_count}',
                        '<br>'.join(
                            f'{task_id}: {", ".join(steps)}' for task_id, steps in entry.unreachable_tasks.items()
                        ),
                    ]
                    for entry in gaps
                ],
            ),
        ]
    return '\n\n'.join(sections) + '\n'


def _measure(measure: str, value: float, *, signed: bool = False) -> str:
    sign = '+' if signed else ''
    if measure == 'success_rate':
        return f'{value * 100:{sign}.0f}%'
    if measure == 'extra_writes':
        return f'{value:{sign}.2f}'
    return f'{value:{sign},.0f}'


def _percent(value: float) -> str:
    return f'{value * 100:.0f}%'


def _table(header: typing.Sequence[str], rows: typing.Sequence[typing.Sequence[str]]) -> str:
    lines = ['| ' + ' | '.join(header) + ' |', '| ' + ' | '.join('---' for _ in header) + ' |']
    lines += ['| ' + ' | '.join(row) + ' |' for row in rows]
    return '\n'.join(lines)
