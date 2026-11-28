import contextlib
import json
import pathlib
import shutil
import typing

import pytest
from click.testing import CliRunner
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from openapi_mcp_gateway_eval import provenance
from openapi_mcp_gateway_eval.cli import benchmark_command
from openapi_mcp_gateway_eval.search_ranking import dataset, markdown, overlap, report, runner
from openapi_mcp_gateway_eval.search_ranking.annotations import annotators, merge
from openapi_mcp_gateway_eval.search_ranking.annotations.pipeline import AnnotationPipeline
from openapi_mcp_gateway_eval.search_ranking.gate_scores import GateScores
from openapi_mcp_gateway_eval.search_ranking.metrics import (
    NormalizedDiscountedCumulativeGain,
    Recall,
    ReciprocalRank,
)
from openapi_mcp_gateway_eval.search_ranking.variants import (
    GatewayVariant,
    PydanticAIKeywordVariant,
    Retriever,
    SpecCorpus,
    Variant,
)
from openapi_mcp_gateway_eval.significance import macro_mean, paired_bootstrap
from openapi_mcp_gateway_eval.spec_pins import SpecChecksumError, SpecPin, sha256_of


FIXTURES_DIR = pathlib.Path(__file__).parent / 'fixtures'
SHOP = FIXTURES_DIR / 'shop.json'
REPOSITORY_PROMPTS = pathlib.Path(__file__).parents[2] / 'benchmarks' / 'search_ranking' / dataset.PROMPTS_DIRECTORY

# The phrase that tells the reviser's instructions apart from the query author's, which share a model.
REVISER_PHRASE = 'revising queries you wrote'

# Three queries both annotators answer alike, which leave the adjudicator and the reviser nothing to do.
AGREED_QUERIES = [
    {'text': 'list all products', 'style': 'literal', 'relevant': {'list_products': 2}},
    {'text': 'a new item came in', 'style': 'paraphrase', 'relevant': {'list_products': 2}},
    {'text': 'what is on the shelves', 'style': 'indirect', 'relevant': {'list_products': 2}},
]

MANIFEST = """\
version: 1
specs:
  products: {{source: shop.json, sha256: {sha256}}}
variants:
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
  variant: default
"""


class ReversedNameSystem(Variant):
    """A variant a test defines on its own, to show a new kind needs no change to the runner or the report."""

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
        summaries = {'list_products': 'List all products', 'get_product_by_id': 'Info for a specific product'}
        assert overlap.best_term_overlap('list products', ['get_product_by_id', 'list_products'], summaries) == 1.0


class TestSpecPins:
    """Pinned specs are checked on every use."""

    def test_a_local_pin_resolves_against_its_directory(self):
        pin = SpecPin(source=SHOP.name, sha256=sha256_of(SHOP))
        assert pin.fetch(FIXTURES_DIR) == SHOP.resolve()

    def test_a_changed_spec_is_refused(self):
        with pytest.raises(SpecChecksumError):
            SpecPin(source=SHOP.name, sha256='0' * 64).fetch(FIXTURES_DIR)

    def test_a_cached_download_is_reused(self, tmp_path, monkeypatch):
        """A remote pin already in the cache needs no network."""
        monkeypatch.setenv('OPENAPI_MCP_GATEWAY_CACHE', str(tmp_path))
        digest = sha256_of(SHOP)
        cached = tmp_path / 'specs' / f'{digest}.json'
        cached.parent.mkdir()
        shutil.copy(SHOP, cached)
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
        assert merge.cohens_kappa([(2, 2), (1, 1), (0, 0)]) == 1.0
        assert merge.cohens_kappa([(2, 2), (2, 1), (1, 2), (1, 1)]) == 0.0

    @pytest.mark.parametrize('relevant', [{'a': 3}, {'a': 1}])
    def test_an_adjudication_needs_valid_grades_and_an_answer(self, relevant):
        with pytest.raises(ValueError, match='grade'):
            _annotation('q1', {'a': 2}, {'b': 2}, adjudication={'relevant': relevant, 'note': ''})

    def test_splits_are_stratified_and_stable(self):
        annotated = [_annotation(f'q{index:02d}', {'a': 2}, {'a': 2}) for index in range(15)]
        split_by_query = merge.assign_splits(annotated)
        assert split_by_query == merge.assign_splits(reversed(annotated))
        assert sum(split == 'dev' for split in split_by_query.values()) == merge.DEV_QUERIES_PER_STYLE

    def test_build_reports_the_agreement(self, tmp_path):
        path = merge.annotations_path(tmp_path, 'products')
        path.parent.mkdir(parents=True)
        annotated = [
            _annotation('q1', {'a': 2}, {'a': 2}),
            _annotation('q2', {'a': 2}, {'b': 2}, adjudication={'relevant': {'b': 2}, 'note': 'b fits'}),
        ]
        path.write_text(''.join(annotation.model_dump_json(exclude_none=True) + '\n' for annotation in annotated))
        spec_dataset, agreement = merge.build_spec_dataset(tmp_path, 'products')
        assert spec_dataset.judgements['dev'] == {'q1': {'a': 2}, 'q2': {'b': 2}}
        assert (agreement.query_count, agreement.shared_answer_count, agreement.adjudicated_count) == (2, 1, 1)


