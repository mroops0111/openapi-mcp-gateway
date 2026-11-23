import typing

from .report import AgentReport


def to_markdown(report: AgentReport) -> str:
    """Render ``report`` as the Markdown tables a results page or a PR description quotes.

    One table holds every scenario, variant, and model with what it cost, and a second compares each with the baseline.
    Then come the variants leaving tasks out of reach, which caps their success rate under every model.
    After them come the variants hiding a read a solution makes, and the failed runs that read data never set.
    """
    sections = [
        f'{_title(report)}, openapi-mcp-gateway {report.gateway_version}, Pydantic AI {report.pydantic_ai_version}. '
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
                'Cached',
                'Output Tokens',
                'Cost',
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
                    _percent(cell.mean_cache_read_tokens / cell.mean_input_tokens if cell.mean_input_tokens else 0.0),
                    f'{cell.mean_output_tokens:,.0f}',
                    _dollars(cell.total_cost),
                    f'{cell.mean_tool_calls:.1f}',
                    f'{cell.mean_tool_errors:.1f}',
                    f'{cell.mean_extra_writes:.1f}',
                ]
                for cell in report.cells
            ],
        ),
        'Tokens are means per run, Cached is the share of input tokens read from the prompt cache, '
        "and Cost is the cell's total in US dollars. In all, "
        + ', '.join(f'`{model}` cost {_dollars(cost)}' for model, cost in _model_costs(report).items())
        + '.',
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
    lookups = [entry for entry in report.coverage if entry.hidden_lookups]
    if lookups:
        sections += [
            "Variants that hide a read a task's solution makes, which a model has to reach another way:",
            _table(
                ['Scenario', 'Variant', 'Hidden Reads'],
                [
                    [
                        entry.scenario,
                        entry.variant,
                        '<br>'.join(
                            f'{task_id}: {", ".join(reads)}' for task_id, reads in entry.hidden_lookups.items()
                        ),
                    ]
                    for entry in lookups
                ],
            ),
        ]
    misled = [record for record in report.records if not record.passed and not record.errored and record.unset_reads]
    if misled:
        sections += [
            'Failed runs that read data their task never set, where a gap in the data may have misled the model:',
            _table(
                ['Scenario', 'Variant', 'Model', 'Task', 'Unset Reads'],
                [
                    [record.scenario, record.variant, record.model, record.task_id, '<br>'.join(record.unset_reads)]
                    for record in misled
                ],
            ),
        ]
    return '\n\n'.join(sections) + '\n'


def _title(report: AgentReport) -> str:
    if report.benchmark_version is None:
        return 'Tool calling eval of your own configs'
    return f'Tool calling benchmark v{report.benchmark_version}'


def _measure(measure: str, value: float, *, signed: bool = False) -> str:
    sign = '+' if signed else ''
    if measure == 'success_rate':
        return f'{value * 100:{sign}.0f}%'
    if measure == 'extra_writes':
        return f'{value:{sign}.2f}'
    return f'{value:{sign},.0f}'


def _model_costs(report: AgentReport) -> dict[str, float | None]:
    costs: dict[str, float | None] = {}
    for cell in report.cells:
        known = costs.get(cell.model, 0.0)
        costs[cell.model] = None if known is None or cell.total_cost is None else known + cell.total_cost
    return costs


def _dollars(value: float | None) -> str:
    # Four places, since one cell of the cheapest models can cost under a cent.
    return 'unknown' if value is None else f'${value:,.4f}'


def _percent(value: float) -> str:
    return f'{value * 100:.0f}%'


def _table(header: typing.Sequence[str], rows: typing.Sequence[typing.Sequence[str]]) -> str:
    lines = ['| ' + ' | '.join(header) + ' |', '| ' + ' | '.join('---' for _ in header) + ' |']
    lines += ['| ' + ' | '.join(row) + ' |' for row in rows]
    return '\n'.join(lines)
