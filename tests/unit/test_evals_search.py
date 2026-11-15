import contextlib
import json
import pathlib
import shutil
import typing

import pytest
from click.testing import CliRunner

from openapi_mcp_gateway.evals import corpus
from openapi_mcp_gateway.evals.cli import main
from openapi_mcp_gateway.evals.search import annotations, dataset, overlap, render, report, runner
from openapi_mcp_gateway.evals.search.baseline import Baseline
from openapi_mcp_gateway.evals.search.metrics import NDCG, Recall, ReciprocalRank
from openapi_mcp_gateway.evals.search.systems import (
    GatewaySystem,
    RandomSystem,
    Retriever,
    SpecCorpus,
    System,
    SystemConfigError,
)
from openapi_mcp_gateway.evals.stats import macro_mean, paired_bootstrap
from tests.conftest import FIXTURES_DIR


PETSTORE = FIXTURES_DIR / 'petstore.json'

MANIFEST = """\
version: 1
specs:
  pets: {{source: petstore.json, sha256: {sha256}}}
systems:
  default:
    gateway: {{exposure: {{style: dynamic}}}}
  names-only:
    gateway:
      exposure: {{style: dynamic, search: {{field_weights: {{name: 1, summary: 0, description: 0, path: 0, tags: 0}}}}}}
  random:
    random: {{}}
gate:
  system: default
"""


class ReversedNames(System):
    """A system a test defines on its own, to show a new kind needs no change elsewhere."""

    kind = 'test-reversed-names'

    @classmethod
    def configure(cls, name: str, config: typing.Mapping[str, typing.Any]) -> typing.Self:
        return cls(name)

    @contextlib.asynccontextmanager
    async def open(self, corpus: SpecCorpus) -> typing.AsyncIterator[Retriever]:
        yield _Fixed(sorted(corpus.operations, reverse=True))


class _Fixed:
    def __init__(self, ranking: list[str]):
        self._ranking = ranking

    async def rank(self, query: str, depth: int) -> list[str]:
        return self._ranking[:depth]


class TestMetrics:
    """Ranking metrics over graded judgements."""

    def test_names_carry_the_cutoff(self):
        assert [NDCG(10).name, Recall(20).name, ReciprocalRank(10).name] == ['ndcg@10', 'recall@20', 'mrr@10']

    def test_ndcg_is_one_for_the_ideal_order(self):
        assert NDCG(10).score(['a', 'b', 'c'], {'a': 2, 'b': 1}) == 1.0

    def test_ndcg_discounts_a_late_answer(self):
        """An answer at rank 2 of an otherwise empty ranking scores 1 / log2(3)."""
        assert NDCG(10).score(['x', 'a'], {'a': 2}) == pytest.approx(1 / 1.584962500721156)

    def test_ndcg_ignores_ranks_past_the_cutoff(self):
        assert NDCG(1).score(['x', 'a'], {'a': 2}) == 0.0

    def test_mrr_and_recall_count_only_the_answer(self):
        """A lesser operation ranked first is not a hit, the answer behind it is."""
        ranking, judgements = ['lesser', 'answer'], {'lesser': 1, 'answer': 2}
        assert ReciprocalRank(10).score(ranking, judgements) == 0.5
        assert Recall(1).score(ranking, judgements) == 0.0
        assert Recall(2).score(ranking, judgements) == 1.0

    def test_recall_is_the_share_of_answers_found(self):
        assert Recall(20).score(['a', 'x'], {'a': 2, 'b': 2}) == 0.5


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
        pin = corpus.SpecPin(source=PETSTORE.name, sha256=corpus.sha256_of(PETSTORE))
        assert corpus.fetch(pin, FIXTURES_DIR) == PETSTORE.resolve()

    def test_a_changed_spec_is_refused(self):
        pin = corpus.SpecPin(source=PETSTORE.name, sha256='0' * 64)
        with pytest.raises(corpus.SpecChecksumError):
            corpus.fetch(pin, FIXTURES_DIR)

    def test_a_cached_download_is_reused(self, tmp_path, monkeypatch):
        """A remote pin already in the cache needs no network."""
        monkeypatch.setenv('OPENAPI_MCP_GATEWAY_CACHE', str(tmp_path))
        digest = corpus.sha256_of(PETSTORE)
        cached = tmp_path / 'specs' / f'{digest}.json'
        cached.parent.mkdir()
        shutil.copy(PETSTORE, cached)
        pin = corpus.SpecPin(source='https://unreachable.invalid/spec.json', sha256=digest)
        assert corpus.fetch(pin, tmp_path) == cached


