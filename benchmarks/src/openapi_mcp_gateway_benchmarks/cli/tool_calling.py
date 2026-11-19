import asyncio
import pathlib
import sys

import click

from ..tool_calling import config_generation, dataset, markdown, reach, report, runner, validation
from .common import BENCHMARK_DIRECTORY, WORK_DIRECTORY, emit_report, output_option, variant_option


@click.group('tool-calling')
def group() -> None:
    """Whether a model finishes real tasks through the gateway, compared with one tool per operation.

    It runs models, which need the providers' API keys in their usual variables.
    """


@group.command('run')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.option(
    '--scenario', 'scenario_names', multiple=True, help='A scenario to run. Repeat for several; the default is all.'
)
@variant_option
@click.option('--model', 'model_names', multiple=True, help='A model to run. Repeat for several; the default is all.')
@click.option('--split', type=click.Choice(dataset.SPLITS), default='test', show_default=True)
@click.option('--repetitions', type=click.IntRange(min=1), help="Runs per task, in place of the manifest's count.")
@output_option
def tool_calling_run(
    benchmark_directory: pathlib.Path,
    scenario_names: tuple[str, ...],
    variant_names: tuple[str, ...],
    model_names: tuple[str, ...],
    split: dataset.Split,
    repetitions: int | None,
    output: pathlib.Path | None,
) -> None:
    """Run BENCHMARK_DIRECTORY's tasks under each variant and model, and print the tables."""
    benchmark = dataset.load(benchmark_directory)
    records = asyncio.run(
        runner.run(
            benchmark,
            scenario_names=scenario_names,
            variant_names=variant_names,
            model_names=model_names,
            split=split,
            repetitions=repetitions,
        )
    )
    coverage = reach.intent_coverage(benchmark, scenarios=scenario_names, variants=variant_names)
    built_report = report.build_report(benchmark, records, coverage)
    emit_report(built_report, markdown.to_markdown(built_report), output)


@group.command('check')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
def tool_calling_check(benchmark_directory: pathlib.Path) -> None:
    """Prove every task of BENCHMARK_DIRECTORY can pass and every hand-written variant reaches it, for CI.

    It calls no model, and lists the tasks each generated variant leaves out of reach without failing.
    """
    benchmark = dataset.load(benchmark_directory)
    coverage = reach.intent_coverage(benchmark)
    problems = asyncio.run(validation.validate(benchmark, coverage))
    task_count = sum(len(scenario.tasks) for scenario in benchmark.scenarios.values())
    if problems:
        for problem in problems:
            click.echo(f'{problem.scenario} {problem.task_id} under {problem.variant}: {problem.problem}', err=True)
        sys.exit(1)
    click.echo(f'All {task_count} tasks pass with their solutions, and every hand-written variant reaches them.')
    generated = set(benchmark.manifest.config_generator.variants)
    for entry in coverage:
        if entry.variant in generated and entry.unreachable_tasks:
            click.echo(
                f'{entry.scenario} under {entry.variant} reaches {entry.reachable_task_count} of {entry.task_count} '
                f'tasks, missing {", ".join(entry.unreachable_tasks)}.'
            )


@group.command('generate')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.argument('work_directory', type=WORK_DIRECTORY)
@click.option(
    '--scenario',
    'scenario_names',
    multiple=True,
    help='A scenario to generate. Repeat for several; the default is all.',
)
@click.option(
    '--variant', 'variant_names', multiple=True, help='A variant to generate. Repeat for several; the default is all.'
)
def tool_calling_generate(
    benchmark_directory: pathlib.Path,
    work_directory: pathlib.Path,
    scenario_names: tuple[str, ...],
    variant_names: tuple[str, ...],
) -> None:
    """Write each scenario's generated variants with the manifest's config generator.

    Each run's workspace and exchange stay in WORK_DIRECTORY.
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