class TestDataset:
    """Loading a benchmark and refusing an inconsistent one."""

    def test_round_trip(self, tmp_path):
        benchmark = dataset.load(_shop_benchmark(tmp_path))
        assert set(benchmark.specs['products'].judgements['test']) == {'p2', 'p3'}
        assert benchmark.specs['products'].queries['p1'].style == 'literal'
        assert isinstance(benchmark.manifest.variants['default'], GatewayVariant)

    def test_a_query_judged_in_both_splits_is_refused(self, tmp_path):
        benchmark_directory = _shop_benchmark(tmp_path)
        with (dataset.spec_directory(benchmark_directory, 'products') / 'qrels' / 'test.tsv').open('a') as file:
            file.write('p1\tlist_products\t2\n')
        with pytest.raises(dataset.DatasetError, match='both dev and test'):
            dataset.load(benchmark_directory)

    def test_a_query_without_an_answer_is_refused(self, tmp_path):
        benchmark_directory = _shop_benchmark(tmp_path)
        (dataset.spec_directory(benchmark_directory, 'products') / 'qrels' / 'dev.tsv').write_text(
            'query-id\tcorpus-id\tscore\np1\tlist_products\t1\n'
        )
        with pytest.raises(dataset.DatasetError, match='no operation graded 2'):
            dataset.load(benchmark_directory)

    def test_an_unknown_style_is_refused(self, tmp_path):
        benchmark_directory = _shop_benchmark(tmp_path)
        queries = dataset.spec_directory(benchmark_directory, 'products') / 'queries.jsonl'
        queries.write_text(queries.read_text().replace('"literal"', '"poetic"'))
        with pytest.raises(dataset.DatasetError, match='style'):
            dataset.load(benchmark_directory)

    @pytest.mark.parametrize(
        ('original', 'replacement', 'message'),
        [
            ('variant: default', 'variant: missing', r'gate\.variant'),
            ('type: random', 'type: nonsense', 'nonsense'),
            ('server: {exposure: {style: dynamic}}', 'server: {spec: other.json}', 'come from each spec'),
            ('server: {exposure: {style: dynamic}}', 'server: {exposure: {style: sideways}}', 'server config'),
        ],
    )
    def test_the_manifest_is_validated_on_load(self, tmp_path, original, replacement, message):
        """A wrong variant or gate fails when the manifest loads, not when a run reaches it."""
        benchmark_directory = _shop_benchmark(tmp_path)
        manifest = benchmark_directory / dataset.MANIFEST_FILE
        manifest.write_text(manifest.read_text().replace(original, replacement, 1))
        with pytest.raises(dataset.DatasetError, match=message):
            dataset.load(benchmark_directory)


