import json
import pathlib
import shutil

import pytest
from click.testing import CliRunner

from openapi_mcp_gateway.evals import corpus
from openapi_mcp_gateway.evals.cli import main
from openapi_mcp_gateway.evals.search import build, dataset, metrics, overlap, report, runner
from openapi_mcp_gateway.evals.stats import macro_mean, paired_bootstrap
from tests.conftest import FIXTURES_DIR


_PETSTORE = FIXTURES_DIR / 'petstore.json'


class TestMetrics:
    """Ranking metrics over graded judgements."""

    def test_ndcg_is_one_for_the_ideal_order(self):
        assert metrics.ndcg(['a', 'b', 'c'], {'a': 2, 'b': 1}, 10) == 1.0

    def test_ndcg_discounts_a_late_answer(self):
        """An answer at rank 2 of an otherwise empty ranking scores 1 / log2(3)."""
        assert metrics.ndcg(['x', 'a'], {'a': 2}, 10) == pytest.approx(1 / 1.584962500721156)

    def test_ndcg_ignores_ranks_past_the_cutoff(self):
        assert metrics.ndcg(['x', 'a'], {'a': 2}, 1) == 0.0

    def test_mrr_and_recall_count_only_the_answer(self):
        """A lesser operation ranked first is not a hit, the answer behind it is."""
        ranking, judgements = ['lesser', 'answer'], {'lesser': 1, 'answer': 2}
        assert metrics.reciprocal_rank(ranking, judgements, 10) == 0.5
        assert metrics.recall(ranking, judgements, 1) == 0.0
        assert metrics.recall(ranking, judgements, 2) == 1.0

    def test_recall_is_the_share_of_answers_found(self):
        assert metrics.recall(['a', 'x'], {'a': 2, 'b': 2}, 20) == 0.5


class TestStats:
    """Macro means and the paired bootstrap."""

    def test_macro_mean_weighs_every_stratum_alike(self):
        assert macro_mean({'big': [1.0, 1.0, 1.0, 1.0], 'small': [0.0]}) == 0.5

    def test_identical_systems_are_not_significantly_different(self):
        difference = paired_bootstrap({'a': [0.0] * 20, 'b': [0.0] * 20}, resamples=200)
        assert (difference.mean, difference.low, difference.high) == (0.0, 0.0, 0.0)
        assert not difference.significant

    def test_a_consistent_gain_is_significant(self):
        difference = paired_bootstrap({'a': [0.2, 0.3, 0.25, 0.1] * 5}, resamples=500)
        assert difference.significant
        assert difference.low <= difference.mean <= difference.high

    def test_the_interval_is_reproducible(self):
        values = {'a': [0.5, -0.2, 0.1, 0.0, 0.3]}
        assert paired_bootstrap(values, resamples=300) == paired_bootstrap(values, resamples=300)

    def test_an_empty_stratum_is_refused(self):
        with pytest.raises(ValueError):
            paired_bootstrap({'a': []})


class TestOverlap:
    """How much of an answer's wording a query repeats."""

    def test_a_literal_query_repeats_the_summary(self):
        assert overlap.overlap('merge a pull request', 'pulls_merge Merge a pull request') == 1.0

    def test_a_paraphrase_repeats_nothing(self):
        assert overlap.overlap('combine this change into main', 'pulls_merge Merge a pull request') == 0.0

    def test_the_best_answer_counts(self):
        summaries = {'list_pets': 'List all pets', 'get_pet_by_id': 'Info for a specific pet'}
        assert overlap.best_overlap('list pets', ['get_pet_by_id', 'list_pets'], summaries) == 1.0


class TestCorpus:
    """Pinned specs are checked on every use."""

    def test_a_local_pin_resolves_against_its_directory(self):
        pin = corpus.SpecPin(source=_PETSTORE.name, sha256=corpus.sha256_of(_PETSTORE))
        assert corpus.fetch(pin, FIXTURES_DIR) == _PETSTORE.resolve()

    def test_a_changed_spec_is_refused(self):
        pin = corpus.SpecPin(source=_PETSTORE.name, sha256='0' * 64)
        with pytest.raises(corpus.SpecChecksumError):
            corpus.fetch(pin, FIXTURES_DIR)

    def test_a_cached_download_is_reused(self, tmp_path, monkeypatch):
        """A remote pin already in the cache needs no network."""
        monkeypatch.setenv('OPENAPI_MCP_GATEWAY_CACHE', str(tmp_path))
        digest = corpus.sha256_of(_PETSTORE)
        cached = tmp_path / 'specs' / f'{digest}.json'
        cached.parent.mkdir()
        shutil.copy(_PETSTORE, cached)
        pin = corpus.SpecPin(source='https://unreachable.invalid/spec.json', sha256=digest)
        assert corpus.fetch(pin, tmp_path) == cached


