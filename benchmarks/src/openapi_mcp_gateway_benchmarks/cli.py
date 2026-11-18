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
from .search_ranking import (
    annotation_workflow,
    annotations,
    automated_annotation,
    dataset,
    markdown,
    overlap,
    report,
    runner,
)
from .search_ranking.gate_scores import GateScores
from .spec_pins import SpecChecksumError
from .tool_calling import config_generation
from .tool_calling import dataset as tool_calling_dataset
from .tool_calling import markdown as tool_calling_markdown
from .tool_calling import reach as tool_calling_reach
from .tool_calling import report as tool_calling_report
from .tool_calling import runner as tool_calling_runner
from .tool_calling import validation as tool_calling_validation


BENCHMARK_DIRECTORY = click.Path(exists=True, file_okay=False, path_type=pathlib.Path)
WORK_DIRECTORY = click.Path(file_okay=False, path_type=pathlib.Path)

StepResult = typing.TypeVar('StepResult')

# The errors a command reports in one line, since each names what to fix, where any other error is a bug to trace.
HANDLED_ERRORS = (BenchmarkDataError, SpecChecksumError, ToolCallError, FileNotFoundError, httpx.HTTPError)


@click.group()
@click.option('-v', '--verbose', is_flag=True, help='Log progress, including each spec and variant as it runs.')
def main(verbose: bool) -> None:
    """Benchmarks of what the gateway does for a model."""
    # The gateway logs every registration at INFO, which buries a benchmark's own output.
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
    if verbose:
        logging.getLogger('openapi_mcp_gateway_benchmarks').setLevel(logging.INFO)


@main.group('search-ranking')
def search_ranking() -> None:
    """How well search_operations ranks the operation a request asks for."""


@search_ranking.command('run')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.option('--split', type=click.Choice(dataset.SPLITS), default='test', show_default=True)
@click.option(
    '--variant', 'variant_names', multiple=True, help='A variant to run. Repeat for several; the default is all.'
)
@click.option(
    '--output', type=click.Path(dir_okay=False, path_type=pathlib.Path), help='Write the full report as JSON.'
)
def search_ranking_run(
    benchmark_directory: pathlib.Path, split: dataset.Split, variant_names: tuple[str, ...], output: pathlib.Path | None
) -> None:
    """Rank BENCHMARK_DIRECTORY's queries under each variant and print the tables."""
    benchmark = _without_traceback(lambda: dataset.load(benchmark_directory))
    variants = _without_traceback(lambda: runner.select_variants(benchmark, variant_names))
    results = _without_traceback(lambda: asyncio.run(runner.run(benchmark, split, variants)))
    built_report = report.build_report(benchmark, split, results)
    if output is not None:
        output.write_text(built_report.model_dump_json(indent=2) + '\n', encoding='utf-8')
    click.echo(markdown.to_markdown(built_report), nl=False)


