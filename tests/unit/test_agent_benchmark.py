import json
import pathlib
import shutil
import typing

import httpx
import pytest
from click.testing import CliRunner

from openapi_mcp_gateway.benchmarks.agent import dataset, grading, report, runner, validation
from openapi_mcp_gateway.benchmarks.agent.markdown import to_markdown
from openapi_mcp_gateway.benchmarks.agent.replay import RecordedRequest, ReplayUpstream, ResponseOverride
from openapi_mcp_gateway.benchmarks.agent.sampling import SchemaSampler
from openapi_mcp_gateway.benchmarks.agent.solution import solution_model
from openapi_mcp_gateway.benchmarks.cli import main
from openapi_mcp_gateway.benchmarks.local_server import serve
from openapi_mcp_gateway.benchmarks.spec_pins import sha256_of
from tests.conftest import FIXTURES_DIR


PETSTORE = FIXTURES_DIR / 'petstore.json'

MANIFEST = """\
version: 1
specs:
  pets: {{source: petstore.json, sha256: {sha256}}}
models:
  scripted: {{model: test}}
scenarios: [pets]
repetitions: 2
system_prompt: Carry out the request with the tools.
"""

SCENARIO = """\
intent: "Keep the shop's pet records current."
servers: {pets: pets}
"""

TASKS = """\
- id: pets-t01
  prompt: Pet 7 was adopted, take it off the list.
  split: final
  in_intent: true
  expected_calls:
    - {server: pets, method: DELETE, path: '/pets/{petId}', path_parameters: {petId: '7'}}
  solution:
    - {server: pets, operation: delete_pet, arguments: {petId: 7}}
- id: pets-t02
  prompt: Show me the admin view of the pets.
  split: final
  in_intent: false
  expected_calls:
    - {server: pets, method: GET, path: /admin/pets}
  solution:
    - {server: pets, operation: listAdminPets, arguments: {}}
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
        assert [(request.method, request.operation) for request in replay.requests] == [
            ('GET', 'GET /pets/{petId}'),
            ('DELETE', 'DELETE /pets/{petId}'),
            ('GET', None),
        ]


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
        assert grading.grade(expected, requests).passed

    def test_a_wrong_value_or_an_extra_write_fails(self):
        expected = [_expected('PATCH', '/repos/{owner}/{repo}/issues/{number}', {'number': '412'})]
        assert not grading.grade(expected, [_request('PATCH', '/repos/acme/web/issues/413')]).passed
        extra = grading.grade(
            expected, [_request('PATCH', '/repos/acme/web/issues/412'), _request('DELETE', '/repos/acme/web')]
        )
        assert extra.unexpected_writes == ['DELETE /repos/acme/web on github']

    def test_a_form_body_matches_as_text(self):
        expected = [_expected('POST', '/v1/refunds', {}, body={'amount': 500, 'charge': 'ch_1'})]
        requests = [_request('POST', '/v1/refunds', body={'amount': ['500'], 'charge': ['ch_1']})]
        assert grading.grade(expected, requests).passed


class TestDataset:
    """Loading an agent benchmark and refusing an inconsistent one."""

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
        assert all(passed for (variant, task, _), passed in outcomes.items() if task == 'pets-t01')
        assert outcomes[('no-admin', 'pets-t02', 1)] is False

    async def test_the_report_compares_with_the_baseline(self, tmp_path):
        benchmark = dataset.load(_petstore_benchmark(tmp_path))
        built = report.build_report(benchmark, await runner.run(benchmark, model_factory=_scripted))
        no_admin = next(cell for cell in built.cells if cell.variant == 'no-admin')
        assert (no_admin.in_intent_success_rate, no_admin.out_of_intent_success_rate) == (1.0, 0.0)
        assert {comparison.variant for comparison in built.comparisons} == {'dynamic', 'no-admin'}
        assert '| pets | no-admin | scripted |' in to_markdown(built)


class TestValidation:
    """Proving every task can pass, calling no model."""

    async def test_a_good_benchmark_has_no_problems(self, tmp_path):
        assert await validation.validate(dataset.load(_petstore_benchmark(tmp_path))) == []

    async def test_a_solution_that_does_not_pass_is_reported(self, tmp_path):
        root = _petstore_benchmark(tmp_path)
        tasks = root / 'scenarios' / 'pets' / 'tasks.yaml'
        tasks.write_text(tasks.read_text().replace("path_parameters: {petId: '7'}", "path_parameters: {petId: '8'}"))
        problems = await validation.validate(dataset.load(root))
        assert [(problem.task_id, problem.variant) for problem in problems] == [('pets-t01', 'baseline')]

    async def test_a_variant_hiding_an_in_intent_operation_is_reported(self, tmp_path):
        root = _petstore_benchmark(tmp_path)
        variant = root / 'scenarios' / 'pets' / 'variants' / 'no-admin.yaml'
        variant.write_text('servers:\n  pets:\n    policy: {deny: ["DELETE /pets/*"]}\n')
        problems = await validation.validate(dataset.load(root))
        assert [(problem.task_id, problem.variant) for problem in problems] == [('pets-t01', 'no-admin')]

    def test_the_command_reports_success(self, tmp_path):
        result = CliRunner().invoke(main, ['agent', 'validate', str(_petstore_benchmark(tmp_path))])
        assert result.exit_code == 0, result.output
        assert 'All 2 tasks pass' in result.output


def _scripted(_model_name: str, task: dataset.Task) -> typing.Any:
    return solution_model(task, {'pets': ''})


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
    """A one-scenario agent benchmark over the petstore fixture, with three variants and two tasks."""
    benchmark_directory = root / 'agent'
    scenario_directory = benchmark_directory / 'scenarios' / 'pets'
    (scenario_directory / 'variants').mkdir(parents=True)
    shutil.copy(PETSTORE, benchmark_directory / 'petstore.json')
    (benchmark_directory / dataset.MANIFEST_FILE).write_text(MANIFEST.format(sha256=sha256_of(PETSTORE)))
    (scenario_directory / dataset.SCENARIO_FILE).write_text(SCENARIO)
    (scenario_directory / dataset.TASKS_FILE).write_text(TASKS)
    for name, content in VARIANTS.items():
        (scenario_directory / 'variants' / f'{name}.yaml').write_text(content)
    return benchmark_directory
