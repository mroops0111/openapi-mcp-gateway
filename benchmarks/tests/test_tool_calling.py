import datetime
import json
import pathlib
import shutil
import typing

import httpx
import pytest
import yaml
from click.testing import CliRunner
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from openapi_mcp_gateway_benchmarks.cli import main
from openapi_mcp_gateway_benchmarks.spec_pins import sha256_of
from openapi_mcp_gateway_benchmarks.tool_calling import (
    config_generation,
    dataset,
    grading,
    reach,
    report,
    runner,
    validation,
)
from openapi_mcp_gateway_benchmarks.tool_calling.local_server import serve
from openapi_mcp_gateway_benchmarks.tool_calling.markdown import to_markdown
from openapi_mcp_gateway_benchmarks.tool_calling.replay import RecordedRequest, ReplayUpstream, ResponseOverride
from openapi_mcp_gateway_benchmarks.tool_calling.sampling import SchemaSampler
from openapi_mcp_gateway_benchmarks.tool_calling.validation import solution_model


FIXTURES_DIR = pathlib.Path(__file__).parent / 'fixtures'
PETSTORE = FIXTURES_DIR / 'petstore.json'
REPOSITORY_PROMPTS = pathlib.Path(__file__).parents[1] / 'tool_calling' / dataset.PROMPTS_DIRECTORY

MANIFEST = """\
version: 1
specs:
  pets: {{source: petstore.json, sha256: {sha256}}}
models:
  scripted: {{model: test}}
scenarios: [pets]
repetitions: 2
system_prompt: Carry out the request with the tools.
config_generator:
  model: {{model: test}}
  skill_library: skills
  skill: generate-config
  variants: {{generated: ''}}
"""

SKILL = """\
---
name: generate-config
description: Generate an openapi-mcp-gateway config for an API.
---

Expose only the operations the request needs.
"""

# A config the gateway refuses, since a server has no key by this name.
UNLOADABLE_CONFIG = """\
servers:
  - {name: pets, spec: pets.json, base_url: https://upstream.invalid, polcy: {allow: ['*Pet*']}}
"""

GENERATED_CONFIG = """\
servers:
  - name: pets
    spec: pets.json
    base_url: https://upstream.invalid
    auth: {upstream: {type: none}}
    policy: {allow: ['*Pet*']}
"""

SCENARIO = """\
intent: "Keep the shop's pet records current."
servers: {pets: pets}
"""

TASKS = """\
- id: pets-t01
  prompt: Pet 7 was adopted, take it off the list.
  split: test
  expected_calls:
    - {server: pets, method: DELETE, path: '/pets/{petId}', path_parameters: {petId: '7'}}
  solution:
    - {server: pets, operation: delete_pet, arguments: {petId: 7}}
- id: pets-t02
  prompt: Look up pet 3 for me.
  split: test
  expected_calls:
    - {server: pets, method: GET, path: '/pets/{petId}', path_parameters: {petId: '3'}}
  solution:
    - {server: pets, operation: get_pet_by_id, arguments: {petId: 3}}
"""

VARIANTS = {
    'baseline': '{}\n',
    'dynamic': 'servers:\n  pets:\n    exposure: {style: dynamic}\n',
    'no-admin': 'servers:\n  pets:\n    policy: {deny: ["GET /admin/*"]}\n',
}


class TestSampling:
    """Fixed sample responses built from a spec."""

    def test_an_example_wins_over_the_schema(self):
        document = {'components': {'schemas': {'Pet': {'type': 'object', 'example': {'id': 1, 'name': 'Rex'}}}}}
        operation = {
            'responses': {'200': {'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Pet'}}}}}
        }
        assert SchemaSampler(document).response_body(operation) == (200, {'id': 1, 'name': 'Rex'})

    def test_a_schema_is_sampled_by_type_and_format(self):
        schema = {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer'},
                'created_at': {'type': 'string', 'format': 'date-time'},
                'state': {'type': 'string', 'enum': ['open', 'closed']},
                'tags': {'type': 'array', 'items': {'type': 'string'}},
            },
        }
        assert SchemaSampler({}).sample(schema) == {
            'id': 1,
            'created_at': '2026-01-15T09:30:00Z',
            'state': 'open',
            'tags': ['string'],
        }

    def test_a_recursive_schema_ends(self):
        document = {
            'components': {
                'schemas': {'Node': {'type': 'object', 'properties': {'child': {'$ref': '#/components/schemas/Node'}}}}
            }
        }
        sample = SchemaSampler(document).sample({'$ref': '#/components/schemas/Node'})
        assert json.dumps(sample).count('child') <= 6