@search_ranking.command('check')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.option('--update', is_flag=True, help='Write this run as the new gate scores instead of comparing against them.')
def search_ranking_check(benchmark_directory: pathlib.Path, update: bool) -> None:
    """Check BENCHMARK_DIRECTORY's data, then fail when the gated variant scores below its gate scores, as CI runs it.

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
    variants = _without_traceback(lambda: runner.select_variants(benchmark, [gate.variant]))
    results = _without_traceback(lambda: asyncio.run(runner.run(benchmark, gate.split, variants)))
    current = GateScores.from_report(report.build_report(benchmark, gate.split, results), gate)
    if update:
        current.save(benchmark.gate_scores_path)
        click.echo(f'\nWrote {benchmark.gate_scores_path}')
        return
    if not benchmark.gate_scores_path.exists():
        raise click.ClickException(f'{benchmark.gate_scores_path} not found; run with --update to create it')
    failures = GateScores.load(benchmark.gate_scores_path).regressions(current, gate.tolerance)
    click.echo(json.dumps({'macro': current.macro, 'specs': current.specs}, indent=2))
    if failures:
        click.echo(f'\nBelow the gate scores by more than {gate.tolerance}:', err=True)
        click.echo('\n'.join(failures), err=True)
        sys.exit(1)
    click.echo(f'\nWithin {gate.tolerance} of the gate scores.')


@search_ranking.command('generate')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.argument('work_directory', type=WORK_DIRECTORY)
@click.option('--author-model', required=True, help='The query author, as Pydantic AI names a model.')
@click.option('--assessor-model', required=True, help='The independent assessor, ideally from another provider.')
@click.option('--spec', 'spec_names', multiple=True, help='A spec to annotate. Repeat for several; the default is all.')
def search_ranking_generate(
    benchmark_directory: pathlib.Path,
    work_directory: pathlib.Path,
    author_model: str,
    assessor_model: str,
    spec_names: tuple[str, ...],
) -> None:
    """Write each spec's annotations anew with both annotators run through Pydantic AI, into WORK_DIRECTORY.

    It runs stages 1 and 2 with the annotators between them, leaving what collect reads.
    It runs models, which need the providers' API keys in their usual variables.
    """
    manifest = _without_traceback(lambda: dataset.load_manifest(benchmark_directory))
    runs = _without_traceback(
        lambda: asyncio.run(
            automated_annotation.annotate(
                manifest,
                benchmark_directory,
                work_directory,
                author_model=author_model,
                assessor_model=assessor_model,
                specs=spec_names,
            )
        )
    )
    for run in runs:
        click.echo(
            f'{run.spec} {run.role}: {run.model}, {run.input_tokens} input and {run.output_tokens} output tokens'
        )


@search_ranking.group('annotations')
def search_ranking_annotations() -> None:
    """Make the queries and judgements anew, in four stages, for when the data is annotated again.

    The two annotators run between the stages, from the files and instructions each stage writes.
    """


@search_ranking_annotations.command('prepare-authoring')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.argument('work_directory', type=WORK_DIRECTORY)
def search_ranking_annotations_prepare_authoring(
    benchmark_directory: pathlib.Path, work_directory: pathlib.Path
) -> None:
    """Stage 1 writes each spec's operation list and the query author's instructions into WORK_DIRECTORY."""
    manifest = _without_traceback(lambda: dataset.load_manifest(benchmark_directory))
    for path in annotation_workflow.prepare_authoring(manifest, benchmark_directory, work_directory):
        click.echo(f'Wrote {path}')


@search_ranking_annotations.command('prepare-assessment')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.argument('work_directory', type=WORK_DIRECTORY)
def search_ranking_annotations_prepare_assessment(
    benchmark_directory: pathlib.Path, work_directory: pathlib.Path
) -> None:
    """Stage 2 writes each spec's queries, without the author's answers, and the independent assessor's instructions."""
    manifest = _without_traceback(lambda: dataset.load_manifest(benchmark_directory))
    paths = _without_traceback(
        lambda: annotation_workflow.prepare_assessment(manifest, benchmark_directory, work_directory)
    )
    for path in paths:
        click.echo(f'Wrote {path}')


@search_ranking_annotations.command('collect')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.argument('work_directory', type=WORK_DIRECTORY)
def search_ranking_annotations_collect(benchmark_directory: pathlib.Path, work_directory: pathlib.Path) -> None:
    """Stage 3 checks both annotators' output and writes it as BENCHMARK_DIRECTORY's annotations."""
    manifest = _without_traceback(lambda: dataset.load_manifest(benchmark_directory))
    query_counts = _without_traceback(
        lambda: annotation_workflow.collect(manifest, benchmark_directory, work_directory)
    )
    for spec, query_count in query_counts.items():
        click.echo(f'{spec}: {query_count} queries collected')


@search_ranking_annotations.command('build')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
def search_ranking_annotations_build(benchmark_directory: pathlib.Path) -> None:
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


@main.group('tool-calling')
def tool_calling() -> None:
    """Whether a model finishes real tasks through the gateway, compared with one tool per operation.

    It runs models, which need the providers' API keys in their usual variables.
    """


