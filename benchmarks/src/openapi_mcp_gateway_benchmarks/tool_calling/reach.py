import re
import typing

import pydantic

from ..gateway_connection import list_operations
from .dataset import BASELINE_VARIANT, Benchmark, ExpectedCall
from .grading import reads
from .harness import ScenarioRun


PATH_PARAMETER = re.compile(r'\{[^}/]+\}')

# Each server's operations, by server name, as method and path template, and the same keyed by listed name.
Served = dict[str, list[tuple[str, str]]]
Listed = dict[str, dict[str, tuple[str, str]]]


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
    """Each scenario's tasks a variant serves, calling no model.

    Only the named scenarios and variants are listed, or all of them when none are named.
    """
    spec_paths = {name: str(pin.fetch(benchmark.root)) for name, pin in benchmark.manifest.specs.items()}
    coverage = []
    for scenario in benchmark.scenarios.values():
        if scenarios and scenario.name not in scenarios:
            continue
        scenario_run = ScenarioRun(benchmark, scenario, spec_paths)
        # Listing a server's operations loads its spec, which takes a second for GitHub, so each listing is made once.
        baseline = _listed(scenario_run, BASELINE_VARIANT)
        for variant in scenario.variants:
            if variants and variant not in variants:
                continue
            listed = baseline if variant == BASELINE_VARIANT else _listed(scenario_run, variant)
            served = {server: list(operations.values()) for server, operations in listed.items()}
            unreachable = unreached_steps(scenario_run, served)
            coverage.append(
                IntentCoverage(
                    scenario=scenario.name,
                    variant=variant,
                    task_count=len(scenario.tasks),
                    reachable_task_count=len(scenario.tasks) - len(unreachable),
                    unreachable_tasks=unreachable,
                    hidden_lookups=hidden_lookups(scenario_run, served, baseline),
                )
            )
    return coverage


def unreached_steps(scenario_run: ScenarioRun, served: Served) -> dict[str, list[str]]:
    """Each task with a step a variant serves no operation for, by id, to those steps.

    ``served`` holds the variant's operations by server, as method and path template.
    A step with alternatives is reached when the variant serves every call of any one of them.
    """
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


def hidden_lookups(scenario_run: ScenarioRun, served: Served, baseline: Listed) -> dict[str, list[str]]:
    """Each task whose solution reads through an operation a variant does not serve, by id, to those reads.

    Only the reads the task does not grade count, such as looking up the id that the graded write needs.
    The solution names each operation as ``baseline`` lists it, which maps the name to its method and path.
    A name the baseline does not list is left to validation, which reports the solution failing.
    """
    read_only_operations = scenario_run.read_only_operations()
    hidden: dict[str, list[str]] = {}
    for task in scenario_run.scenario.tasks:
        graded = [call for step in task.expected_calls for alternative in step.alternatives for call in alternative]
        for call in task.solution:
            operation = baseline.get(call.server, {}).get(call.operation)
            if operation is None:
                continue
            method, path = operation
            if not reads(method, path, read_only_operations.get(call.server, ())) or operation in served[call.server]:
                continue
            # A read the task grades is already counted as out of reach.
            if any(expected.server == call.server and _same_operation(expected, method, path) for expected in graded):
                continue
            hidden.setdefault(task.id, []).append(f'{method} {path} on {call.server}')
    return hidden


def _listed(scenario_run: ScenarioRun, variant: str) -> Listed:
    """Each server's operations ``variant`` serves, by server name, from the name a tool lists to method and path."""
    return {
        server.name: {listing.name: (listing.method, listing.path) for listing in list_operations(server)}
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