class TestReplay:
    """The stand-in upstream answers and records every request."""

    async def test_answers_overrides_samples_and_unknown_paths(self):
        replay = ReplayUpstream({'pets': json.loads(PETSTORE.read_text())})
        replay.start_task([ResponseOverride(server='pets', method='GET', path='/pets/{petId}', body={'name': 'Rex'})])
        async with serve(replay.app) as url, httpx.AsyncClient() as client:
            overridden = await client.get(f'{url}/pets/pets/7')
            deleted = await client.delete(f'{url}/pets/pets/7')
            unknown = await client.get(f'{url}/pets/nowhere')
        assert overridden.json() == {'name': 'Rex'}
        assert deleted.status_code == 204
        assert unknown.status_code == 404
        assert [(request.method, request.operation, request.answer) for request in replay.requests] == [
            ('GET', 'GET /pets/{petId}', 'task'),
            ('DELETE', 'DELETE /pets/{petId}', 'spec'),
            ('GET', None, 'not_found'),
        ]

    async def test_the_override_naming_the_query_answers_it(self):
        """One search endpoint answers each kind it is asked for, and anything else from the general override."""
        replay = ReplayUpstream({'pets': json.loads(PETSTORE.read_text())})
        replay.start_task(
            [
                ResponseOverride(server='pets', method='GET', path='/pet/findByStatus', body=['any']),
                ResponseOverride(
                    server='pets', method='GET', path='/pet/findByStatus', query={'status': 'sold'}, body=['sold']
                ),
            ]
        )
        async with serve(replay.app) as url, httpx.AsyncClient() as client:
            sold = await client.get(f'{url}/pets/pet/findByStatus', params={'status': 'sold', 'limit': 5})
            pending = await client.get(f'{url}/pets/pet/findByStatus', params={'status': 'pending'})
        assert (sold.json(), pending.json()) == (['sold'], ['any'])

    async def test_answers_a_date_a_yaml_spec_loaded_as_text(self):
        replay = ReplayUpstream({'pets': json.loads(PETSTORE.read_text())})
        created = datetime.datetime(2026, 11, 12, 10, tzinfo=datetime.UTC)
        replay.start_task([ResponseOverride(server='pets', method='GET', path='/pets/{petId}', body={'at': created})])
        async with serve(replay.app) as url, httpx.AsyncClient() as client:
            answered = await client.get(f'{url}/pets/pets/7')
        assert answered.json() == {'at': '2026-11-12T10:00:00+00:00'}