class TestRun:
    """Running a benchmark against a gateway built in-process."""

    async def test_scores_every_query_under_each_system(self, tmp_path):
        benchmark = dataset.load(_shop_benchmark(tmp_path))
        results = await runner.run(benchmark, 'test', runner.select_variants(benchmark))
        assert set(results) == {'default', 'names-only', 'random'}
        result_by_query = {result.query_id: result for result in results['default']}
        assert result_by_query['p2'].ranking[0] == 'create_product'
        assert result_by_query['p2'].scores['recall@1'] == 1.0

    async def test_a_new_kind_of_system_runs_without_changes_elsewhere(self, tmp_path):
        benchmark = dataset.load(_shop_benchmark(tmp_path))
        results = await runner.run(benchmark, 'test', {'reversed': ReversedNameSystem()})
        assert [len(result.ranking) for result in results['reversed']] == [6, 6]

    async def test_a_judged_operation_the_spec_does_not_serve_is_refused(self, tmp_path):
        benchmark_directory = _shop_benchmark(tmp_path)
        (dataset.spec_directory(benchmark_directory, 'products') / 'qrels' / 'test.tsv').write_text(
            'query-id\tcorpus-id\tscore\np2\tcreate_product\t2\np3\tbuy_product\t2\n'
        )
        benchmark = dataset.load(benchmark_directory)
        with pytest.raises(dataset.DatasetError, match='buy_product'):
            await runner.run(benchmark, 'test', runner.select_variants(benchmark))

    def test_an_unknown_system_name_is_refused(self, tmp_path):
        benchmark = dataset.load(_shop_benchmark(tmp_path))
        with pytest.raises(dataset.DatasetError, match='missing'):
            runner.select_variants(benchmark, ['missing'])

    async def test_report_gate_scores_and_regressions(self, tmp_path):
        benchmark = dataset.load(_shop_benchmark(tmp_path))
        results = await runner.run(benchmark, 'test', runner.select_variants(benchmark))
        built_report = report.build_report(benchmark, 'test', results)
        assert {comparison.against for comparison in built_report.comparisons} == {'names-only', 'random'}
        assert '| default |' in markdown.to_markdown(built_report)

        scores = GateScores.from_report(built_report, benchmark.manifest.gate)
        assert scores.regressions(scores, 0.01) == []
        raised = scores.model_copy(update={'macro': {metric: value + 0.5 for metric, value in scores.macro.items()}})
        assert len(raised.regressions(scores, 0.01)) == 2

        other_split = scores.model_copy(update={'split': 'dev'})
        assert scores.regressions(other_split, 0.01) == ["split is 'dev' in this run but 'test' in the gate scores"]

    async def test_comparisons_follow_the_gated_metrics(self, tmp_path):
        benchmark_directory = _shop_benchmark(tmp_path)
        manifest = benchmark_directory / dataset.MANIFEST_FILE
        manifest.write_text(manifest.read_text() + '  metrics: [mrr@10]\n')
        benchmark = dataset.load(benchmark_directory)
        results = await runner.run(benchmark, 'test', runner.select_variants(benchmark))
        built_report = report.build_report(benchmark, 'test', results)
        assert {comparison.metric for comparison in built_report.comparisons} == {'mrr@10'}


class TestCli:
    """The commands CI and a developer run."""

    def test_check_passes_against_its_own_gate_scores_and_fails_below_them(self, tmp_path):
        benchmark_directory = _shop_benchmark(tmp_path)
        cli = CliRunner()
        assert (
            cli.invoke(
                benchmark_command,
                ['search-ranking', 'check', '--benchmark-directory', str(benchmark_directory), '--update'],
            ).exit_code
            == 0
        )
        assert (
            cli.invoke(
                benchmark_command, ['search-ranking', 'check', '--benchmark-directory', str(benchmark_directory)]
            ).exit_code
            == 0
        )

        gate_scores_path = benchmark_directory / dataset.GATE_SCORES_FILE
        scores = json.loads(gate_scores_path.read_text())
        scores['macro']['ndcg@10'] = 2.0
        gate_scores_path.write_text(json.dumps(scores))
        failed = cli.invoke(
            benchmark_command, ['search-ranking', 'check', '--benchmark-directory', str(benchmark_directory)]
        )
        assert failed.exit_code == 1
        assert 'macro ndcg@10' in failed.output

    def test_run_writes_a_report(self, tmp_path):
        benchmark_directory = _shop_benchmark(tmp_path)
        output = tmp_path / 'report.json'
        result = CliRunner().invoke(
            benchmark_command,
            ['search-ranking', 'run', '--benchmark-directory', str(benchmark_directory), '--output', str(output)],
        )
        assert result.exit_code == 0, result.output
        written = report.Report.model_validate_json(output.read_text())
        assert (written.benchmark_version, written.split, written.reference_variant) == (1, 'test', 'default')

    def test_an_unknown_system_is_a_clean_error(self, tmp_path):
        benchmark_directory = str(_shop_benchmark(tmp_path))
        arguments = ['search-ranking', 'run', '--benchmark-directory', benchmark_directory, '--variant', 'missing']
        result = CliRunner().invoke(benchmark_command, arguments)
        assert result.exit_code == 1
        assert 'Unknown variant' in result.output

    def test_a_stale_pin_is_a_clean_error(self, tmp_path):
        benchmark_directory = _shop_benchmark(tmp_path)
        (benchmark_directory / 'shop.json').write_text('{}')
        result = CliRunner().invoke(
            benchmark_command, ['search-ranking', 'run', '--benchmark-directory', str(benchmark_directory)]
        )
        assert result.exit_code == 1
        assert 'SHA-256' in result.output
        assert 'Traceback' not in result.output

    def test_a_malformed_query_line_is_a_clean_error(self, tmp_path):
        benchmark_directory = _shop_benchmark(tmp_path)
        with (dataset.spec_directory(benchmark_directory, 'products') / 'queries.jsonl').open('a') as file:
            file.write('{not json\n')
        result = CliRunner().invoke(
            benchmark_command, ['search-ranking', 'run', '--benchmark-directory', str(benchmark_directory)]
        )
        assert result.exit_code == 1
        assert 'queries.jsonl:4' in result.output

    def test_check_refuses_a_paraphrase_that_repeats_its_answer(self, tmp_path):
        """A mislabelled query fails the check before any ranking runs."""
        benchmark_directory = _shop_benchmark(tmp_path)
        queries = dataset.spec_directory(benchmark_directory, 'products') / 'queries.jsonl'
        queries.write_text(queries.read_text().replace('"literal"}}\n{"_id": "p2"', '"paraphrase"}}\n{"_id": "p2"'))
        result = CliRunner().invoke(
            benchmark_command,
            ['search-ranking', 'check', '--benchmark-directory', str(benchmark_directory), '--update'],
        )
        assert result.exit_code == 1
        assert 'p1 (paraphrase' in result.output
        assert not (benchmark_directory / dataset.GATE_SCORES_FILE).exists()


