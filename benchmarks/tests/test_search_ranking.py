import contextlib
import json
import pathlib
import shutil
import typing

import pytest
from click.testing import CliRunner
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from openapi_mcp_gateway_benchmarks.cli import main
from openapi_mcp_gateway_benchmarks.search_ranking import (
    annotation_workflow,
    annotations,
    automated_annotation,
    dataset,
    markdown,
    overlap,
    report,
    runner,
)
from openapi_mcp_gateway_benchmarks.search_ranking.baseline import Baseline
from openapi_mcp_gateway_benchmarks.search_ranking.metrics import (
    NormalizedDiscountedCumulativeGain,
    Recall,
    ReciprocalRank,
)
from openapi_mcp_gateway_benchmarks.search_ranking.systems import (
    GatewaySystem,
    PydanticAIKeywordSystem,
    Retriever,
    SpecCorpus,
    System,
)
from openapi_mcp_gateway_benchmarks.significance import macro_mean, paired_bootstrap
from openapi_mcp_gateway_benchmarks.spec_pins import SpecChecksumError, SpecPin, sha256_of


FIXTURES_DIR = pathlib.Path(__file__).parent / 'fixtures'
PETSTORE = FIXTURES_DIR / 'petstore.json'
REPOSITORY_ANNOTATIONS = pathlib.Path(__file__).parents[1] / 'search_ranking' / 'annotations'

MANIFEST = """\
version: 1
specs:
  pets: {{source: petstore.json, sha256: {sha256}}}
systems:
  default:
    type: gateway
    server: {{exposure: {{style: dynamic}}}}
  names-only:
    type: gateway
    server:
      exposure: {{style: dynamic, search: {{field_weights: {{name: 1, summary: 0, description: 0, path: 0, tags: 0}}}}}}
  random:
    type: random
gate:
  system: default
"""


class ReversedNameSystem(System):
    """A system a test defines on its own, to show a new kind needs no change to the runner or the report."""

    type: typing.Literal['reversed-names'] = 'reversed-names'

    @contextlib.asynccontextmanager
    async def open(self, corpus: SpecCorpus) -> typing.AsyncIterator[Retriever]:
        yield _FixedRanking(sorted(corpus.operations, reverse=True))


class _FixedRanking:
    def __init__(self, ranking: list[str]):
        self._ranking = ranking

    async def rank(self, query: str, depth: int) -> list[str]:
        return self._ranking[:depth]


class TestMetrics:
    """Ranking metrics over graded judgements."""

    def test_names_carry_the_cutoff(self):
        names = [NormalizedDiscountedCumulativeGain(10).name, Recall(20).name, ReciprocalRank(10).name]
        assert names == ['ndcg@10', 'recall@20', 'mrr@10']

    def test_ndcg_is_one_for_the_ideal_order(self):
        assert NormalizedDiscountedCumulativeGain(10).score(['a', 'b', 'c'], {'a': 2, 'b': 1}) == 1.0

    def test_ndcg_discounts_a_late_answer(self):
        """An answer at rank 2 of an otherwise empty ranking scores 1 / log2(3)."""
        score = NormalizedDiscountedCumulativeGain(10).score(['x', 'a'], {'a': 2})
        assert score == pytest.approx(1 / 1.584962500721156)

    def test_ndcg_ignores_ranks_past_the_cutoff(self):
        assert NormalizedDiscountedCumulativeGain(1).score(['x', 'a'], {'a': 2}) == 0.0

    def test_mrr_and_recall_count_only_the_answer(self):
        """A lesser operation ranked first is not a hit, the answer behind it is."""
        ranking, judgements = ['lesser', 'answer'], {'lesser': 1, 'answer': 2}
        assert ReciprocalRank(10).score(ranking, judgements) == 0.5
        assert Recall(1).score(ranking, judgements) == 0.0
        assert Recall(2).score(ranking, judgements) == 1.0

    def test_recall_is_the_share_of_answers_found(self):
        assert Recall(20).score(['a', 'x'], {'a': 2, 'b': 2}) == 0.5


