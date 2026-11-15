import re

import pydantic

from ..gateway_connection import list_operations
from .dataset import BASELINE_VARIANT, Benchmark, ExpectedCall, Scenario
from .harness import ScenarioRun
from .solution import solution_model


PATH_PARAMETER = re.compile(r'\{[^}/]+\}')


class ValidationProblem(pydantic.BaseModel):
    """One way a scenario's data fails to hold together."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    scenario: str
    task_id: str
    variant: str
    problem: str


async def validate(benchmark: Benchmark) -> list[ValidationProblem]:
    """Prove every task can pass and every variant still reaches what an in-intent task needs, calling no model.

    Each task's solution runs under the baseline through the real gateway and the replay upstream, and has to pass
    the grader, so a task can never expect a call the gateway cannot make or the grader cannot see.
    A variant that hides an operation an in-intent task needs is a problem in that variant's config.
    An out-of-intent task may lose its operation to a variant, since showing that cost is what it is for.
    """
    spec_paths = {name: str(pin.fetch(benchmark.root)) for name, pin in benchmark.manifest.specs.items()}
    problems = []
    for scenario in benchmark.scenarios.values():
        scenario_run = ScenarioRun(benchmark, scenario, spec_paths)
        problems.extend(await _validate_solutions(scenario_run))
        problems.extend(_validate_reach(scenario_run))
    return problems


async def _validate_solutions(scenario_run: ScenarioRun) -> list[ValidationProblem]:
    scenario = scenario_run.scenario
    prefixes = {server: scenario_run.tool_prefix(server) for server in scenario.servers}
    problems = []
    async with scenario_run.serving(), scenario_run.gateway(BASELINE_VARIANT) as mcp_urls:
        for task in scenario.tasks:
            agent = scenario_run.agent(solution_model(task, prefixes), None, mcp_urls)
            outcome = await scenario_run.run_task(agent, task)
            if outcome.tool_errors:
                problems.append(
                    _problem(
                        scenario,
                        task.id,
                        BASELINE_VARIANT,
                        f'the solution made {outcome.tool_errors} failing tool calls',
                    )
                )
            if not outcome.grade.passed:
                detail = '; '.join(outcome.grade.missing_calls + outcome.grade.unexpected_writes)
                problems.append(_problem(scenario, task.id, BASELINE_VARIANT, f'the solution does not pass: {detail}'))
    return problems


def _validate_reach(scenario_run: ScenarioRun) -> list[ValidationProblem]:
    scenario = scenario_run.scenario
    problems = []
    for variant in scenario.variants:
        config = scenario_run.gateway_config(variant)
        served = {
            server.name: [(listing.method, listing.path) for listing in list_operations(server)]
            for server in config.servers
        }
        for task in scenario.tasks:
            if not task.in_intent:
                continue
            for expected in task.expected_calls:
                if not any(_same_operation(expected, method, path) for method, path in served[expected.server]):
                    problems.append(
                        _problem(
                            scenario,
                            task.id,
                            variant,
                            f'{expected.method} {expected.path} is not served on {expected.server}',
                        )
                    )
    return problems


def _same_operation(expected: ExpectedCall, method: str, template: str) -> bool:
    """Whether ``expected`` names the operation at ``method`` and ``template``, whatever its placeholders are called.

    An expected call names its path as a template or as a concrete path, so either one is tried.
    """
    if expected.method != method:
        return False
    if PATH_PARAMETER.sub('{}', expected.path) == PATH_PARAMETER.sub('{}', template):
        return True
    pattern = '^' + '[^/]+'.join(re.escape(part) for part in PATH_PARAMETER.split(template)) + '/?$'
    return re.match(pattern, expected.path) is not None


def _problem(scenario: Scenario, task_id: str, variant: str, problem: str) -> ValidationProblem:
    return ValidationProblem(scenario=scenario.name, task_id=task_id, variant=variant, problem=problem)