class TestPydanticAIKeywordSystem:
    """Pydantic AI's keyword tool search, run through the framework's public API."""

    async def test_ranks_by_the_framework_search(self, tmp_path):
        benchmark = dataset.load(_shop_benchmark(tmp_path))
        corpus = await runner.load_corpus(benchmark, 'products')
        async with PydanticAIKeywordVariant().open(corpus) as retriever:
            ranking = await retriever.rank('delete a product', 20)
        assert ranking[0] == 'delete_product'
        assert len(ranking) == len(set(ranking))


class TestAnnotationPipeline:
    """Every annotator run through Pydantic AI, with scripted models in place of providers."""

    async def test_makes_the_data_with_every_role_and_retries_what_its_checks_refuse(self, tmp_path):
        benchmark_directory = _shop_benchmark(tmp_path)
        _copy_annotation_templates(benchmark_directory)
        authored = [
            {'text': 'list all products', 'style': 'literal', 'relevant': {'list_products': 2}},
            {'text': 'create a product', 'style': 'paraphrase', 'relevant': {'create_product': 2}},
            {'text': 'we stopped selling this item', 'style': 'indirect', 'relevant': {'delete_product': 2}},
        ]
        author = _ScriptedModel(
            {
                REVISER_PHRASE: [
                    [{'id': 'products-q002', 'text': 'create my product'}],
                    [{'id': 'products-q002', 'text': 'a new item came in'}],
                ],
                '': [[{**authored[0], 'relevant': {'no_such_operation': 2}}], authored],
            }
        )
        assessor = _ScriptedModel(
            {
                '': [
                    [
                        {'id': 'products-q001', 'relevant': {'list_products': 2}},
                        {'id': 'products-q002', 'relevant': {'create_product_with_owner': 2}},
                        {'id': 'products-q003', 'relevant': {'delete_product': 2}},
                    ]
                ]
            }
        )
        adjudicator = _ScriptedModel(
            {'': [[{'id': 'products-q002', 'relevant': {'create_product': 2}, 'note': 'no owner'}]]}
        )
        pipeline = _pipeline(benchmark_directory, author, assessor, adjudicator)

        [result] = await pipeline.run(tmp_path / 'work')

        assert [(run.role, run.model_requests) for run in result.runs] == [
            ('query author', 2),
            ('independent assessor', 1),
            ('adjudicator', 1),
            ('reviser', 2),
        ]
        spec_dataset, agreement = merge.build_spec_dataset(benchmark_directory, 'products')
        assert spec_dataset.queries['products-q002'].text == 'a new item came in'
        assert (agreement.shared_answer_count, agreement.adjudicated_count) == (2, 1)
        record = provenance.provenance_path(merge.annotations_path(benchmark_directory, 'products'))
        assert [run['role'] for run in json.loads(record.read_text())] == [run.role for run in result.runs]
        assert (tmp_path / 'work' / 'products' / f'independent_assessor{annotators.MESSAGES_SUFFIX}').exists()

    async def test_the_assessor_never_sees_the_authors_answers(self, tmp_path):
        benchmark_directory = _shop_benchmark(tmp_path)
        _copy_annotation_templates(benchmark_directory)
        author = _ScriptedModel({'': [AGREED_QUERIES]})
        judged = [{'id': f'products-q00{number}', 'relevant': {'list_products': 2}} for number in (1, 2, 3)]
        assessor = _ScriptedModel({'': [judged]})
        pipeline = _pipeline(benchmark_directory, author, assessor, _ScriptedModel({}))

        await pipeline.annotate_spec('products', tmp_path / 'work')

        [instructions] = assessor.instructions
        assert '{"id":"products-q002","text":"a new item came in"}' in instructions
        assert '"relevant"' not in instructions

    async def test_a_failing_spec_writes_nothing(self, tmp_path):
        benchmark_directory = _shop_benchmark(tmp_path)
        _copy_annotation_templates(benchmark_directory)
        wrong = [{'text': 'list products', 'style': 'literal', 'relevant': {'no_such_operation': 2}}]
        author = _ScriptedModel({'': [wrong] * (annotators.OUTPUT_RETRIES + 1)})
        pipeline = _pipeline(benchmark_directory, author, _ScriptedModel({}), _ScriptedModel({}))

        with pytest.raises(UnexpectedModelBehavior):
            await pipeline.run(tmp_path / 'work')

        assert not merge.annotations_path(benchmark_directory, 'products').exists()

    def test_the_command_needs_all_three_models(self, tmp_path):
        arguments = [
            'search-ranking',
            'generate-annotations',
            '--benchmark-directory',
            str(_shop_benchmark(tmp_path)),
        ]
        result = CliRunner().invoke(
            benchmark_command, [*arguments, '--author-model', 'test', '--assessor-model', 'test']
        )
        assert result.exit_code == 2
        assert '--adjudicator-model' in result.output


