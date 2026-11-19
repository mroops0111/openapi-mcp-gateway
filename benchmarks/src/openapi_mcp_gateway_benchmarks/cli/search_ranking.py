import asyncio
import json
import pathlib
import sys

import click

from ..search_ranking import dataset, markdown, overlap, report, runner
from ..search_ranking.annotations import automated, merge, workflow
from ..search_ranking.gate_scores import GateScores
from .common import BENCHMARK_DIRECTORY, WORK_DIRECTORY, emit_report, output_option, variant_option


@click.group('search-ranking')
def group() -> None:
    """How well search_operations ranks the operation a request asks for."""


@group.command('run')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.option('--split', type=click.Choice(dataset.SPLITS), default='test', show_default=True)
@variant_option
@output_option
def search_ranking_run(
    benchmark_directory: pathlib.Path, split: dataset.Split, variant_names: tuple[str, ...], output: pathlib.Path | None
) -> None:
    """Rank BENCHMARK_DIRECTORY's queries under each variant and print the tables."""
    benchmark = dataset.load(benchmark_directory)
    variants = runner.select_variants(benchmark, variant_names)
    results = asyncio.run(runner.run(benchmark, split, variants))
    built_report = report.build_report(benchmark, split, results)
    emit_report(built_report, markdown.to_markdown(built_report), output)


@group.command('check')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.option('--update', is_flag=True, help='Write this run as the new gate scores instead of comparing against them.')
def search_ranking_check(benchmark_directory: pathlib.Path, update: bool) -> None:
    """Check BENCHMARK_DIRECTORY's data, then fail when the gated variant scores below its gate scores, as CI runs it.

    The data passes when its files agree and no paraphrase or indirect query repeats its answer's words.
    """
    benchmark = dataset.load(benchmark_directory)
    style_report = overlap.check_styles(benchmark)
    for style, mean in style_report.mean_overlaps().items():
        click.echo(f'{style}: mean overlap {mean:.2f} over {len(style_report.overlaps_by_style[style])} queries')
    if style_report.flagged_queries:
        click.echo(f'\nQueries that repeat more than {overlap.MAX_NON_LITERAL_OVERLAP:.0%} of their answer:', err=True)
        for flagged in style_report.flagged_queries:
            query = flagged.query
            click.echo(f'{query.id} ({query.style}, {flagged.overlap:.2f}): {query.text}', err=True)
        sys.exit(1)

    gate = benchmark.manifest.gate
    variants = runner.select_variants(benchmark, [gate.variant])
    results = asyncio.run(runner.run(benchmark, gate.split, variants))
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


@group.command('generate')
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
    manifest = dataset.load_manifest(benchmark_directory)
    runs = asyncio.run(
        automated.annotate(
            manifest,
            benchmark_directory,
            work_directory,
            author_model=author_model,
            assessor_model=assessor_model,
            specs=spec_names,
        )
    )
    for run in runs:
        click.echo(
            f'{run.spec} {run.role}: {run.model}, {run.input_tokens} input and {run.output_tokens} output tokens'
        )


@group.group('annotations')
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
    manifest = dataset.load_manifest(benchmark_directory)
    for path in workflow.prepare_authoring(manifest, benchmark_directory, work_directory):
        click.echo(f'Wrote {path}')


@search_ranking_annotations.command('prepare-assessment')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.argument('work_directory', type=WORK_DIRECTORY)
def search_ranking_annotations_prepare_assessment(
    benchmark_directory: pathlib.Path, work_directory: pathlib.Path
) -> None:
    """Stage 2 writes each spec's queries, without the author's answers, and the independent assessor's instructions."""
    manifest = dataset.load_manifest(benchmark_directory)
    paths = workflow.prepare_assessment(manifest, benchmark_directory, work_directory)
    for path in paths:
        click.echo(f'Wrote {path}')


@search_ranking_annotations.command('collect')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
@click.argument('work_directory', type=WORK_DIRECTORY)
def search_ranking_annotations_collect(benchmark_directory: pathlib.Path, work_directory: pathlib.Path) -> None:
    """Stage 3 checks both annotators' output and writes it as BENCHMARK_DIRECTORY's annotations."""
    manifest = dataset.load_manifest(benchmark_directory)
    query_counts = workflow.collect(manifest, benchmark_directory, work_directory)
    for spec, query_count in query_counts.items():
        click.echo(f'{spec}: {query_count} queries collected')


@search_ranking_annotations.command('build')
@click.argument('benchmark_directory', type=BENCHMARK_DIRECTORY)
def search_ranking_annotations_build(benchmark_directory: pathlib.Path) -> None:
    """Stage 4 merges BENCHMARK_DIRECTORY's annotations into its queries and judgements, and reports the agreement.

    A query whose annotators share no answer stops the build until an adjudication for it is added.
    """
    manifest = dataset.load_manifest(benchmark_directory)
    # Every spec is built before any is written, so a spec that needs an adjudication leaves the data as it was.
    built = {spec: merge.build_spec_dataset(benchmark_directory, spec) for spec in manifest.specs}
    for spec, (spec_dataset, agreement) in built.items():
        dataset.write_spec_dataset(benchmark_directory, spec_dataset)
        click.echo(
            f'{spec}: {agreement.query_count} queries, shared answer on {agreement.shared_answer_count}, '
            f'kappa {agreement.kappa:.3f}, {agreement.adjudicated_count} adjudicated'
        )
