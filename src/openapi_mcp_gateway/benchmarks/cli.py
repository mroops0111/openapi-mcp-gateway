import asyncio
import json
import logging
import pathlib
import sys
import typing

import click
import httpx

from .errors import BenchmarkDataError
from .gateway_connection import ToolCallError
from .search import annotation_workflow, annotations, dataset, markdown, overlap, report, runner
from .search.baseline import Baseline
from .spec_pins import SpecChecksumError


BENCHMARK_DIRECTORY = click.Path(exists=True, file_okay=False, path_type=pathlib.Path)
WORK_DIRECTORY = click.Path(file_okay=False, path_type=pathlib.Path)

StepResult = typing.TypeVar('StepResult')

# The errors a command reports in one line, since each names what to fix, where any other error is a bug to trace.
HANDLED_ERRORS = (BenchmarkDataError, SpecChecksumError, ToolCallError, FileNotFoundError, httpx.HTTPError)


@click.group()
@click.option('-v', '--verbose', is_flag=True, help='Log progress, including each spec and system as it runs.')
def main(verbose: bool) -> None:
    """Benchmarks of what the gateway does for a model."""
    # The gateway logs every registration at INFO, which buries a benchmark's own output.
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
    if verbose:
        logging.getLogger('openapi_mcp_gateway.benchmarks').setLevel(logging.INFO)


@main.group()
def search() -> None:
    """How well search_operations ranks the operation a request asks for."""


@search.command('run')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.option('--split', type=click.Choice(dataset.SPLITS), default='test', show_default=True)
@click.option(
    '--system', 'system_names', multiple=True, help='A system to run. Repeat for several; the default is all.'
)
@click.option(
    '--output', type=click.Path(dir_okay=False, path_type=pathlib.Path), help='Write the full report as JSON.'
)
def search_run(
    benchmark_directory: pathlib.Path, split: dataset.Split, system_names: tuple[str, ...], output: pathlib.Path | None
) -> None:
    """Rank BENCHMARK_DIRECTORY's queries under each system and print the tables."""
    benchmark = _without_traceback(lambda: dataset.load(benchmark_directory))
    systems = _without_traceback(lambda: runner.select_systems(benchmark, system_names))
    results = _without_traceback(lambda: asyncio.run(runner.run(benchmark, split, systems)))
    built_report = report.build_report(benchmark, split, results)
    if output is not None:
        output.write_text(built_report.model_dump_json(indent=2) + '\n', encoding='utf-8')
    click.echo(markdown.to_markdown(built_report), nl=False)


@search.command('check')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.option('--update', is_flag=True, help='Write this run as the new baseline instead of comparing against it.')
def search_check(benchmark_directory: pathlib.Path, update: bool) -> None:
    """Check BENCHMARK_DIRECTORY's data, then fail when the gated system scores below its baseline, as CI runs it.

    The data passes when its files agree and no paraphrase or indirect query repeats its answer's words.
    """
    benchmark = _without_traceback(lambda: dataset.load(benchmark_directory))
    style_report = _without_traceback(lambda: overlap.check_styles(benchmark))
    for style, mean in style_report.mean_overlaps().items():
        click.echo(f'{style}: mean overlap {mean:.2f} over {len(style_report.overlaps_by_style[style])} queries')
    if style_report.flagged_queries:
        click.echo(f'\nQueries that repeat more than {overlap.MAX_NON_LITERAL_OVERLAP:.0%} of their answer:', err=True)
        for flagged in style_report.flagged_queries:
            query = flagged.query
            click.echo(f'{query.id} ({query.style}, {flagged.overlap:.2f}): {query.text}', err=True)
        sys.exit(1)

    gate = benchmark.manifest.gate
    systems = _without_traceback(lambda: runner.select_systems(benchmark, [gate.system]))
    results = _without_traceback(lambda: asyncio.run(runner.run(benchmark, gate.split, systems)))
    current = Baseline.from_report(report.build_report(benchmark, gate.split, results), gate)
    if update:
        current.save(benchmark.baseline_path)
        click.echo(f'\nWrote {benchmark.baseline_path}')
        return
    if not benchmark.baseline_path.exists():
        raise click.ClickException(f'{benchmark.baseline_path} not found; run with --update to create it')
    failures = Baseline.load(benchmark.baseline_path).regressions(current, gate.tolerance)
    click.echo(json.dumps({'macro': current.macro, 'specs': current.specs}, indent=2))
    if failures:
        click.echo(f'\nBelow the baseline by more than {gate.tolerance}:', err=True)
        click.echo('\n'.join(failures), err=True)
        sys.exit(1)
    click.echo(f'\nWithin {gate.tolerance} of the baseline.')


@search.group('annotations')
def search_annotations() -> None:
    """Make the queries and judgements anew, in four stages, for when the data is annotated again.

    The two annotators run between the stages, from the files and instructions each stage writes.
    """


@search_annotations.command('prepare-authoring')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.argument('work_directory', type=WORK_DIRECTORY)
def search_annotations_prepare_authoring(benchmark_directory: pathlib.Path, work_directory: pathlib.Path) -> None:
    """Stage 1 writes each spec's operation list and the query author's instructions into WORK_DIRECTORY."""
    manifest = _without_traceback(lambda: dataset.load_manifest(benchmark_directory))
    for path in annotation_workflow.prepare_authoring(manifest, benchmark_directory, work_directory):
        click.echo(f'Wrote {path}')


