"""The benchmark commands, run today as ``python -m openapi_mcp_gateway.evals``.

Each benchmark is a command group taking the benchmark directory as its argument,
so the same commands run the repository's own data and, later, a user's.
"""

import asyncio
import collections
import json
import logging
import pathlib
import statistics
import sys

import click

from .search import build as search_build
from .search import dataset as search_dataset
from .search import overlap as search_overlap
from .search import report as search_report
from .search import runner as search_runner
from .target import operation_summaries


_BENCHMARK_DIR = click.Path(exists=True, file_okay=False, path_type=pathlib.Path)


@click.group()
@click.option('-v', '--verbose', is_flag=True, help='Log progress, including each spec and variant as it runs.')
def main(verbose: bool) -> None:
    """Benchmarks of what the gateway does for a model."""
    # The gateway logs every registration at INFO, which buries a benchmark's own output.
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
    if verbose:
        logging.getLogger('openapi_mcp_gateway.evals').setLevel(logging.INFO)


@main.group()
def search() -> None:
    """How well search_operations ranks the operation a request asks for."""


@search.command('build')
@click.argument('benchmark_dir', type=_BENCHMARK_DIR)
def search_build_command(benchmark_dir: pathlib.Path) -> None:
    """Merge the annotations in BENCHMARK_DIR into its queries and qrels, and report the annotators' agreement."""
    manifest = search_dataset.load_manifest(benchmark_dir)
    rows = []
    for spec in manifest.specs:
        spec_set, agreement = search_build.build_spec(benchmark_dir, spec)
        search_dataset.write_spec_set(benchmark_dir, spec_set)
        rows.append(
            f'{spec}: {agreement.queries} queries, shared answer on {agreement.shared_answer}, '
            f'kappa {agreement.kappa:.3f}, {agreement.adjudicated} adjudicated'
        )
    click.echo('\n'.join(rows))


@search.command('validate')
@click.argument('benchmark_dir', type=_BENCHMARK_DIR)
def search_validate_command(benchmark_dir: pathlib.Path) -> None:
    """Check BENCHMARK_DIR's files, and that no paraphrase or indirect query repeats its answer's words."""
    benchmark = _load(benchmark_dir)
    variant = benchmark.manifest.gate.variant
    flagged: list[str] = []
    overlaps: dict[str, list[float]] = collections.defaultdict(list)
    for spec, spec_set in benchmark.specs.items():
        summaries = operation_summaries(search_runner.server_config(benchmark, spec, variant))
        for split in search_dataset.SPLITS:
            for query, judgements in spec_set.split(split):
                answers = [name for name, grade in judgements.items() if grade == search_dataset.ANSWER_GRADE]
                share = search_overlap.best_overlap(query.text, answers, summaries)
                overlaps[query.style].append(share)
                if query.style != 'literal' and share > search_overlap.MAX_NON_LITERAL_OVERLAP:
                    flagged.append(f'{query.id} ({query.style}, {share:.2f}): {query.text}')
    for style in search_dataset.STYLES:
        if overlaps[style]:
            click.echo(f'{style}: mean overlap {statistics.fmean(overlaps[style]):.2f} over {len(overlaps[style])}')
    if flagged:
        click.echo(
            f'\n{len(flagged)} quer(ies) repeat more than {search_overlap.MAX_NON_LITERAL_OVERLAP:.0%} of their answer:'
        )
        click.echo('\n'.join(flagged))
        sys.exit(1)
    click.echo('All queries are consistent.')


@search.command('run')
@click.argument('benchmark_dir', type=_BENCHMARK_DIR)
@click.option('--split', type=click.Choice(search_dataset.SPLITS), default='test', show_default=True)
@click.option('--variant', 'variants', multiple=True, help='A variant to run. Repeat for several; default is all.')
@click.option(
    '--output', type=click.Path(dir_okay=False, path_type=pathlib.Path), help='Write the full report as JSON.'
)
def search_run_command(
    benchmark_dir: pathlib.Path, split: search_dataset.Split, variants: tuple[str, ...], output: pathlib.Path | None
) -> None:
    """Rank BENCHMARK_DIR's queries under each variant and print the tables."""
    benchmark = _load(benchmark_dir)
    results = asyncio.run(search_runner.run(benchmark, split, variants or None))
    report = search_report.build_report(benchmark, split, results)
    if output is not None:
        output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    click.echo(search_report.to_markdown(report), nl=False)


@search.command('check')
@click.argument('benchmark_dir', type=_BENCHMARK_DIR)
@click.option('--update', is_flag=True, help='Write this run as the new baseline instead of comparing against it.')
def search_check_command(benchmark_dir: pathlib.Path, update: bool) -> None:
    """Fail when the gated variant scores below BENCHMARK_DIR's baseline, as CI runs it."""
    benchmark = _load(benchmark_dir)
    gate = benchmark.manifest.gate
    results = asyncio.run(search_runner.run(benchmark, gate.split, [gate.variant], include_random=False))
    report = search_report.build_report(benchmark, gate.split, results)
    current = search_report.baseline_from(report, benchmark)
    if update:
        benchmark.baseline_path.write_text(json.dumps(current, indent=2) + '\n', encoding='utf-8')
        click.echo(f'Wrote {benchmark.baseline_path}')
        return
    if not benchmark.baseline_path.exists():
        raise click.ClickException(f'{benchmark.baseline_path} not found; run with --update to create it')
    baseline = json.loads(benchmark.baseline_path.read_text(encoding='utf-8'))
    failures = search_report.regressions(report, baseline, gate.tolerance)
    click.echo(json.dumps({'macro': current['macro'], 'specs': current['specs']}, indent=2))
    if failures:
        click.echo(f'\nBelow the baseline by more than {gate.tolerance}:', err=True)
        click.echo('\n'.join(failures), err=True)
        sys.exit(1)
    click.echo(f'\nWithin {gate.tolerance} of the baseline.')


def _load(benchmark_dir: pathlib.Path) -> search_dataset.Benchmark:
    try:
        return search_dataset.load(benchmark_dir)
    except search_dataset.DatasetError as error:
        raise click.ClickException(str(error)) from error