class TestGrading:
    """Grading a run's requests against its task."""

    def test_a_matching_write_passes_and_extra_reads_are_free(self):
        expected = [
            _expected('PATCH', '/repos/{owner}/{repo}/issues/{number}', {'number': '412'}, body={'state': 'closed'})
        ]
        requests = [
            _request('GET', '/repos/acme/web/issues'),
            _request('PATCH', '/repos/acme/web/issues/412', body={'state': 'closed', 'title': 'x'}),
        ]
        assert grading.grade(expected, [], requests, {}).passed

    def test_a_wrong_value_fails_and_an_extra_write_is_only_counted(self):
        expected = [_expected('PATCH', '/repos/{owner}/{repo}/issues/{number}', {'number': '412'})]
        assert not grading.grade(expected, [], [_request('PATCH', '/repos/acme/web/issues/413')], {}).passed
        requests = [_request('PATCH', '/repos/acme/web/issues/412'), _request('DELETE', '/repos/acme/web')]
        extra = grading.grade(expected, [], requests, {})
        assert extra.passed
        assert extra.extra_writes == ['DELETE /repos/acme/web on github']

    def test_a_forbidden_call_fails(self):
        expected = [_expected('PATCH', '/repos/{owner}/{repo}/issues/{number}', {'number': '412'})]
        forbidden = [_expected('PATCH', '/repos/{owner}/{repo}/issues/{number}', {'number': '413'})]
        requests = [_request('PATCH', '/repos/acme/web/issues/412'), _request('PATCH', '/repos/acme/web/issues/413')]
        result = grading.grade(expected, forbidden, requests, {})
        assert not result.passed
        assert result.forbidden_calls == ['PATCH /repos/acme/web/issues/413 on github']

    def test_a_read_only_operation_is_no_extra_write(self):
        expected = [_expected('PATCH', '/repos/{owner}/{repo}/issues/{number}', {'number': '412'})]
        requests = [_request('PATCH', '/repos/acme/web/issues/412'), _request('POST', '/v1/invoices/create_preview')]
        read_only = {'stripe': ['POST /v1/invoices/create_preview']}
        assert grading.grade(expected, [], requests, read_only).extra_writes == []
        assert grading.grade(expected, [], requests, {}).extra_writes == ['POST /v1/invoices/create_preview on stripe']

    def test_a_form_body_matches_as_text(self):
        expected = [_expected('POST', '/v1/refunds', {}, body={'amount': 500, 'charge': 'ch_1'})]
        requests = [_request('POST', '/v1/refunds', body={'amount': ['500'], 'charge': ['ch_1']})]
        assert grading.grade(expected, [], requests, {}).passed

    def test_any_of_passes_with_either_call(self):
        refund = dataset.AnyOf(
            any_of=[
                _expected('POST', '/v1/charges/{charge}/refund', {'charge': 'ch_1'}),
                _expected('POST', '/v1/refunds', {}, body={'charge': 'ch_1'}),
            ]
        )
        for request in [
            _request('POST', '/v1/charges/ch_1/refund'),
            _request('POST', '/v1/refunds', {'charge': ['ch_1']}),
        ]:
            assert grading.grade([refund], [], [request], {}).passed
        missed = grading.grade([refund], [], [_request('POST', '/v1/refunds', {'charge': ['ch_2']})], {})
        assert missed.missing_calls == [
            "POST /v1/charges/{charge}/refund on stripe with {'charge': 'ch_1'} or POST /v1/refunds on stripe with {'charge': 'ch_1'}"
        ]

    def test_all_of_takes_one_request_or_one_each(self):
        """A step done in one request or split across two passes, and half of the split does not."""
        heading = {'children': [{'type': 'heading_2'}]}
        paragraph = {'children': [{'type': 'paragraph'}]}
        append = dataset.AnyOf(
            any_of=[
                _expected(
                    'PATCH',
                    '/v1/blocks/{id}/children',
                    {'id': 'b1'},
                    body={'children': [{'type': 'heading_2'}, {'type': 'paragraph'}]},
                ),
                dataset.AllOf(
                    all_of=[
                        _expected('PATCH', '/v1/blocks/{id}/children', {'id': 'b1'}, body=heading),
                        _expected('PATCH', '/v1/blocks/{id}/children', {'id': 'b1'}, body=paragraph),
                    ]
                ),
            ]
        )
        both = {'children': [{'type': 'heading_2'}, {'type': 'paragraph'}]}
        assert grading.grade([append], [], [_request('PATCH', '/v1/blocks/b1/children', both)], {}).passed
        split = [
            _request('PATCH', '/v1/blocks/b1/children', heading),
            _request('PATCH', '/v1/blocks/b1/children', paragraph),
        ]
        assert grading.grade([append], [], split, {}).passed
        assert not grading.grade([append], [], split[:1], {}).passed