@search_annotations.command('prepare-assessment')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.argument('work_directory', type=WORK_DIRECTORY)
def search_annotations_prepare_assessment(benchmark_directory: pathlib.Path, work_directory: pathlib.Path) -> None:
    """Stage 2 writes each spec's queries, without the author's answers, and the independent assessor's instructions."""
    manifest = _without_traceback(lambda: dataset.load_manifest(benchmark_directory))
    paths = _without_traceback(
        lambda: annotation_workflow.prepare_assessment(manifest, benchmark_directory, work_directory)
    )
    for path in paths:
        click.echo(f'Wrote {path}')


@search_annotations.command('collect')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.argument('work_directory', type=WORK_DIRECTORY)
def search_annotations_collect(benchmark_directory: pathlib.Path, work_directory: pathlib.Path) -> None:
    """Stage 3 checks both annotators' output and writes it as BENCHMARK_DIRECTORY's annotations."""
    manifest = _without_traceback(lambda: dataset.load_manifest(benchmark_directory))
    query_counts = _without_traceback(
        lambda: annotation_workflow.collect(manifest, benchmark_directory, work_directory)
    )
    for spec, query_count in query_counts.items():
        click.echo(f'{spec}: {query_count} queries collected')


@search_annotations.command('build')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
def search_annotations_build(benchmark_directory: pathlib.Path) -> None:
    """Stage 4 merges BENCHMARK_DIRECTORY's annotations into its queries and judgements, and reports the agreement.

    A query whose annotators share no answer stops the build until an adjudication for it is added.
    """
    manifest = _without_traceback(lambda: dataset.load_manifest(benchmark_directory))
    # Every spec is built before any is written, so a spec that needs an adjudication leaves the data as it was.
    built = _without_traceback(
        lambda: {spec: annotations.build_spec_dataset(benchmark_directory, spec) for spec in manifest.specs}
    )
    for spec, (spec_dataset, agreement) in built.items():
        dataset.write_spec_dataset(benchmark_directory, spec_dataset)
        click.echo(
            f'{spec}: {agreement.query_count} queries, shared answer on {agreement.shared_answer_count}, '
            f'kappa {agreement.kappa:.3f}, {agreement.adjudicated_count} adjudicated'
        )


@main.group()
def agent() -> None:
    """Whether a model finishes real tasks through the gateway, compared with one tool per operation.

    It runs models, which needs the benchmarks extra and the providers' API keys in their usual variables.
    """


@agent.command('run')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.option(
    '--scenario', 'scenario_names', multiple=True, help='A scenario to run. Repeat for several; the default is all.'
)
@click.option(
    '--variant', 'variant_names', multiple=True, help='A variant to run. Repeat for several; the default is all.'
)
@click.option('--model', 'model_names', multiple=True, help='A model to run. Repeat for several; the default is all.')
@click.option('--split', type=click.Choice(['practice', 'final']), default='final', show_default=True)
@click.option('--repetitions', type=click.IntRange(min=1), help="Runs per task, in place of the manifest's count.")
@click.option(
    '--output', type=click.Path(dir_okay=False, path_type=pathlib.Path), help='Write the full report as JSON.'
)
def agent_run(
    benchmark_directory: pathlib.Path,
    scenario_names: tuple[str, ...],
    variant_names: tuple[str, ...],
    model_names: tuple[str, ...],
    split: str,
    repetitions: int | None,
    output: pathlib.Path | None,
) -> None:
    """Run BENCHMARK_DIRECTORY's tasks under each variant and model, and print the tables."""
    agent_dataset, agent_markdown, agent_report, agent_runner, _ = _agent_modules()
    benchmark = _without_traceback(lambda: agent_dataset.load(benchmark_directory))
    records = _without_traceback(
        lambda: asyncio.run(
            agent_runner.run(
                benchmark,
                scenario_names=scenario_names,
                variant_names=variant_names,
                model_names=model_names,
                split=split,
                repetitions=repetitions,
            )
        )
    )
    built_report = agent_report.build_report(benchmark, records)
    if output is not None:
        output.write_text(built_report.model_dump_json(indent=2) + '\n', encoding='utf-8')
    click.echo(agent_markdown.to_markdown(built_report), nl=False)


@agent.command('validate')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
def agent_validate(benchmark_directory: pathlib.Path) -> None:
    """Prove every task of BENCHMARK_DIRECTORY can pass and every variant reaches it, calling no model, as CI runs it."""
    agent_dataset, _, _, _, agent_validation = _agent_modules()
    benchmark = _without_traceback(lambda: agent_dataset.load(benchmark_directory))
    problems = _without_traceback(lambda: asyncio.run(agent_validation.validate(benchmark)))
    task_count = sum(len(scenario.tasks) for scenario in benchmark.scenarios.values())
    if problems:
        for problem in problems:
            click.echo(f'{problem.scenario} {problem.task_id} under {problem.variant}: {problem.problem}', err=True)
        sys.exit(1)
    click.echo(f'All {task_count} tasks pass with their solutions, and every variant reaches its in-intent tasks.')


def _agent_modules() -> tuple[typing.Any, ...]:
    """The agent benchmark's modules, imported only when a command needs them, since they need the benchmarks extra."""
    try:
        from .agent import dataset as agent_dataset
        from .agent import markdown as agent_markdown
        from .agent import report as agent_report
        from .agent import runner as agent_runner
        from .agent import validation as agent_validation
    except ImportError as error:
        raise click.ClickException(
            f'The agent benchmark needs the benchmarks extra, such as uv sync --extra benchmarks ({error})'
        ) from error
    return agent_dataset, agent_markdown, agent_report, agent_runner, agent_validation


def _without_traceback(step: typing.Callable[[], StepResult]) -> StepResult:
    """Run ``step``, turning the errors bad data, a stale pin, or an unreachable spec raise into a clean error."""
    try:
        return step()
    except HANDLED_ERRORS as error:
        raise click.ClickException(str(error)) from error