class TestAnnotations:
    """Merging two annotators into qrels."""

    def test_an_answer_needs_both_annotators(self):
        """Grade 2 only where both say 2, and anything either judges relevant is at least 1."""
        annotation = _annotation('q1', {'a': 2, 'b': 2, 'c': 1}, {'a': 2, 'b': 1, 'd': 1})
        assert annotation.judgements() == {'a': 2, 'b': 1, 'c': 1, 'd': 1}

    def test_an_adjudication_replaces_both_annotators(self):
        annotation = _annotation('q1', {'a': 2}, {'b': 2}, adjudication={'relevant': {'a': 2, 'b': 2}, 'note': ''})
        assert annotation.judgements() == {'a': 2, 'b': 2}

    def test_a_query_without_a_shared_answer_needs_an_adjudication(self):
        with pytest.raises(dataset.DatasetError, match='adjudication'):
            _annotation('q1', {'a': 2}, {'b': 2}).judgements()

    def test_kappa(self):
        assert annotations.cohens_kappa([(2, 2), (1, 1), (0, 0)]) == 1.0
        assert annotations.cohens_kappa([(2, 2), (2, 1), (1, 2), (1, 1)]) == 0.0

    def test_splits_are_stratified_and_stable(self):
        queries = [_annotation(f'q{i:02d}', {'a': 2}, {'a': 2}) for i in range(15)]
        first = annotations.assign_splits(queries)
        assert first == annotations.assign_splits(reversed(queries))
        assert sum(split == 'dev' for split in first.values()) == annotations.DEV_PER_STYLE

    def test_build_reports_the_agreement(self, tmp_path):
        path = tmp_path / annotations.ANNOTATIONS / 'pets.jsonl'
        path.parent.mkdir()
        records = [
            _annotation('q1', {'a': 2}, {'a': 2}),
            _annotation('q2', {'a': 2}, {'b': 2}, adjudication={'relevant': {'b': 2}, 'note': 'b fits'}),
        ]
        path.write_text(''.join(record.model_dump_json(exclude_none=True) + '\n' for record in records))
        spec_set, agreement = annotations.build_spec(tmp_path, 'pets')
        assert spec_set.qrels['dev'] == {'q1': {'a': 2}, 'q2': {'b': 2}}
        assert (agreement.queries, agreement.shared_answer, agreement.adjudicated) == (2, 1, 1)


class TestDataset:
    """Loading a benchmark and refusing an inconsistent one."""

    def test_round_trip(self, tmp_path):
        loaded = dataset.load(_petstore_benchmark(tmp_path))
        assert set(loaded.specs['pets'].qrels['test']) == {'p2', 'p3'}
        assert loaded.specs['pets'].queries['p1'].style == 'literal'
        assert loaded.manifest.systems['random'].kind == 'random'

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

    def test_the_gate_must_name_a_system(self, tmp_path):
        benchmark_dir = _petstore_benchmark(tmp_path)
        manifest = benchmark_dir / dataset.MANIFEST
        manifest.write_text(manifest.read_text().replace('system: default', 'system: missing'))
        with pytest.raises(dataset.DatasetError, match=r'gate\.system'):
            dataset.load(benchmark_dir)

    def test_a_system_is_one_key(self, tmp_path):
        benchmark_dir = _petstore_benchmark(tmp_path)
        manifest = benchmark_dir / dataset.MANIFEST
        manifest.write_text(manifest.read_text().replace('    random: {}', '    random: {}\n    gateway: {}'))
        with pytest.raises(dataset.DatasetError, match='one key'):
            dataset.load(benchmark_dir)


