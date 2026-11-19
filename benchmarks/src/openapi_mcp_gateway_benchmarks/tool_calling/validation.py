import typing

import pydantic
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from openapi_mcp_gateway.exposure.meta_tool import CALL_OPERATION_TOOL

from .dataset import BASELINE_VARIANT, Benchmark, Scenario, SolutionCall, Task
from .harness import ScenarioRun
from .reach import IntentCoverage


class ValidationProblem(pydantic.BaseModel):
    """One way a scenario's data fails to hold together."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    scenario: str
    task_id: str
    variant: str
    problem: str


async def validate(benchmark: Benchmark, coverage: typing.Sequence[IntentCoverage]) -> list[ValidationProblem]:
    """Prove every task can pass and every hand-written variant reaches what each task needs, calling no model.

    Each task's solution runs under the baseline through the real gateway and the replay upstream,
    and has to pass the grader, so a task can never expect a call the gateway cannot make or the grader cannot see.
    Every task is one the scenario's intent names, so a hand-written variant hiding an operation a task needs is faulty.
    A generated variant is the generator's to get right, so what it leaves out is measured as intent coverage instead.
    ``coverage`` is ``reach.intent_coverage`` for the benchmark, which the caller reuses for the generated variants.
    """
    spec_paths = {name: str(pin.fetch(benchmark.root)) for name, pin in benchmark.manifest.specs.items()}
    problems = []
    for scenario in benchmark.scenarios.values():
        problems.extend(await _validate_solutions(ScenarioRun(benchmark, scenario, spec_paths)))
    problems.extend(_validate_reach(benchmark, coverage))
    return problems


def solution_model(task: Task, tool_prefixes: typing.Mapping[str, str]) -> FunctionModel:
    """A model that makes ``task``'s solution calls in one turn and then stops, needing no provider.

    It calls an operation's own tool where the variant exposes one, and ``call_operation`` where the variant is dynamic,
    so the same solution proves a task passes under static and dynamic exposure alike.
    """

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        answered = any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts)
        if answered:
            return ModelResponse(parts=[TextPart('Done.')])
        tool_names = {tool.name for tool in info.function_tools}
        calls = [_tool_call(call, tool_prefixes.get(call.server, ''), tool_names) for call in task.solution]
        if None in calls:
            # A variant that hides an operation leaves the model nothing to call, so the run ends and fails its grade.
            return ModelResponse(parts=[TextPart('No tool reaches what this task needs.')])
        return ModelResponse(parts=[call for call in calls if call is not None])

    return FunctionModel(respond)


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


def _validate_reach(benchmark: Benchmark, coverage: typing.Sequence[IntentCoverage]) -> list[ValidationProblem]:
    """Each step a hand-written variant leaves out of reach, which for a generated one the report counts instead."""
    generated = set(benchmark.manifest.config_generator.variants)
    return [
        ValidationProblem(
            scenario=entry.scenario, task_id=task_id, variant=entry.variant, problem=f'{step} is not served'
        )
        for entry in coverage
        if entry.variant not in generated
        for task_id, steps in entry.unreachable_tasks.items()
        for step in steps
    ]


def _problem(scenario: Scenario, task_id: str, variant: str, problem: str) -> ValidationProblem:
    return ValidationProblem(scenario=scenario.name, task_id=task_id, variant=variant, problem=problem)


def _tool_call(call: SolutionCall, prefix: str, tool_names: set[str]) -> ToolCallPart | None:
    if prefix + call.operation in tool_names:
        return ToolCallPart(tool_name=prefix + call.operation, args=call.arguments)
    if prefix + CALL_OPERATION_TOOL in tool_names:
        return ToolCallPart(
            tool_name=prefix + CALL_OPERATION_TOOL, args={'name': call.operation, 'arguments': call.arguments}
        )
    return None
