import re
import typing

import pydantic

from ..gateway_connection import list_operations
from .dataset import BASELINE_VARIANT, Benchmark, ExpectedCall
from .grading import READ_METHODS
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
    # Each task, by id, to the reads its solution makes that the variant does not serve, such as looking up an id.
    # A model may reach the same fact another way, so these leave the task within reach.
    hidden_lookups: dict[str, list[str]] = pydantic.Field(default_factory=dict)


def intent_coverage(
    benchmark: Benchmark, *, scenarios: typing.Sequence[str] = (), variants: typing.Sequence[str] = ()
) -> list[IntentCoverage]:
    """Each scenario's tasks a variant serves, for the named scenarios and variants or else all of them, calling no model."""
    spec_paths = {name: str(pin.fetch(benchmark.root)) for name, pin in benchmark.manifest.specs.items()}
    coverage = []
    for scenario in benchmark.scenarios.values():
        if scenarios and scenario.name not in scenarios:
            continue
        scenario_run = ScenarioRun(benchmark, scenario, spec_paths)
        for variant in scenario.variants:
            if variants and variant not in variants:
                continue
            unreachable = unreached_steps(scenario_run, variant)
            coverage.append(
                IntentCoverage(
                    scenario=scenario.name,
                    variant=variant,
                    task_count=len(scenario.tasks),
                    reachable_task_count=len(scenario.tasks) - len(unreachable),
                    unreachable_tasks=unreachable,
                    hidden_lookups=hidden_lookups(scenario_run, variant),
                )
            )
    return coverage


def unreached_steps(scenario_run: ScenarioRun, variant: str) -> dict[str, list[str]]:
    """Each task with a step ``variant`` serves no operation for, by id, to those steps.

    A step with alternatives is reached when the variant serves every call of any one of them.
    """
    served = _served(scenario_run, variant)
    unreached: dict[str, list[str]] = {}
    for task in scenario_run.scenario.tasks:
        for step in task.expected_calls:
            if not any(
                all(
                    any(_same_operation(call, method, path) for method, path in served[call.server])
                    for call in alternative
                )
                for alternative in step.alternatives
            ):
                unreached.setdefault(task.id, []).append(
                    ' or '.join(
                        ' and '.join(f'{call.method} {call.path} on {call.server}' for call in alternative)
                        for alternative in step.alternatives
                    )
                )
    return unreached


def hidden_lookups(scenario_run: ScenarioRun, variant: str) -> dict[str, list[str]]:
    """Each task whose solution reads through an operation ``variant`` does not serve, by id, to those reads.

    Only the reads the task does not grade count, such as looking up the id that the graded write needs.

    The solution names each operation as the baseline lists it, so the baseline maps the name to its method and path.
    """
    served = _served(scenario_run, variant)
    baseline = {
        (server.name, listing.name): (listing.method, listing.path)
        for server in scenario_run.gateway_config(BASELINE_VARIANT).servers
        for listing in list_operations(server)
    }
    hidden: dict[str, list[str]] = {}
    for task in scenario_run.scenario.tasks:
        graded = [call for step in task.expected_calls for alternative in step.alternatives for call in alternative]
        for call in task.solution:
            method, path = baseline[(call.server, call.operation)]
            if method not in READ_METHODS or (method, path) in served[call.server]:
                continue
            # A read the task grades is already counted as out of reach.
            if any(expected.server == call.server and _same_operation(expected, method, path) for expected in graded):
                continue
            hidden.setdefault(task.id, []).append(f'{method} {path} on {call.server}')
    return hidden


def _served(scenario_run: ScenarioRun, variant: str) -> dict[str, list[tuple[str, str]]]:
    """Each server's operations ``variant`` serves, by server name, as method and path template."""
    return {
        server.name: [(listing.method, listing.path) for listing in list_operations(server)]
        for server in scenario_run.gateway_config(variant).servers
    }


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
