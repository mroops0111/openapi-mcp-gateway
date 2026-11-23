import asyncio
import pathlib

import click

from ..tool_calling import config_generation, dataset, markdown, reach, report, runner, validation
from .common import (
    benchmark_directory_option,
    echo_measured_coverage,
    emit_report,
    output_option,
    refuse_problems,
    variant_option,
    work_directory_option,
)


@click.group('tool-calling')
def group() -> None:
    """Whether a model finishes real tasks through the gateway, compared with one tool per operation.

    It runs models, which need the providers' API keys in their usual variables.
    """


@group.command('run')
@benchmark_directory_option('tool-calling')
@click.option(
    '--scenario', 'scenario_names', multiple=True, help='A scenario to run. Repeat for several; the default is all.'
)
@variant_option
@click.option('--model', 'model_names', multiple=True, help='A model to run. Repeat for several; the default is all.')
@click.option('--split', type=click.Choice(dataset.SPLITS), default='test', show_default=True)
@click.option('--repeat', type=click.IntRange(min=1), help="Runs per task, in place of the manifest's count.")
@output_option
def tool_calling_run(
    benchmark_directory: pathlib.Path,
    scenario_names: tuple[str, ...],
    variant_names: tuple[str, ...],
    model_names: tuple[str, ...],
    split: dataset.Split,
    repeat: int | None,
    output: pathlib.Path | None,
) -> None:
    """Run the benchmark's tasks under each variant and model, and print the tables."""
    benchmark = dataset.load(benchmark_directory)
    records = asyncio.run(
        runner.run(
            benchmark,
            scenario_names=scenario_names,
            variant_names=variant_names,
            model_names=model_names,
            split=split,
            repeat=repeat,
        )
    )
    coverage = reach.intent_coverage(benchmark, scenarios=scenario_names, variants=variant_names)
    built_report = report.build_report(benchmark, records, coverage)
    emit_report(built_report, markdown.to_markdown(built_report), output)


@group.command('check')
@benchmark_directory_option('tool-calling')
def tool_calling_check(benchmark_directory: pathlib.Path) -> None:
    """Prove every task of the benchmark can pass and every hand-written variant reaches it, for CI.

    It calls no model, and lists without failing the tasks each generated variant leaves out of reach,
    and those whose solution reads through an operation it hides.
    """
    benchmark = dataset.load(benchmark_directory)
    coverage = reach.intent_coverage(benchmark)
    problems = asyncio.run(validation.validate(benchmark, coverage))
    task_count = sum(len(scenario.tasks) for scenario in benchmark.scenarios.values())
    refuse_problems(problems)
    click.echo(f'All {task_count} tasks pass with their solutions, and every hand-written variant reaches them.')
    echo_measured_coverage(benchmark, coverage)


@group.command('generate-config')
@benchmark_directory_option('tool-calling')
@work_directory_option
@click.option(
    '--scenario',
    'scenario_names',
    multiple=True,
    help='A scenario to generate. Repeat for several; the default is all.',
)
@click.option(
    '--variant', 'variant_names', multiple=True, help='A variant to generate. Repeat for several; the default is all.'
)
def tool_calling_generate_config(
    benchmark_directory: pathlib.Path,
    work_directory: pathlib.Path,
    scenario_names: tuple[str, ...],
    variant_names: tuple[str, ...],
) -> None:
    """Write each scenario's generated variants with the manifest's config generator.

    Each run's workspace and exchange stay in the work directory.
    It runs a model, which needs its provider's API key in the usual variable.
    """
    benchmark = dataset.load(benchmark_directory)
    runs = asyncio.run(
        config_generation.generate(benchmark, work_directory, scenarios=scenario_names, variants=variant_names)
    )
    for run in runs:
        click.echo(
            f'{run.scenario} {run.variant}: {run.model}, {run.input_tokens} input and {run.output_tokens} output tokens, '
            f'{run.model_requests} requests'
        )
