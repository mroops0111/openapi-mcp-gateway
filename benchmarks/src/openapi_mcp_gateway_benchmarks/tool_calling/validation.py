import pydantic

from .dataset import BASELINE_VARIANT, Benchmark, Scenario
from .harness import ScenarioRun
from .reach import unreached_steps
from .solution import solution_model


class ValidationProblem(pydantic.BaseModel):
    """One way a scenario's data fails to hold together."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    scenario: str
    task_id: str
    variant: str
    problem: str


async def validate(benchmark: Benchmark) -> list[ValidationProblem]:
    """Prove every task can pass and every hand-written variant reaches what each task needs, calling no model.

    Each task's solution runs under the baseline through the real gateway and the replay upstream,
    and has to pass the grader, so a task can never expect a call the gateway cannot make or the grader cannot see.
    Every task is one the scenario's intent names, so a hand-written variant hiding an operation a task needs is faulty.
    A generated variant is the generator's to get right, so what it leaves out is measured as intent coverage instead.
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
            agent = scenario_run.agent(solution_model(task, prefixes), None, BASELINE_VARIANT, mcp_urls)
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
                detail = '; '.join(outcome.grade.missing_calls + outcome.grade.forbidden_calls)
                problems.append(_problem(scenario, task.id, BASELINE_VARIANT, f'the solution does not pass: {detail}'))
            elif outcome.grade.extra_writes:
                detail = '; '.join(outcome.grade.extra_writes)
                problems.append(_problem(scenario, task.id, BASELINE_VARIANT, f'the solution writes more: {detail}'))
    return problems


def _validate_reach(scenario_run: ScenarioRun) -> list[ValidationProblem]:
    """Each task a hand-written variant leaves out of reach, which for a generated one the report counts instead."""
    scenario = scenario_run.scenario
    generated = set(scenario_run.benchmark.manifest.config_generator.variants)
    return [
        _problem(scenario, task_id, variant, f'{step} is not served')
        for variant in scenario.variants
        if variant not in generated
        for task_id, steps in unreached_steps(scenario_run, variant).items()
        for step in steps
    ]


def _problem(scenario: Scenario, task_id: str, variant: str, problem: str) -> ValidationProblem:
    return ValidationProblem(scenario=scenario.name, task_id=task_id, variant=variant, problem=problem)