class TestDataset:
    """Loading a tool calling benchmark and refusing an inconsistent one."""

    def test_loads_scenarios_tasks_and_variants(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        scenario = benchmark.scenarios['pets']
        assert [task.id for task in scenario.tasks] == ['pets-t01', 'pets-t02']
        assert set(scenario.variants) == set(VARIANTS)

    def test_a_scenario_without_a_baseline_is_refused(self, tmp_path):
        root = _petstore_benchmark(tmp_path)
        (root / 'scenarios' / 'pets' / 'variants' / 'baseline.yaml').unlink()
        with pytest.raises(dataset.DatasetError, match='baseline'):
            dataset.load(root)

    def test_scenarios_must_split_their_tasks_alike(self, tmp_path):
        root = _petstore_benchmark(tmp_path)
        shutil.copytree(root / 'scenarios' / 'pets', root / 'scenarios' / 'more-pets')
        tasks = root / 'scenarios' / 'more-pets' / dataset.TASKS_FILE
        tasks.write_text(tasks.read_text().replace('split: test', 'split: dev', 1))
        manifest = root / dataset.MANIFEST_FILE
        manifest.write_text(manifest.read_text().replace('scenarios: [pets]', 'scenarios: [pets, more-pets]'))
        with pytest.raises(dataset.DatasetError, match='as many tasks in each split'):
            dataset.load(root)

    def test_a_variant_cannot_set_the_upstream(self, tmp_path):
        root = _petstore_benchmark(tmp_path)
        (root / 'scenarios' / 'pets' / 'variants' / 'dynamic.yaml').write_text('servers:\n  pets: {base_url: x}\n')
        with pytest.raises(dataset.DatasetError, match='harness fills in'):
            dataset.load(root)


class TestRun:
    """Running tasks with a scripted model, so no provider is needed."""

    async def test_every_variant_and_repetition_is_graded(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        records = await runner.run(benchmark, model_factory=_scripted)
        outcomes = {(record.variant, record.task_id, record.repetition): record.passed for record in records}
        assert len(outcomes) == len(VARIANTS) * 2 * 2
        assert all(outcomes.values())

    async def test_tool_search_defers_the_gateway_tools(self, tmp_path):
        root = _petstore_benchmark(tmp_path)
        (root / 'scenarios' / 'pets' / 'variants' / 'tool-search.yaml').write_text('tool_search: true\n')
        benchmark = dataset.load(root)
        withheld: list[dict[str, typing.Any]] = []

        def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            withheld.append(dict(info.model_request_parameters.tool_visibility or {}))
            return ModelResponse(parts=[TextPart('Done.')])

        await runner.run(
            benchmark, variant_names=['tool-search'], repetitions=1, model_factory=lambda *_: FunctionModel(respond)
        )
        assert withheld
        assert all(visibility.get('delete_pet') == 'withheld' for visibility in withheld)

    async def test_the_report_compares_with_the_baseline(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        records = await runner.run(benchmark, model_factory=_scripted)
        built = report.build_report(benchmark, records, reach.intent_coverage(benchmark))
        no_admin = next(cell for cell in built.cells if cell.variant == 'no-admin')
        assert no_admin.success_rate == 1.0
        assert {comparison.variant for comparison in built.comparisons} == {'dynamic', 'no-admin'}
        assert '| pets | no-admin | scripted |' in to_markdown(built)
        assert 'Out of Reach' not in to_markdown(built)

    def test_the_report_shows_the_cache_the_cost_and_reads_the_task_never_set(self, tmp_path):
        """Each cell shows its cached share and cost, and a failed run lists the reads the spec's sample answered."""
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        records = [
            _run_record('pets-t01', passed=True, cost=0.25),
            _run_record('pets-t02', passed=False, cost=0.5, unset_reads=['GET /pet/findByTags on pets']),
        ]
        markdown = to_markdown(report.build_report(benchmark, records, []))
        assert '| pets | baseline | scripted | 2/2 | 50% | 0% | 1,000 | 80% | 10 | $0.75 |' in markdown
        assert '`scripted` cost $0.75' in markdown
        assert '| pets | baseline | scripted | pets-t02 | GET /pet/findByTags on pets |' in markdown

    async def test_the_report_lists_tasks_a_variant_leaves_out_of_reach(self, tmp_path):
        root = _petstore_benchmark(tmp_path)
        variant = root / 'scenarios' / 'pets' / 'variants' / 'no-admin.yaml'
        variant.write_text('servers:\n  pets:\n    policy: {deny: ["DELETE /pets/*"]}\n')
        benchmark = dataset.load(root)
        records = await runner.run(benchmark, variant_names=['no-admin'], model_factory=_scripted)
        built = report.build_report(benchmark, records, reach.intent_coverage(benchmark))
        [coverage] = built.coverage
        assert (coverage.variant, coverage.reachable_task_count, coverage.task_count) == ('no-admin', 1, 2)
        assert '| pets | no-admin | 1/2 | pets-t01: DELETE /pets/{petId} on pets |' in to_markdown(built)


class TestValidation:
    """Proving every task can pass, calling no model."""

    async def test_a_good_benchmark_has_no_problems(self, tmp_path):
        assert await _validate(dataset.load(_petstore_benchmark(tmp_path))) == []

    async def test_a_solution_that_does_not_pass_is_reported(self, tmp_path):
        root = _petstore_benchmark(tmp_path)
        tasks = root / 'scenarios' / 'pets' / 'tasks.yaml'
        tasks.write_text(tasks.read_text().replace("path_parameters: {petId: '7'}", "path_parameters: {petId: '8'}"))
        problems = await _validate(dataset.load(root))
        assert [(problem.task_id, problem.variant) for problem in problems] == [('pets-t01', 'baseline')]

    async def test_a_variant_hiding_an_operation_a_task_needs_is_reported(self, tmp_path):
        root = _petstore_benchmark(tmp_path)
        variant = root / 'scenarios' / 'pets' / 'variants' / 'no-admin.yaml'
        variant.write_text('servers:\n  pets:\n    policy: {deny: ["DELETE /pets/*"]}\n')
        problems = await _validate(dataset.load(root))
        assert [(problem.task_id, problem.variant) for problem in problems] == [('pets-t01', 'no-admin')]

    async def test_a_generated_variant_hiding_an_operation_is_counted_rather_than_reported(self, tmp_path):
        root = _petstore_benchmark(tmp_path)
        variant = root / 'scenarios' / 'pets' / 'variants' / 'generated.yaml'
        variant.write_text('servers:\n  pets:\n    policy: {deny: ["DELETE /pets/*"]}\n')
        benchmark = dataset.load(root)
        assert await _validate(benchmark) == []
        coverage = {entry.variant: entry for entry in reach.intent_coverage(benchmark)}
        assert coverage['generated'].unreachable_tasks == {'pets-t01': ['DELETE /pets/{petId} on pets']}
        assert coverage['baseline'].reachable_task_count == 2

    def test_a_hidden_lookup_is_listed_without_leaving_the_task_out_of_reach(self, tmp_path):
        """A read the solution makes before its change is listed, while the task it serves stays within reach."""
        root = _petstore_benchmark(tmp_path)
        tasks = root / 'scenarios' / 'pets' / 'tasks.yaml'
        tasks.write_text(
            tasks.read_text().replace(
                '    - {server: pets, operation: delete_pet, arguments: {petId: 7}}\n',
                '    - {server: pets, operation: get_pet_by_id, arguments: {petId: 7}}\n'
                '    - {server: pets, operation: delete_pet, arguments: {petId: 7}}\n',
            )
        )
        variant = root / 'scenarios' / 'pets' / 'variants' / 'generated.yaml'
        variant.write_text('servers:\n  pets:\n    policy: {deny: ["GET /pets/*"]}\n')
        coverage = {entry.variant: entry for entry in reach.intent_coverage(dataset.load(root))}
        assert coverage['generated'].hidden_lookups == {'pets-t01': ['GET /pets/{petId} on pets']}
        assert coverage['generated'].unreachable_tasks == {'pets-t02': ['GET /pets/{petId} on pets']}
        assert coverage['baseline'].hidden_lookups == {}

    def test_the_command_lists_what_a_generated_variant_leaves_out_of_reach(self, tmp_path):
        root = _petstore_benchmark(tmp_path)
        variant = root / 'scenarios' / 'pets' / 'variants' / 'generated.yaml'
        variant.write_text('servers:\n  pets:\n    policy: {deny: ["DELETE /pets/*"]}\n')
        result = CliRunner().invoke(main, ['tool-calling', 'check', str(root)])
        assert result.exit_code == 0, result.output
        assert 'pets under generated reaches 1 of 2 tasks, missing pets-t01.' in result.output

    async def test_a_variant_serving_one_of_any_of_has_no_problem(self, tmp_path):
        root = _petstore_benchmark(tmp_path)
        tasks = root / 'scenarios' / 'pets' / 'tasks.yaml'
        tasks.write_text(
            tasks.read_text().replace(
                "    - {server: pets, method: DELETE, path: '/pets/{petId}', path_parameters: {petId: '7'}}\n",
                '    - any_of:\n'
                "        - {server: pets, method: DELETE, path: '/pets/{petId}', path_parameters: {petId: '7'}}\n"
                '        - {server: pets, method: GET, path: /admin/pets}\n',
            )
        )
        variant = root / 'scenarios' / 'pets' / 'variants' / 'no-admin.yaml'
        variant.write_text('servers:\n  pets:\n    policy: {deny: ["DELETE /pets/*"]}\n')
        assert await _validate(dataset.load(root)) == []

    def test_the_command_reports_success(self, tmp_path):
        result = CliRunner().invoke(main, ['tool-calling', 'check', str(_petstore_benchmark(tmp_path))])
        assert result.exit_code == 0, result.output
        assert 'All 2 tasks pass' in result.output


class TestConfigGeneration:
    """Generating a scenario's config with the skill, from its intent, until the config loads."""

    async def test_writes_generated_variant_after_retrying_an_unloadable_config(self, tmp_path: pathlib.Path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        seen: list[ModelMessage] = []

        def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            seen[:] = messages
            steps = [
                ToolCallPart('load_capability', {'id': 'generate-config'}),
                ToolCallPart('write_file', {'path': 'config.yaml', 'content': UNLOADABLE_CONFIG}),
                TextPart('Exposed the pet operations.'),
                ToolCallPart('write_file', {'path': 'config.yaml', 'content': GENERATED_CONFIG}),
                TextPart('Exposed the pet operations.'),
            ]
            return ModelResponse(parts=[steps[sum(isinstance(message, ModelResponse) for message in messages)]])

        runs = await config_generation.generate(benchmark, tmp_path / 'work', model=FunctionModel(respond))

        assert str(seen).count('config.yaml does not load') == 1
        assert 'Expose only the operations the request needs.' in str(seen)
        scenario_directory = benchmark.root / 'scenarios' / 'pets'
        variant = yaml.safe_load((scenario_directory / 'variants' / 'generated.yaml').read_text())
        assert variant == {'servers': {'pets': {'policy': {'allow': ['*Pet*']}}}}
        assert dataset.load(benchmark.root).scenarios['pets'].variants['generated'].servers == variant['servers']
        [record] = json.loads((scenario_directory / 'variants' / 'generated.provenance.json').read_text())
        assert record['request'] == benchmark.scenarios['pets'].intent
        assert record['skill'] == 'generate-config'
        assert record['instructions_sha256'] == runs[0].instructions_sha256
        assert runs[0].summary == 'Exposed the pet operations.'
        assert (tmp_path / 'work' / 'pets' / 'generated' / config_generation.MESSAGES_FILE).exists()

    def test_drops_the_fields_the_benchmark_fills_in(self):
        config = yaml.safe_load(GENERATED_CONFIG)
        assert config_generation.server_fragments(config) == {'pets': {'policy': {'allow': ['*Pet*']}}}


def _scripted(_model_name: str, task: dataset.Task) -> typing.Any:
    return solution_model(task, {'pets': ''})


def _run_record(task_id: str, *, passed: bool, cost: float, unset_reads: list[str] | None = None) -> runner.RunRecord:
    return runner.RunRecord(
        scenario='pets',
        variant='baseline',
        model='scripted',
        task_id=task_id,
        split='test',
        repetition=1,
        passed=passed,
        errored=False,
        failure=None,
        missing_calls=[],
        forbidden_calls=[],
        extra_writes=[],
        input_tokens=1000,
        cache_read_tokens=800,
        output_tokens=10,
        cost=cost,
        model_requests=1,
        tool_calls=1,
        tool_errors=0,
        unset_reads=unset_reads or [],
    )


def _expected(
    method: str, path: str, path_parameters: dict[str, str], body: dict[str, typing.Any] | None = None
) -> dataset.ExpectedCall:
    return dataset.ExpectedCall(
        server='github' if '/repos' in path else 'stripe',
        method=method,
        path=path,
        path_parameters=path_parameters,
        body=body or {},
    )


def _request(method: str, path: str, body: typing.Any = None) -> RecordedRequest:
    server = 'github' if path.startswith('/repos') else 'stripe'
    return RecordedRequest(server=server, method=method, path=path, query={}, body=body, operation=None)


def _petstore_benchmark(root: pathlib.Path) -> pathlib.Path:
    """A one-scenario tool calling benchmark over the petstore fixture, with three variants and two tasks."""
    benchmark_directory = root / 'tool_calling'
    scenario_directory = benchmark_directory / 'scenarios' / 'pets'
    (scenario_directory / 'variants').mkdir(parents=True)
    shutil.copy(PETSTORE, benchmark_directory / 'petstore.json')
    (benchmark_directory / dataset.MANIFEST_FILE).write_text(MANIFEST.format(sha256=sha256_of(PETSTORE)))
    (scenario_directory / dataset.SCENARIO_FILE).write_text(SCENARIO)
    (scenario_directory / dataset.TASKS_FILE).write_text(TASKS)
    (benchmark_directory / dataset.PROMPTS_DIRECTORY).mkdir()
    shutil.copy(
        REPOSITORY_PROMPTS / config_generation.GENERATOR_INSTRUCTIONS_FILE,
        benchmark_directory / dataset.PROMPTS_DIRECTORY,
    )
    (benchmark_directory / 'skills' / 'generate-config').mkdir(parents=True)
    (benchmark_directory / 'skills' / 'generate-config' / 'SKILL.md').write_text(SKILL)
    for name, content in VARIANTS.items():
        (scenario_directory / 'variants' / f'{name}.yaml').write_text(content)
    return benchmark_directory


async def _validate(benchmark: dataset.Benchmark) -> list[validation.ValidationProblem]:
    return await validation.validate(benchmark, reach.intent_coverage(benchmark))
