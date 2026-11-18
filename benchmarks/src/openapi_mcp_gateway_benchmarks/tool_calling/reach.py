import re

import pydantic

from ..gateway_connection import list_operations
from .dataset import Benchmark, ExpectedCall
from .harness import ScenarioRun


PATH_PARAMETER = re.compile(r'\{[^}/]+\}')


class IntentCoverage(pydantic.BaseModel):
    """How many of a scenario's tasks a variant serves every operation for, which a generated config may fall short of.

    A task the variant leaves out of reach fails under any model, so the count bounds the variant's success rate.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    scenario: str
    variant: str
    task_count: int
    reachable_task_count: int
    # Each task out of reach, by id, to the steps the variant serves no operation for.
    unreachable_tasks: dict[str, list[str]]


def intent_coverage(benchmark: Benchmark) -> list[IntentCoverage]:
    """Each scenario's tasks a variant serves, for every variant of every scenario, calling no model."""
    spec_paths = {name: str(pin.fetch(benchmark.root)) for name, pin in benchmark.manifest.specs.items()}
    coverage = []
    for scenario in benchmark.scenarios.values():
        scenario_run = ScenarioRun(benchmark, scenario, spec_paths)
        for variant in scenario.variants:
            unreachable = unreached_steps(scenario_run, variant)
            coverage.append(
                IntentCoverage(
                    scenario=scenario.name,
                    variant=variant,
                    task_count=len(scenario.tasks),
                    reachable_task_count=len(scenario.tasks) - len(unreachable),
                    unreachable_tasks=unreachable,
                )
            )
    return coverage


def unreached_steps(scenario_run: ScenarioRun, variant: str) -> dict[str, list[str]]:
    """Each task with a step ``variant`` serves no operation for, by id, to those steps.

    A step with alternatives is reached when the variant serves any one of them.
    """
    config = scenario_run.gateway_config(variant)
    served = {
        server.name: [(listing.method, listing.path) for listing in list_operations(server)]
        for server in config.servers
    }
    unreached: dict[str, list[str]] = {}
    for task in scenario_run.scenario.tasks:
        for step in task.expected_calls:
            if not any(
                _same_operation(call, method, path)
                for call in step.alternatives
                for method, path in served[call.server]
            ):
                unreached.setdefault(task.id, []).append(
                    ' or '.join(f'{call.method} {call.path} on {call.server}' for call in step.alternatives)
                )
    return unreached


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