class TestSystems:
    """Systems are built from the manifest by kind."""

    def test_kinds_pick_their_classes(self):
        assert isinstance(System.from_spec('a', 'gateway', {}), GatewaySystem)
        assert isinstance(System.from_spec('b', 'random', {}), RandomSystem)
        assert isinstance(System.from_spec('c', ReversedNames.kind, {}), ReversedNames)

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(SystemConfigError, match='unknown kind'):
            System.from_spec('a', 'nonsense', {})

    def test_a_gateway_system_cannot_set_the_spec(self):
        with pytest.raises(SystemConfigError, match='name or spec'):
            System.from_spec('a', 'gateway', {'spec': 'other.json'})

    def test_a_kind_cannot_be_taken_twice(self):
        with pytest.raises(TypeError, match='already taken'):
            type('Duplicate', (RandomSystem,), {'kind': 'random'})


class TestRun:
    """Running a benchmark against a gateway built in-process."""

    async def test_scores_every_query_under_each_system(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        results = await runner.run(benchmark, 'test', runner.build_systems(benchmark))
        assert set(results) == {'default', 'names-only', 'random'}
        by_id = {result.query_id: result for result in results['default']}
        assert by_id['p2'].ranking[0] == 'create_pet'
        assert by_id['p2'].scores['recall@1'] == 1.0

    async def test_a_new_kind_of_system_runs_without_changes_elsewhere(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        results = await runner.run(benchmark, 'test', [ReversedNames('reversed')])
        assert [len(result.ranking) for result in results['reversed']] == [6, 6]

    async def test_a_judged_operation_the_spec_does_not_serve_is_refused(self, tmp_path):
        benchmark_dir = _petstore_benchmark(tmp_path)
        (benchmark_dir / 'pets' / 'qrels' / 'test.tsv').write_text(
            'query-id\tcorpus-id\tscore\np2\tcreate_pet\t2\np3\tadopt_pet\t2\n'
        )
        benchmark = dataset.load(benchmark_dir)
        with pytest.raises(dataset.DatasetError, match='adopt_pet'):
            await runner.run(benchmark, 'test', runner.build_systems(benchmark))

    def test_an_unknown_system_name_is_refused(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        with pytest.raises(dataset.DatasetError, match='missing'):
            runner.build_systems(benchmark, ['missing'])

    async def test_report_baseline_and_regressions(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        built = report.build_report(
            benchmark, 'test', await runner.run(benchmark, 'test', runner.build_systems(benchmark))
        )
        assert {row['against'] for row in built['comparisons']} == {'names-only', 'random'}
        assert '| default |' in render.to_markdown(built)

        baseline = Baseline.from_report(built, benchmark)
        assert baseline.regressions(baseline, 0.01) == []
        raised = baseline.model_copy(
            update={'macro': {metric: value + 0.5 for metric, value in baseline.macro.items()}}
        )
        assert len(raised.regressions(baseline, 0.01)) == 2


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

    def test_an_unknown_system_is_a_clean_error(self, tmp_path):
        result = CliRunner().invoke(main, ['search', 'run', str(_petstore_benchmark(tmp_path)), '--system', 'missing'])
        assert result.exit_code == 1
        assert 'Unknown system' in result.output

    def test_validate_flags_a_paraphrase_that_repeats_its_answer(self, tmp_path):
        benchmark_dir = _petstore_benchmark(tmp_path)
        queries = benchmark_dir / 'pets' / 'queries.jsonl'
        queries.write_text(queries.read_text().replace('"literal"}}\n{"_id": "p2"', '"paraphrase"}}\n{"_id": "p2"'))
        result = CliRunner().invoke(main, ['search', 'validate', str(benchmark_dir)])
        assert result.exit_code == 1
        assert 'p1 (paraphrase' in result.output


def _annotation(
    query_id: str, a: dict[str, int], b: dict[str, int], adjudication: dict[str, typing.Any] | None = None
) -> annotations.Annotation:
    return annotations.Annotation.model_validate(
        {
            'id': query_id,
            'text': 't',
            'style': 'literal',
            'annotator_a': a,
            'annotator_b': b,
            'adjudication': adjudication,
        }
    )


def _petstore_benchmark(root: pathlib.Path) -> pathlib.Path:
    """A three-query benchmark over the petstore fixture, pinned as a local file, with one dev and two test queries."""
    benchmark_dir = root / 'benchmark'
    benchmark_dir.mkdir()
    shutil.copy(PETSTORE, benchmark_dir / 'petstore.json')
    (benchmark_dir / dataset.MANIFEST).write_text(MANIFEST.format(sha256=corpus.sha256_of(PETSTORE)))
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