class TestBuild:
    """Merging two annotators into qrels."""

    def test_an_answer_needs_both_annotators(self):
        """Grade 2 only where both say 2, and anything either judges relevant is at least 1."""
        merged = build._merge({'a': 2, 'b': 2, 'c': 1}, {'a': 2, 'b': 1, 'd': 1})
        assert merged == {'a': 2, 'b': 1, 'c': 1, 'd': 1}

    def test_kappa(self):
        assert build.cohens_kappa([(2, 2), (1, 1), (0, 0)]) == 1.0
        assert build.cohens_kappa([(2, 2), (2, 1), (1, 2), (1, 1)]) == 0.0

    def test_splits_are_stratified_and_stable(self):
        queries = [dataset.Query(id=f'q{i:02d}', text='t', style='literal') for i in range(15)]
        first = build.assign_splits(queries)
        assert first == build.assign_splits(reversed(queries))
        assert sum(split == 'dev' for split in first.values()) == build.DEV_PER_STYLE

    def test_a_query_without_a_shared_answer_needs_an_adjudication(self, tmp_path):
        directory = tmp_path / build.ANNOTATIONS / 'pets'
        directory.mkdir(parents=True)
        _write_jsonl(
            directory / 'annotator_a.jsonl', [{'id': 'q1', 'text': 't', 'style': 'literal', 'relevant': {'a': 2}}]
        )
        _write_jsonl(directory / 'annotator_b.jsonl', [{'id': 'q1', 'relevant': {'b': 2}}])
        with pytest.raises(dataset.DatasetError, match='adjudication'):
            build.build_spec(tmp_path, 'pets')
        _write_jsonl(directory / 'adjudication.jsonl', [{'id': 'q1', 'relevant': {'a': 2, 'b': 2}, 'note': ''}])
        spec_set, agreement = build.build_spec(tmp_path, 'pets')
        assert spec_set.qrels['dev'] == {'q1': {'a': 2, 'b': 2}}
        assert (agreement.shared_answer, agreement.adjudicated) == (0, 1)


class TestDataset:
    """Loading a benchmark and refusing an inconsistent one."""

    def test_round_trip(self, tmp_path):
        benchmark_dir = _petstore_benchmark(tmp_path)
        loaded = dataset.load(benchmark_dir)
        assert set(loaded.specs['pets'].qrels['test']) == {'p2', 'p3'}
        assert loaded.specs['pets'].queries['p1'].style == 'literal'

    def test_a_query_judged_in_both_splits_is_refused(self, tmp_path):
        benchmark_dir = _petstore_benchmark(tmp_path)
        with (benchmark_dir / 'pets' / 'qrels' / 'test.tsv').open('a') as file:
            file.write('p1\tlist_pets\t2\n')
        with pytest.raises(dataset.DatasetError, match='both dev and test'):
            dataset.load(benchmark_dir)

    def test_a_query_without_an_answer_is_refused(self, tmp_path):
        benchmark_dir = _petstore_benchmark(tmp_path)
        (benchmark_dir / 'pets' / 'qrels' / 'dev.tsv').write_text('query-id\tcorpus-id\tscore\np1\tlist_pets\t1\n')
        with pytest.raises(dataset.DatasetError, match='no operation graded 2'):
            dataset.load(benchmark_dir)

    def test_an_unknown_style_is_refused(self, tmp_path):
        benchmark_dir = _petstore_benchmark(tmp_path)
        queries = benchmark_dir / 'pets' / 'queries.jsonl'
        queries.write_text(queries.read_text().replace('"literal"', '"poetic"'))
        with pytest.raises(dataset.DatasetError, match='style'):
            dataset.load(benchmark_dir)

    def test_the_gate_must_name_a_variant(self, tmp_path):
        benchmark_dir = _petstore_benchmark(tmp_path)
        manifest = benchmark_dir / dataset.MANIFEST
        manifest.write_text(manifest.read_text().replace('variant: default', 'variant: missing'))
        with pytest.raises(dataset.DatasetError, match=r'gate\.variant'):
            dataset.load(benchmark_dir)