class TestSignificance:
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
        differences = {'a': [0.5, -0.2, 0.1, 0.0, 0.3]}
        assert paired_bootstrap(differences, resamples=300) == paired_bootstrap(differences, resamples=300)

    def test_an_empty_stratum_is_refused(self):
        with pytest.raises(ValueError):
            paired_bootstrap({'a': []})


class TestOverlap:
    """How much of an answer's wording a query repeats."""

    def test_a_literal_query_repeats_the_summary(self):
        assert overlap.term_overlap('merge a pull request', 'pulls_merge Merge a pull request') == 1.0

    def test_a_paraphrase_repeats_nothing(self):
        assert overlap.term_overlap('combine this change into main', 'pulls_merge Merge a pull request') == 0.0

    def test_the_best_answer_counts(self):
        summaries = {'list_pets': 'List all pets', 'get_pet_by_id': 'Info for a specific pet'}
        assert overlap.best_term_overlap('list pets', ['get_pet_by_id', 'list_pets'], summaries) == 1.0


class TestSpecPins:
    """Pinned specs are checked on every use."""

    def test_a_local_pin_resolves_against_its_directory(self):
        pin = SpecPin(source=PETSTORE.name, sha256=sha256_of(PETSTORE))
        assert pin.fetch(FIXTURES_DIR) == PETSTORE.resolve()

    def test_a_changed_spec_is_refused(self):
        with pytest.raises(SpecChecksumError):
            SpecPin(source=PETSTORE.name, sha256='0' * 64).fetch(FIXTURES_DIR)

    def test_a_cached_download_is_reused(self, tmp_path, monkeypatch):
        """A remote pin already in the cache needs no network."""
        monkeypatch.setenv('OPENAPI_MCP_GATEWAY_CACHE', str(tmp_path))
        digest = sha256_of(PETSTORE)
        cached = tmp_path / 'specs' / f'{digest}.json'
        cached.parent.mkdir()
        shutil.copy(PETSTORE, cached)
        assert SpecPin(source='https://unreachable.invalid/spec.json', sha256=digest).fetch(tmp_path) == cached


class TestAnnotations:
    """Merging two annotators into judgements."""

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

    @pytest.mark.parametrize('relevant', [{'a': 3}, {'a': 1}])
    def test_an_adjudication_needs_valid_grades_and_an_answer(self, relevant):
        with pytest.raises(ValueError, match='grade'):
            _annotation('q1', {'a': 2}, {'b': 2}, adjudication={'relevant': relevant, 'note': ''})

    def test_splits_are_stratified_and_stable(self):
        annotated = [_annotation(f'q{index:02d}', {'a': 2}, {'a': 2}) for index in range(15)]
        split_by_query = annotations.assign_splits(annotated)
        assert split_by_query == annotations.assign_splits(reversed(annotated))
        assert sum(split == 'dev' for split in split_by_query.values()) == annotations.DEV_QUERIES_PER_STYLE

    def test_build_reports_the_agreement(self, tmp_path):
        path = tmp_path / annotations.ANNOTATIONS_DIRECTORY / 'pets.jsonl'
        path.parent.mkdir()
        annotated = [
            _annotation('q1', {'a': 2}, {'a': 2}),
            _annotation('q2', {'a': 2}, {'b': 2}, adjudication={'relevant': {'b': 2}, 'note': 'b fits'}),
        ]
        path.write_text(''.join(annotation.model_dump_json(exclude_none=True) + '\n' for annotation in annotated))
        spec_dataset, agreement = annotations.build_spec_dataset(tmp_path, 'pets')
        assert spec_dataset.judgements['dev'] == {'q1': {'a': 2}, 'q2': {'b': 2}}
        assert (agreement.query_count, agreement.shared_answer_count, agreement.adjudicated_count) == (2, 1, 1)


