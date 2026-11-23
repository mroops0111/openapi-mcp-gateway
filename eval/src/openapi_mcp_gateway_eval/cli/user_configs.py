import asyncio
import pathlib
import typing

import click
from pydantic_ai.exceptions import UserError
from pydantic_ai.models import infer_model

from ..tool_calling import markdown, reach, report, runner, user_configs, validation
from .common import (
    config_option,
    echo_measured_coverage,
    emit_report,
    output_option,
    refuse_problems,
    task_file_option,
)


# What ``--against`` takes to run the user's configs alone.
NO_COMPARISON = 'none'


@click.command('run')
@task_file_option
@config_option
@click.option(
    '--model',
    'model_names',
    multiple=True,
    required=True,
    help='A model to run, as Pydantic AI names it, such as anthropic:claude-haiku-5-5. Repeat for several.',
)
@click.option(
    '--against',
    multiple=True,
    type=click.Choice([*user_configs.COMPARISONS, NO_COMPARISON]),
    default=('baseline',),
    show_default=True,
    help='What to compare the configs with. Repeat for several, or give none to run the configs alone.',
)
@click.option('--repeat', type=click.IntRange(min=1), default=1, show_default=True, help='Runs per task.')
@output_option
def run(
    task_file: pathlib.Path,
    configs: dict[str, pathlib.Path],
    model_names: tuple[str, ...],
    against: tuple[str, ...],
    repeat: int,
    output: pathlib.Path | None,
) -> None:
    """Run the task file's tasks under each config and comparison with each model, and print the tables.

    It first checks the tasks as ``check`` does and runs no model when one fails.
    No real API is called, since a replay answers every request from the tasks and the specs.
    It runs models, which need the providers' API keys in their usual variables.
    """
    comparisons = _comparisons(against)
    for model_name in model_names:
        _check_model(model_name)
    benchmark = user_configs.load(configs, task_file, model_names=model_names, repeat=repeat)
    variant_names = [*configs, *comparisons]
    coverage = reach.intent_coverage(benchmark, variants=variant_names)
    refuse_problems(asyncio.run(validation.validate(benchmark, coverage)))
    task_count = sum(len(scenario.tasks) for scenario in benchmark.scenarios.values())
    click.echo(
        f'Running {task_count} tasks under {len(variant_names)} variants with {len(model_names)} models, '
        f'{repeat} times each, {task_count * len(variant_names) * len(model_names) * repeat} runs in all.',
        err=True,
    )
    records = asyncio.run(runner.run(benchmark, variant_names=variant_names, split=None))
    built_report = report.build_report(benchmark, records, coverage)
    emit_report(built_report, markdown.to_markdown(built_report), output)


@click.command('check')
@task_file_option
@config_option
def check(task_file: pathlib.Path, configs: dict[str, pathlib.Path]) -> None:
    """Prove each task of the task file with a solution can pass, and list what each config leaves out of reach.

    It calls no model and no real API.
    """
    benchmark = user_configs.load(configs, task_file, model_names=())
    coverage = reach.intent_coverage(benchmark)
    refuse_problems(asyncio.run(validation.validate(benchmark, coverage)))
    tasks = [task for scenario in benchmark.scenarios.values() for task in scenario.tasks]
    unsolved = [task.id for task in tasks if not task.solution]
    click.echo(f'All {len(tasks) - len(unsolved)} tasks with a solution pass with it.')
    if unsolved:
        click.echo(f'Nothing proves {", ".join(unsolved)} can pass, since they have no solution.')
    echo_measured_coverage(benchmark, coverage)


def _comparisons(against: typing.Sequence[str]) -> list[str]:
    if NO_COMPARISON in against and len(against) > 1:
        raise click.BadParameter(f'{NO_COMPARISON} cannot come with a comparison', param_hint='--against')
    return [comparison for comparison in dict.fromkeys(against) if comparison != NO_COMPARISON]


def _check_model(model_name: str) -> None:
    """Refuse a model Pydantic AI cannot build, such as an unknown provider or one whose API key is missing."""
    try:
        infer_model(model_name)
    except UserError as error:
        raise click.BadParameter(str(error), param_hint='--model') from error
