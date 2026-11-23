import asyncio
import json
import pathlib
import sys

import click

from ..search_ranking import dataset, markdown, overlap, report, runner
from ..search_ranking.annotations import merge
from ..search_ranking.annotations.pipeline import AnnotationPipeline
from ..search_ranking.gate_scores import GateScores
from .common import benchmark_directory_option, emit_report, output_option, variant_option, work_directory_option


@click.group('search-ranking')
def group() -> None:
    """How well search_operations ranks the operation a request asks for."""


@group.command('run')
@benchmark_directory_option('search-ranking')
@click.option('--split', type=click.Choice(dataset.SPLITS), default='test', show_default=True)
@variant_option
@output_option
def search_ranking_run(
    benchmark_directory: pathlib.Path, split: dataset.Split, variant_names: tuple[str, ...], output: pathlib.Path | None
) -> None:
    """Rank the benchmark's queries under each variant and print the tables."""
    benchmark = dataset.load(benchmark_directory)
    variants = runner.select_variants(benchmark, variant_names)
    results = asyncio.run(runner.run(benchmark, split, variants))
    built_report = report.build_report(benchmark, split, results)
    emit_report(built_report, markdown.to_markdown(built_report), output)


@group.command('check')
@benchmark_directory_option('search-ranking')
@click.option('--update', is_flag=True, help='Write this run as the new gate scores instead of comparing against them.')
def search_ranking_check(benchmark_directory: pathlib.Path, update: bool) -> None:
    """Check the benchmark's data, then fail when the gated variant scores below its gate scores, as CI runs it.

    The data passes when its files agree and no paraphrase or indirect query repeats its answer's words.
    """
    benchmark = dataset.load(benchmark_directory)
    _refuse_contradicted_styles(benchmark)
    current = _gate_scores(benchmark)
    if update:
        _save_gate_scores(benchmark, current)
        return
    gate = benchmark.manifest.gate
    if not benchmark.gate_scores_path.exists():
        raise click.ClickException(f'{benchmark.gate_scores_path} not found; run with --update to create it')
    failures = GateScores.load(benchmark.gate_scores_path).regressions(current, gate.tolerance)
    click.echo(json.dumps({'macro': current.macro, 'specs': current.specs}, indent=2))
    if failures:
        click.echo(f'\nBelow the gate scores by more than {gate.tolerance}:', err=True)
        click.echo('\n'.join(failures), err=True)
        sys.exit(1)
    click.echo(f'\nWithin {gate.tolerance} of the gate scores.')


@group.command('generate-annotations')
@benchmark_directory_option('search-ranking')
@work_directory_option
@click.option(
    '--author-model', required=True, help='The query author, which also rewords, as Pydantic AI names a model.'
)
@click.option('--assessor-model', required=True, help='The independent assessor, ideally from another provider.')
@click.option('--adjudicator-model', required=True, help='The adjudicator, ideally from a third provider.')
@click.option('--spec', 'spec_names', multiple=True, help='A spec to annotate. Repeat for several; the default is all.')
def search_ranking_generate_annotations(
    benchmark_directory: pathlib.Path,
    work_directory: pathlib.Path,
    author_model: str,
    assessor_model: str,
    adjudicator_model: str,
    spec_names: tuple[str, ...],
) -> None:
    """Make each spec's queries and judgements anew with models, then write the new gate scores.

    The query author writes the queries, the independent assessor judges them without the author's answers,
    the adjudicator settles the queries the two share no answer for,
    and the author rewords any paraphrase or indirect query that repeats its answer.
    It runs models, which need the providers' API keys in their usual variables.
    """
    manifest = dataset.load_manifest(benchmark_directory)
    pipeline = AnnotationPipeline(
        manifest,
        benchmark_directory,
        author_model=author_model,
        assessor_model=assessor_model,
        adjudicator_model=adjudicator_model,
    )
    annotated = asyncio.run(pipeline.run(work_directory, spec_names))
    for spec_annotation in annotated:
        for run in spec_annotation.runs:
            click.echo(
                f'{run.spec} {run.role}: {run.model}, {run.input_tokens} input and {run.output_tokens} output tokens'
            )
    # Every spec is built before any is written, so a spec that cannot be built leaves the data as it was.
    built = {
        spec_annotation.spec: merge.build_spec_dataset(benchmark_directory, spec_annotation.spec)
        for spec_annotation in annotated
    }
    for spec, (spec_dataset, agreement) in built.items():
        dataset.write_spec_dataset(benchmark_directory, spec_dataset)
        click.echo(
            f'{spec}: {agreement.query_count} queries, shared answer on {agreement.shared_answer_count}, '
            f'kappa {agreement.kappa:.3f}, {agreement.adjudicated_count} adjudicated'
        )
    benchmark = dataset.load(benchmark_directory)
    _refuse_contradicted_styles(benchmark)
    _save_gate_scores(benchmark, _gate_scores(benchmark))


def _refuse_contradicted_styles(benchmark: dataset.Benchmark) -> None:
    """Print each style's mean overlap, then exit 1 on any paraphrase or indirect query that repeats its answer."""
    style_report = overlap.check_styles(benchmark)
    for style, mean in style_report.mean_overlaps().items():
        click.echo(f'{style}: mean overlap {mean:.2f} over {len(style_report.overlaps_by_style[style])} queries')
    if style_report.flagged_queries:
        click.echo(f'\nQueries that repeat more than {overlap.MAX_NON_LITERAL_OVERLAP:.0%} of their answer:', err=True)
        for flagged in style_report.flagged_queries:
            query = flagged.query
            click.echo(f'{query.id} ({query.style}, {flagged.overlap:.2f}): {query.text}', err=True)
        sys.exit(1)


def _gate_scores(benchmark: dataset.Benchmark) -> GateScores:
    gate = benchmark.manifest.gate
    variants = runner.select_variants(benchmark, [gate.variant])
    results = asyncio.run(runner.run(benchmark, gate.split, variants))
    return GateScores.from_report(report.build_report(benchmark, gate.split, results), gate)


def _save_gate_scores(benchmark: dataset.Benchmark, scores: GateScores) -> None:
    scores.save(benchmark.gate_scores_path)
    click.echo(f'\nWrote {benchmark.gate_scores_path}')