@tool_calling.command('run')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.option(
    '--scenario', 'scenario_names', multiple=True, help='A scenario to run. Repeat for several; the default is all.'
)
@click.option(
    '--variant', 'variant_names', multiple=True, help='A variant to run. Repeat for several; the default is all.'
)
@click.option('--model', 'model_names', multiple=True, help='A model to run. Repeat for several; the default is all.')
@click.option('--split', type=click.Choice(tool_calling_dataset.SPLITS), default='test', show_default=True)
@click.option('--repetitions', type=click.IntRange(min=1), help="Runs per task, in place of the manifest's count.")
@click.option(
    '--output', type=click.Path(dir_okay=False, path_type=pathlib.Path), help='Write the full report as JSON.'
)
def tool_calling_run(
    benchmark_directory: pathlib.Path,
    scenario_names: tuple[str, ...],
    variant_names: tuple[str, ...],
    model_names: tuple[str, ...],
    split: tool_calling_dataset.Split,
    repetitions: int | None,
    output: pathlib.Path | None,
) -> None:
    """Run BENCHMARK_DIRECTORY's tasks under each variant and model, and print the tables."""
    benchmark = _without_traceback(lambda: tool_calling_dataset.load(benchmark_directory))
    records = _without_traceback(
        lambda: asyncio.run(
            tool_calling_runner.run(
                benchmark,
                scenario_names=scenario_names,
                variant_names=variant_names,
                model_names=model_names,
                split=split,
                repetitions=repetitions,
            )
        )
    )
    coverage = _without_traceback(lambda: tool_calling_reach.intent_coverage(benchmark))
    built_report = tool_calling_report.build_report(benchmark, records, coverage)
    if output is not None:
        output.write_text(built_report.model_dump_json(indent=2) + '\n', encoding='utf-8')
    click.echo(tool_calling_markdown.to_markdown(built_report), nl=False)


@tool_calling.command('check')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
def tool_calling_check(benchmark_directory: pathlib.Path) -> None:
    """Prove every task of BENCHMARK_DIRECTORY can pass and every hand-written variant reaches it, for CI.

    It calls no model, and lists the tasks each generated variant leaves out of reach without failing.
    """
    benchmark = _without_traceback(lambda: tool_calling_dataset.load(benchmark_directory))
    problems = _without_traceback(lambda: asyncio.run(tool_calling_validation.validate(benchmark)))
    task_count = sum(len(scenario.tasks) for scenario in benchmark.scenarios.values())
    if problems:
        for problem in problems:
            click.echo(f'{problem.scenario} {problem.task_id} under {problem.variant}: {problem.problem}', err=True)
        sys.exit(1)
    click.echo(f'All {task_count} tasks pass with their solutions, and every hand-written variant reaches them.')
    generated = set(benchmark.manifest.config_generator.variants)
    coverage = _without_traceback(lambda: tool_calling_reach.intent_coverage(benchmark))
    for entry in coverage:
        if entry.variant in generated and entry.unreachable_tasks:
            click.echo(
                f'{entry.scenario} under {entry.variant} reaches {entry.reachable_task_count} of {entry.task_count} '
                f'tasks, missing {", ".join(entry.unreachable_tasks)}.'
            )


@tool_calling.command('generate')
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
    benchmark = _without_traceback(lambda: tool_calling_dataset.load(benchmark_directory))
    runs = _without_traceback(
        lambda: asyncio.run(
            config_generation.generate(benchmark, work_directory, scenarios=scenario_names, variants=variant_names)
        )
    )
    for run in runs:
        click.echo(
            f'{run.scenario} {run.variant}: {run.model}, {run.input_tokens} input and {run.output_tokens} output tokens, '
            f'{run.model_requests} requests'
        )


def _without_traceback(step: typing.Callable[[], StepResult]) -> StepResult:
    """Run ``step``, turning the errors bad data, a stale pin, or an unreachable spec raise into a clean error."""
    try:
        return step()
    except HANDLED_ERRORS as error:
        raise click.ClickException(str(error)) from error