class TestDataset:
    """Loading a benchmark and refusing an inconsistent one."""

    def test_round_trip(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        assert set(benchmark.specs['pets'].judgements['test']) == {'p2', 'p3'}
        assert benchmark.specs['pets'].queries['p1'].style == 'literal'
        assert isinstance(benchmark.manifest.systems['default'], GatewaySystem)

    def test_a_query_judged_in_both_splits_is_refused(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        with (benchmark_directory / 'pets' / 'qrels' / 'test.tsv').open('a') as file:
            file.write('p1\tlist_pets\t2\n')
        with pytest.raises(dataset.DatasetError, match='both dev and test'):
            dataset.load(benchmark_directory)

    def test_a_query_without_an_answer_is_refused(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        (benchmark_directory / 'pets' / 'qrels' / 'dev.tsv').write_text(
            'query-id\tcorpus-id\tscore\np1\tlist_pets\t1\n'
        )
        with pytest.raises(dataset.DatasetError, match='no operation graded 2'):
            dataset.load(benchmark_directory)

    def test_an_unknown_style_is_refused(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        queries = benchmark_directory / 'pets' / 'queries.jsonl'
        queries.write_text(queries.read_text().replace('"literal"', '"poetic"'))
        with pytest.raises(dataset.DatasetError, match='style'):
            dataset.load(benchmark_directory)

    @pytest.mark.parametrize(
        ('original', 'replacement', 'message'),
        [
            ('system: default', 'system: missing', r'gate\.system'),
            ('type: random', 'type: nonsense', 'nonsense'),
            ('server: {exposure: {style: dynamic}}', 'server: {spec: other.json}', 'come from each spec'),
            ('server: {exposure: {style: dynamic}}', 'server: {exposure: {style: sideways}}', 'server config'),
        ],
    )
    def test_the_manifest_is_validated_on_load(self, tmp_path, original, replacement, message):
        """A wrong system or gate fails when the manifest loads, not when a run reaches it."""
        benchmark_directory = _petstore_benchmark(tmp_path)
        manifest = benchmark_directory / dataset.MANIFEST_FILE
        manifest.write_text(manifest.read_text().replace(original, replacement, 1))
        with pytest.raises(dataset.DatasetError, match=message):
            dataset.load(benchmark_directory)


class TestRun:
    """Running a benchmark against a gateway built in-process."""

    async def test_scores_every_query_under_each_system(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        results = await runner.run(benchmark, 'test', runner.select_systems(benchmark))
        assert set(results) == {'default', 'names-only', 'random'}
        result_by_query = {result.query_id: result for result in results['default']}
        assert result_by_query['p2'].ranking[0] == 'create_pet'
        assert result_by_query['p2'].scores['recall@1'] == 1.0

    async def test_a_new_kind_of_system_runs_without_changes_elsewhere(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        results = await runner.run(benchmark, 'test', {'reversed': ReversedNameSystem()})
        assert [len(result.ranking) for result in results['reversed']] == [6, 6]

    async def test_a_judged_operation_the_spec_does_not_serve_is_refused(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        (benchmark_directory / 'pets' / 'qrels' / 'test.tsv').write_text(
            'query-id\tcorpus-id\tscore\np2\tcreate_pet\t2\np3\tadopt_pet\t2\n'
        )
        benchmark = dataset.load(benchmark_directory)
        with pytest.raises(dataset.DatasetError, match='adopt_pet'):
            await runner.run(benchmark, 'test', runner.select_systems(benchmark))

    def test_an_unknown_system_name_is_refused(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        with pytest.raises(dataset.DatasetError, match='missing'):
            runner.select_systems(benchmark, ['missing'])

    async def test_report_baseline_and_regressions(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        results = await runner.run(benchmark, 'test', runner.select_systems(benchmark))
        built_report = report.build_report(benchmark, 'test', results)
        assert {comparison.against for comparison in built_report.comparisons} == {'names-only', 'random'}
        assert '| default |' in markdown.to_markdown(built_report)

        baseline = Baseline.from_report(built_report, benchmark.manifest.gate)
        assert baseline.regressions(baseline, 0.01) == []
        raised = baseline.model_copy(
            update={'macro': {metric: value + 0.5 for metric, value in baseline.macro.items()}}
        )
        assert len(raised.regressions(baseline, 0.01)) == 2

        other_split = baseline.model_copy(update={'split': 'dev'})
        assert baseline.regressions(other_split, 0.01) == ["split is 'dev' in this run but 'test' in the baseline"]

    async def test_comparisons_follow_the_gated_metrics(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        manifest = benchmark_directory / dataset.MANIFEST_FILE
        manifest.write_text(manifest.read_text() + '  metrics: [mrr@10]\n')
        benchmark = dataset.load(benchmark_directory)
        results = await runner.run(benchmark, 'test', runner.select_systems(benchmark))
        built_report = report.build_report(benchmark, 'test', results)
        assert {comparison.metric for comparison in built_report.comparisons} == {'mrr@10'}


class TestCli:
    """The commands CI and a developer run."""

    def test_check_passes_against_its_own_baseline_and_fails_below_it(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        cli = CliRunner()
        assert cli.invoke(main, ['search-ranking', 'check', str(benchmark_directory), '--update']).exit_code == 0
        assert cli.invoke(main, ['search-ranking', 'check', str(benchmark_directory)]).exit_code == 0

        baseline_path = benchmark_directory / dataset.BASELINE_FILE
        baseline = json.loads(baseline_path.read_text())
        baseline['macro']['ndcg@10'] = 2.0
        baseline_path.write_text(json.dumps(baseline))
        failed = cli.invoke(main, ['search-ranking', 'check', str(benchmark_directory)])
        assert failed.exit_code == 1
        assert 'macro ndcg@10' in failed.output

    def test_run_writes_a_report(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        output = tmp_path / 'report.json'
        result = CliRunner().invoke(main, ['search-ranking', 'run', str(benchmark_directory), '--output', str(output)])
        assert result.exit_code == 0, result.output
        written = report.Report.model_validate_json(output.read_text())
        assert (written.benchmark_version, written.split, written.reference_system) == (1, 'test', 'default')

    def test_an_unknown_system_is_a_clean_error(self, tmp_path):
        arguments = ['search-ranking', 'run', str(_petstore_benchmark(tmp_path)), '--system', 'missing']
        result = CliRunner().invoke(main, arguments)
        assert result.exit_code == 1
        assert 'Unknown system' in result.output

    def test_a_stale_pin_is_a_clean_error(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        (benchmark_directory / 'petstore.json').write_text('{}')
        result = CliRunner().invoke(main, ['search-ranking', 'run', str(benchmark_directory)])
        assert result.exit_code == 1
        assert 'SHA-256' in result.output
        assert 'Traceback' not in result.output

    def test_a_malformed_query_line_is_a_clean_error(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        with (benchmark_directory / 'pets' / 'queries.jsonl').open('a') as file:
            file.write('{not json\n')
        result = CliRunner().invoke(main, ['search-ranking', 'run', str(benchmark_directory)])
        assert result.exit_code == 1
        assert 'queries.jsonl:4' in result.output

    def test_check_refuses_a_paraphrase_that_repeats_its_answer(self, tmp_path):
        """A mislabelled query fails the check before any ranking runs."""
        benchmark_directory = _petstore_benchmark(tmp_path)
        queries = benchmark_directory / 'pets' / 'queries.jsonl'
        queries.write_text(queries.read_text().replace('"literal"}}\n{"_id": "p2"', '"paraphrase"}}\n{"_id": "p2"'))
        result = CliRunner().invoke(main, ['search-ranking', 'check', str(benchmark_directory), '--update'])
        assert result.exit_code == 1
        assert 'p1 (paraphrase' in result.output
        assert not (benchmark_directory / dataset.BASELINE_FILE).exists()


class TestAnnotationWorkflow:
    """The four stages that make the data anew, with the annotators played by fixed files."""

    def test_the_stages_turn_two_annotators_output_into_a_benchmark(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        _copy_annotation_templates(benchmark_directory)
        work_directory = tmp_path / 'work'
        cli = CliRunner()

        stage = cli.invoke(
            main, ['search-ranking', 'annotations', 'prepare-authoring', str(benchmark_directory), str(work_directory)]
        )
        assert stage.exit_code == 0, stage.output
        operations = (work_directory / 'pets' / annotation_workflow.OPERATIONS_FILE).read_text()
        assert operations.splitlines()[0] == 'name\tmethod\tpath\tsummary'
        assert 'list_pets\tGET\t/pets' in operations
        instructions = (work_directory / 'pets' / annotation_workflow.QUERY_AUTHOR_INSTRUCTIONS_FILE).read_text()
        assert 'operations of the pets API' in instructions

        _write_jsonl(
            work_directory / 'pets' / annotation_workflow.QUERY_AUTHOR_OUTPUT_FILE,
            [
                {'text': 'list my pets', 'style': 'literal', 'relevant': {'list_pets': 2}},
                {'text': 'I need a new animal on record', 'style': 'paraphrase', 'relevant': {'create_pet': 2}},
                {'text': 'the dog went to live on a farm', 'style': 'indirect', 'relevant': {'delete_pet': 2}},
            ],
        )
        stage = cli.invoke(
            main, ['search-ranking', 'annotations', 'prepare-assessment', str(benchmark_directory), str(work_directory)]
        )
        assert stage.exit_code == 0, stage.output
        assessor_queries = (work_directory / 'pets' / annotation_workflow.ASSESSOR_QUERIES_FILE).read_text()
        assert '"id": "pets-q002"' in assessor_queries
        assert 'create_pet' not in assessor_queries

        _write_jsonl(
            work_directory / 'pets' / annotation_workflow.INDEPENDENT_ASSESSOR_OUTPUT_FILE,
            [
                {'id': 'pets-q001', 'relevant': {'list_pets': 2}},
                {'id': 'pets-q002', 'relevant': {'create_pet': 2, 'create_pet_with_owner': 1}},
                {'id': 'pets-q003', 'relevant': {'delete_pet': 2}},
            ],
        )
        stage = cli.invoke(
            main, ['search-ranking', 'annotations', 'collect', str(benchmark_directory), str(work_directory)]
        )
        assert stage.exit_code == 0, stage.output
        stage = cli.invoke(main, ['search-ranking', 'annotations', 'build', str(benchmark_directory)])
        assert stage.exit_code == 0, stage.output
        assert 'pets: 3 queries, shared answer on 3' in stage.output

        rebuilt = dataset.load(benchmark_directory).specs['pets']
        assert rebuilt.judgements['dev']['pets-q002'] == {'create_pet': 2, 'create_pet_with_owner': 1}

    def test_collect_refuses_an_operation_the_spec_does_not_serve(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        _copy_annotation_templates(benchmark_directory)
        work_directory = tmp_path / 'work'
        manifest = dataset.load_manifest(benchmark_directory)
        annotation_workflow.prepare_authoring(manifest, benchmark_directory, work_directory)
        _write_jsonl(
            work_directory / 'pets' / annotation_workflow.QUERY_AUTHOR_OUTPUT_FILE,
            [{'text': 'adopt this kitten', 'style': 'literal', 'relevant': {'adopt_pet': 2}}],
        )
        annotation_workflow.prepare_assessment(manifest, benchmark_directory, work_directory)
        _write_jsonl(
            work_directory / 'pets' / annotation_workflow.INDEPENDENT_ASSESSOR_OUTPUT_FILE,
            [{'id': 'pets-q001', 'relevant': {'create_pet': 2}}],
        )
        with pytest.raises(dataset.DatasetError, match='adopt_pet'):
            annotation_workflow.collect(manifest, benchmark_directory, work_directory)

    def test_a_stage_run_out_of_order_says_which_comes_first(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        _copy_annotation_templates(benchmark_directory)
        arguments = [
            'search-ranking',
            'annotations',
            'prepare-assessment',
            str(benchmark_directory),
            str(tmp_path / 'work'),
        ]
        result = CliRunner().invoke(main, arguments)
        assert result.exit_code == 1
        assert 'stage before this one' in result.output

    def test_collect_keeps_an_adjudication_for_an_unchanged_query(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        _copy_annotation_templates(benchmark_directory)
        work_directory = tmp_path / 'work'
        manifest = dataset.load_manifest(benchmark_directory)
        annotation_workflow.prepare_authoring(manifest, benchmark_directory, work_directory)
        _write_jsonl(
            work_directory / 'pets' / annotation_workflow.QUERY_AUTHOR_OUTPUT_FILE,
            [
                {'text': 'list my pets', 'style': 'literal', 'relevant': {'list_pets': 2}},
                {'text': 'a new animal arrived', 'style': 'paraphrase', 'relevant': {'create_pet': 2}},
                {'text': 'the dog moved to a farm', 'style': 'indirect', 'relevant': {'delete_pet': 2}},
            ],
        )
        annotation_workflow.prepare_assessment(manifest, benchmark_directory, work_directory)
        _write_jsonl(
            work_directory / 'pets' / annotation_workflow.INDEPENDENT_ASSESSOR_OUTPUT_FILE,
            [
                {'id': 'pets-q001', 'relevant': {'list_pets': 2}},
                {'id': 'pets-q002', 'relevant': {'create_pet_with_owner': 2}},
                {'id': 'pets-q003', 'relevant': {'delete_pet': 2}},
            ],
        )
        annotation_workflow.collect(manifest, benchmark_directory, work_directory)
        annotations_file = benchmark_directory / annotations.ANNOTATIONS_DIRECTORY / 'pets.jsonl'
        with pytest.raises(dataset.DatasetError, match='pets-q002'):
            annotations.build_spec_dataset(benchmark_directory, 'pets')
        queries_before = (benchmark_directory / 'pets' / dataset.QUERIES_FILE).read_text()
        result = CliRunner().invoke(main, ['search-ranking', 'annotations', 'build', str(benchmark_directory)])
        assert result.exit_code == 1
        assert (benchmark_directory / 'pets' / dataset.QUERIES_FILE).read_text() == queries_before

        adjudicated = [
            annotation.model_copy(
                update={'adjudication': annotations.Adjudication(relevant={'create_pet': 2}, note='either fits')}
            )
            if annotation.id == 'pets-q002'
            else annotation
            for annotation in annotations.read_annotations(annotations_file)
        ]
        annotations_file.write_text(
            ''.join(annotation.model_dump_json(exclude_none=True) + '\n' for annotation in adjudicated)
        )
        annotation_workflow.collect(manifest, benchmark_directory, work_directory)
        kept = {annotation.id: annotation for annotation in annotations.read_annotations(annotations_file)}
        assert kept['pets-q002'].adjudication is not None
        assert annotations.build_spec_dataset(benchmark_directory, 'pets')[1].adjudicated_count == 1


class TestPydanticAIKeywordSystem:
    """Pydantic AI's keyword tool search, run through the framework's public API."""

    async def test_ranks_by_the_framework_search(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        corpus = await runner.load_corpus(benchmark, 'pets')
        async with PydanticAIKeywordSystem().open(corpus) as retriever:
            ranking = await retriever.rank('delete a pet', 20)
        assert ranking[0] == 'delete_pet'
        assert len(ranking) == len(set(ranking))


class TestAutomatedAnnotation:
    """Both annotators run through Pydantic AI, with scripted models in place of providers."""

    async def test_annotates_retries_a_wrong_name_and_feeds_collect(self, tmp_path):
        benchmark_directory = _petstore_benchmark(tmp_path)
        _copy_annotation_templates(benchmark_directory)
        work_directory = tmp_path / 'work'
        author_answers = [
            [{'text': 'list pets', 'style': 'literal', 'relevant': {'no_such_operation': 2}}],
            [
                {'text': 'list pets', 'style': 'literal', 'relevant': {'list_pets': 2}},
                {'text': 'a new animal arrived', 'style': 'paraphrase', 'relevant': {'create_pet': 2}},
                {'text': 'the dog moved away', 'style': 'indirect', 'relevant': {'delete_pet': 2}},
            ],
        ]
        assessed = [
            {'id': f'pets-q00{number}', 'relevant': {name: 2}}
            for number, name in [(1, 'list_pets'), (2, 'create_pet'), (3, 'delete_pet')]
        ]

        def author(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {'response': author_answers.pop(0)})])

        def assessor(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {'response': assessed})])

        manifest = dataset.load_manifest(benchmark_directory)
        runs = await automated_annotation.annotate(
            manifest,
            benchmark_directory,
            work_directory,
            author_model=FunctionModel(author),
            assessor_model=FunctionModel(assessor),
        )
        assert [(run.role, run.model_requests) for run in runs] == [('query author', 2), ('independent assessor', 1)]
        assert (work_directory / 'pets' / automated_annotation.PROVENANCE_FILE).exists()
        assert annotation_workflow.collect(manifest, benchmark_directory, work_directory) == {'pets': 3}
        assert annotations.build_spec_dataset(benchmark_directory, 'pets')[1].shared_answer_count == 3


def _annotation(
    query_id: str,
    annotator_a: dict[str, int],
    annotator_b: dict[str, int],
    adjudication: dict[str, typing.Any] | None = None,
) -> annotations.Annotation:
    return annotations.Annotation.model_validate(
        {
            'id': query_id,
            'text': 'text',
            'style': 'literal',
            'annotator_a': annotator_a,
            'annotator_b': annotator_b,
            'adjudication': adjudication,
        }
    )


def _copy_annotation_templates(benchmark_directory: pathlib.Path) -> None:
    templates = REPOSITORY_ANNOTATIONS
    target = benchmark_directory / annotations.ANNOTATIONS_DIRECTORY
    target.mkdir(exist_ok=True)
    for template in (annotation_workflow.QUERY_AUTHOR_TEMPLATE, annotation_workflow.INDEPENDENT_ASSESSOR_TEMPLATE):
        shutil.copy(templates / template, target / template)


def _write_jsonl(path: pathlib.Path, records: list[dict[str, typing.Any]]) -> None:
    path.write_text(''.join(json.dumps(record) + '\n' for record in records))


def _petstore_benchmark(root: pathlib.Path) -> pathlib.Path:
    """A three-query benchmark over the petstore fixture, pinned as a local file, with one dev and two test queries."""
    benchmark_directory = root / 'benchmark'
    benchmark_directory.mkdir()
    shutil.copy(PETSTORE, benchmark_directory / 'petstore.json')
    (benchmark_directory / dataset.MANIFEST_FILE).write_text(MANIFEST.format(sha256=sha256_of(PETSTORE)))
    spec_dataset = dataset.SpecDataset(
        name='pets',
        queries={
            'p1': dataset.Query(id='p1', text='list pets', style='literal'),
            'p2': dataset.Query(id='p2', text='create a pet', style='literal'),
            'p3': dataset.Query(id='p3', text='I no longer own this animal', style='indirect'),
        },
        judgements={
            'dev': {'p1': {'list_pets': 2}},
            'test': {'p2': {'create_pet': 2, 'create_pet_with_owner': 1}, 'p3': {'delete_pet': 2}},
        },
    )
    dataset.write_spec_dataset(benchmark_directory, spec_dataset)
    return benchmark_directory