class _ScriptedModel:
    """A model that answers each role from its own script, in turn, and keeps the instructions each run was given.

    A role is told apart by a phrase of its instructions, since the author's model also runs the reviser.
    """

    def __init__(self, scripts: dict[str, list[list[dict[str, typing.Any]]]]):
        self._scripts = {phrase: list(answers) for phrase, answers in scripts.items()}
        self.instructions: list[str] = []

    def model(self) -> FunctionModel:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            instructions = next(
                message.instructions
                for message in reversed(messages)
                if isinstance(message, ModelRequest) and message.instructions
            )
            if not any(isinstance(message, ModelResponse) for message in messages):
                self.instructions.append(instructions)
            answers = next(answers for phrase, answers in self._scripts.items() if phrase in instructions)
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {'response': answers.pop(0)})])

        return FunctionModel(respond)


def _pipeline(
    benchmark_directory: pathlib.Path, author: _ScriptedModel, assessor: _ScriptedModel, adjudicator: _ScriptedModel
) -> AnnotationPipeline:
    return AnnotationPipeline(
        dataset.load_manifest(benchmark_directory),
        benchmark_directory,
        author_model=author.model(),
        assessor_model=assessor.model(),
        adjudicator_model=adjudicator.model(),
    )


def _annotation(
    query_id: str,
    annotator_a: dict[str, int],
    annotator_b: dict[str, int],
    adjudication: dict[str, typing.Any] | None = None,
) -> merge.Annotation:
    return merge.Annotation.model_validate(
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
    templates = REPOSITORY_PROMPTS
    target = benchmark_directory / dataset.PROMPTS_DIRECTORY
    target.mkdir(exist_ok=True)
    for annotator in (
        annotators.AuthoringAnnotator,
        annotators.IndependentAssessingAnnotator,
        annotators.AdjudicatingAnnotator,
        annotators.RevisingAnnotator,
    ):
        template = annotator.template
        shutil.copy(templates / template, target / template)


def _write_jsonl(path: pathlib.Path, records: list[dict[str, typing.Any]]) -> None:
    path.write_text(''.join(json.dumps(record) + '\n' for record in records))


def _shop_benchmark(root: pathlib.Path) -> pathlib.Path:
    """A three-query benchmark over the shop fixture, pinned as a local file, with one dev and two test queries."""
    benchmark_directory = root / 'benchmark'
    benchmark_directory.mkdir()
    shutil.copy(SHOP, benchmark_directory / 'shop.json')
    (benchmark_directory / dataset.MANIFEST_FILE).write_text(MANIFEST.format(sha256=sha256_of(SHOP)))
    spec_dataset = dataset.SpecDataset(
        name='products',
        queries={
            'p1': dataset.Query(id='p1', text='list products', style='literal'),
            'p2': dataset.Query(id='p2', text='create a product', style='literal'),
            'p3': dataset.Query(id='p3', text='we stopped selling this item', style='indirect'),
        },
        judgements={
            'dev': {'p1': {'list_products': 2}},
            'test': {'p2': {'create_product': 2, 'create_product_with_owner': 1}, 'p3': {'delete_product': 2}},
        },
    )
    dataset.write_spec_dataset(benchmark_directory, spec_dataset)
    return benchmark_directory