class TestRun:
    """Running a benchmark against a gateway built in-process."""

    async def test_scores_every_query_under_each_variant_and_the_floor(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        results = await runner.run(benchmark, 'test')
        assert set(results) == {'default', 'names-only', runner.RANDOM}
        by_id = {result.query_id: result for result in results['default']}
        assert by_id['p2'].ranking[0] == 'create_pet'
        assert by_id['p2'].scores['recall@1'] == 1.0

    async def test_a_judged_operation_the_gateway_does_not_serve_is_refused(self, tmp_path):
        benchmark_dir = _petstore_benchmark(tmp_path)
        (benchmark_dir / 'pets' / 'qrels' / 'test.tsv').write_text(
            'query-id\tcorpus-id\tscore\np2\tcreate_pet\t2\np3\tadopt_pet\t2\n'
        )
        with pytest.raises(dataset.DatasetError, match='adopt_pet'):
            await runner.run(dataset.load(benchmark_dir), 'test')

    async def test_report_and_regressions(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        built = report.build_report(benchmark, 'test', await runner.run(benchmark, 'test'))
        assert {row['against'] for row in built['comparisons']} == {'names-only', runner.RANDOM}
        assert '| default |' in report.to_markdown(built)

        baseline = report.baseline_from(built, benchmark)
        assert report.regressions(built, baseline, 0.01) == []
        raised = {**baseline, 'macro': {metric: value + 0.5 for metric, value in baseline['macro'].items()}}
        assert len(report.regressions(built, raised, 0.01)) == 2


class TestCli:
    """The commands CI and a developer run."""

    def test_check_passes_against_its_own_baseline_and_fails_below_it(self, tmp_path):
        benchmark_dir = _petstore_benchmark(tmp_path)
        cli = CliRunner()
        assert cli.invoke(main, ['search', 'check', str(benchmark_dir), '--update']).exit_code == 0
        assert cli.invoke(main, ['search', 'check', str(benchmark_dir)]).exit_code == 0

        baseline_path = benchmark_dir / dataset.BASELINE
        baseline = json.loads(baseline_path.read_text())
        baseline['macro']['ndcg@10'] = 2.0
        baseline_path.write_text(json.dumps(baseline))
        failed = cli.invoke(main, ['search', 'check', str(benchmark_dir)])
        assert failed.exit_code == 1
        assert 'macro ndcg@10' in failed.output

    def test_run_writes_a_report(self, tmp_path):
        benchmark_dir = _petstore_benchmark(tmp_path)
        output = tmp_path / 'report.json'
        result = CliRunner().invoke(main, ['search', 'run', str(benchmark_dir), '--output', str(output)])
        assert result.exit_code == 0, result.output
        assert json.loads(output.read_text())['benchmark'] == {'version': 1, 'split': 'test'}

    def test_validate_flags_a_paraphrase_that_repeats_its_answer(self, tmp_path):
        benchmark_dir = _petstore_benchmark(tmp_path)
        queries = benchmark_dir / 'pets' / 'queries.jsonl'
        queries.write_text(queries.read_text().replace('"literal"}}\n{"_id": "p2"', '"paraphrase"}}\n{"_id": "p2"'))
        result = CliRunner().invoke(main, ['search', 'validate', str(benchmark_dir)])
        assert result.exit_code == 1
        assert 'p1 (paraphrase' in result.output


def _petstore_benchmark(root: pathlib.Path) -> pathlib.Path:
    """A three-query benchmark over the petstore fixture, pinned as a local file, with one dev and two test queries."""
    benchmark_dir = root / 'benchmark'
    benchmark_dir.mkdir()
    shutil.copy(_PETSTORE, benchmark_dir / 'petstore.json')
    (benchmark_dir / dataset.MANIFEST).write_text(
        'version: 1\n'
        'specs:\n'
        f'  pets: {{source: petstore.json, sha256: {corpus.sha256_of(_PETSTORE)}}}\n'
        'variants:\n'
        '  default: {exposure: {style: dynamic}}\n'
        '  names-only:\n'
        '    exposure: {style: dynamic, search: {field_weights: {name: 1, summary: 0, description: 0, path: 0, tags: 0}}}\n'
        'gate:\n'
        '  variant: default\n'
    )
    spec_set = dataset.SpecSet(
        name='pets',
        queries={
            'p1': dataset.Query(id='p1', text='list pets', style='literal'),
            'p2': dataset.Query(id='p2', text='create a pet', style='literal'),
            'p3': dataset.Query(id='p3', text='I no longer own this animal', style='indirect'),
        },
        qrels={
            'dev': {'p1': {'list_pets': 2}},
            'test': {'p2': {'create_pet': 2, 'create_pet_with_owner': 1}, 'p3': {'delete_pet': 2}},
        },
    )
    dataset.write_spec_set(benchmark_dir, spec_set)
    return benchmark_dir


def _write_jsonl(path: pathlib.Path, records: list[dict]) -> None:
    path.write_text(''.join(json.dumps(record) + '\n' for record in records))
