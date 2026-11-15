import asyncio
import json
import logging
import pathlib
import sys
import typing

import click

from .search import annotations, dataset, overlap, render, report, runner
from .search.baseline import Baseline
from .search.systems import SystemConfigError
from .target import operation_summaries


BENCHMARK_DIR = click.Path(exists=True, file_okay=False, path_type=pathlib.Path)

_T = typing.TypeVar('_T')


@click.group()
@click.option('-v', '--verbose', is_flag=True, help='Log progress, including each spec and system as it runs.')
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
@click.argument('benchmark_dir', type=BENCHMARK_DIR)
def search_build(benchmark_dir: pathlib.Path) -> None:
    """Merge the annotations in BENCHMARK_DIR into its queries and qrels, and report the annotators' agreement."""
    manifest = _guard(lambda: dataset.load_manifest(benchmark_dir))
    for spec in manifest.specs:
        spec_set, agreement = _guard(lambda spec=spec: annotations.build_spec(benchmark_dir, spec))
        dataset.write_spec_set(benchmark_dir, spec_set)
        click.echo(
            f'{spec}: {agreement.queries} queries, shared answer on {agreement.shared_answer}, '
            f'kappa {agreement.kappa:.3f}, {agreement.adjudicated} adjudicated'
        )


@search.command('validate')
@click.argument('benchmark_dir', type=BENCHMARK_DIR)
def search_validate(benchmark_dir: pathlib.Path) -> None:
    """Check BENCHMARK_DIR's files, and that no paraphrase or indirect query repeats its answer's words."""
    benchmark = _guard(lambda: dataset.load(benchmark_dir))
    summaries = {spec: operation_summaries(runner.corpus_server(benchmark, spec)) for spec in benchmark.specs}
    styles = overlap.check_styles(benchmark, summaries)
    for style, mean in styles.mean_overlaps().items():
        click.echo(f'{style}: mean overlap {mean:.2f} over {len(styles.overlaps[style])}')
    if styles.flagged:
        click.echo(
            f'\n{len(styles.flagged)} quer(ies) repeat more than {overlap.MAX_NON_LITERAL_OVERLAP:.0%} of their answer:'
        )
        for query, share in styles.flagged:
            click.echo(f'{query.id} ({query.style}, {share:.2f}): {query.text}')
        sys.exit(1)
    click.echo('All queries are consistent.')


@search.command('run')
@click.argument('benchmark_dir', type=BENCHMARK_DIR)
@click.option('--split', type=click.Choice(dataset.SPLITS), default='test', show_default=True)
@click.option('--system', 'systems', multiple=True, help='A system to run. Repeat for several; the default is all.')
@click.option(
    '--output', type=click.Path(dir_okay=False, path_type=pathlib.Path), help='Write the full report as JSON.'
)
def search_run(
    benchmark_dir: pathlib.Path, split: dataset.Split, systems: tuple[str, ...], output: pathlib.Path | None
) -> None:
    """Rank BENCHMARK_DIR's queries under each system and print the tables."""
    benchmark = _guard(lambda: dataset.load(benchmark_dir))
    chosen = _guard(lambda: runner.build_systems(benchmark, systems))
    results = _guard(lambda: asyncio.run(runner.run(benchmark, split, chosen)))
    built = report.build_report(benchmark, split, results)
    if output is not None:
        output.write_text(json.dumps(built, indent=2) + '\n', encoding='utf-8')
    click.echo(render.to_markdown(built), nl=False)


@search.command('check')
@click.argument('benchmark_dir', type=BENCHMARK_DIR)
@click.option('--update', is_flag=True, help='Write this run as the new baseline instead of comparing against it.')
def search_check(benchmark_dir: pathlib.Path, update: bool) -> None:
    """Fail when the gated system scores below BENCHMARK_DIR's baseline, as CI runs it."""
    benchmark = _guard(lambda: dataset.load(benchmark_dir))
    gate = benchmark.manifest.gate
    chosen = _guard(lambda: runner.build_systems(benchmark, [gate.system]))
    results = _guard(lambda: asyncio.run(runner.run(benchmark, gate.split, chosen)))
    current = Baseline.from_report(report.build_report(benchmark, gate.split, results), benchmark)
    if update:
        current.save(benchmark.baseline_path)
        click.echo(f'Wrote {benchmark.baseline_path}')
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


def _guard(step: typing.Callable[[], _T]) -> _T:
    """Run ``step``, turning a benchmark's own errors into a command-line error without a traceback."""
    try:
        return step()
    except (dataset.DatasetError, SystemConfigError) as error:
        raise click.ClickException(str(error)) from error
