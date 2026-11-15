import dataclasses
import logging
import os
import re
import typing

import pydantic
import tenacity
from pydantic_ai.models import Model
from pydantic_ai.retries import RetryConfig
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext
from pydantic_evals.reporting import EvaluationReport

from .dataset import Benchmark, DatasetError, Split, Task
from .harness import TRANSIENT_ERRORS, RunOutcome, ScenarioRun


logger = logging.getLogger(__name__)

# Pydantic AI prints a banner on its first agent, which would land in the middle of a benchmark's output.
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

# How often a run that hit a rate limit or an unavailable provider is tried again before it counts as errored.
TRANSIENT_ATTEMPTS = 4

# Pydantic Evals names the repeats of a case like ``tmdb-t01 [2/3]``.
REPEAT_SUFFIX = re.compile(r' \[(\d+)/\d+\]$')

# What builds a model from its manifest name, which a test replaces with a scripted model.
ModelFactory = typing.Callable[[str, Task], Model | str]

ChosenType = typing.TypeVar('ChosenType')


class RunRecord(pydantic.BaseModel):
    """One run of one task, under one variant and one model, as the report reads it."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    scenario: str
    variant: str
    model: str
    task_id: str
    split: Split
    in_intent: bool
    repetition: int
    passed: bool
    # True when the run failed for reasons outside the model and the variant, after every retry.
    errored: bool
    failure: str | None
    missing_calls: list[str]
    unexpected_writes: list[str]
    input_tokens: int
    output_tokens: int
    model_requests: int
    tool_calls: int
    tool_errors: int


@dataclasses.dataclass
class ExpectedCallsMade(Evaluator[Task, RunOutcome]):
    """Passes a run whose upstream requests make every expected call and write nothing else."""

    def evaluate(self, ctx: EvaluatorContext[Task, RunOutcome]) -> bool:
        return ctx.output.grade.passed


def manifest_models(benchmark: Benchmark) -> ModelFactory:
    """Build each model the way the manifest names it, so Pydantic AI picks the provider from the prefix."""
    return lambda model_name, _task: benchmark.manifest.models[model_name].model


async def run(
    benchmark: Benchmark,
    *,
    scenario_names: typing.Sequence[str] = (),
    variant_names: typing.Sequence[str] = (),
    model_names: typing.Sequence[str] = (),
    split: Split | None = 'final',
    repetitions: int | None = None,
    model_factory: ModelFactory | None = None,
) -> list[RunRecord]:
    """Run every chosen task under every chosen variant and model, as many times as the manifest says.

    Each scenario's tasks for one variant and one model are a Pydantic Evals dataset, evaluated one task at a time,
    since they share one replay upstream. An empty choice means all of them.
    """
    scenarios = _chosen(benchmark.scenarios, scenario_names, 'scenario')
    models = _chosen(benchmark.manifest.models, model_names, 'model')
    factory = model_factory or manifest_models(benchmark)
    repeat = repetitions or benchmark.manifest.repetitions
    spec_paths = {name: str(pin.fetch(benchmark.root)) for name, pin in benchmark.manifest.specs.items()}
    records: list[RunRecord] = []
    for scenario_name, scenario in scenarios.items():
        tasks = [task for task in scenario.tasks if split is None or task.split == split]
        variants = [name for name in scenario.variants if not variant_names or name in variant_names]
        scenario_run = ScenarioRun(benchmark, scenario, spec_paths)
        async with scenario_run.serving():
            for variant in variants:
                async with scenario_run.gateway(variant) as mcp_urls:
                    for model_name in models:
                        report = await _evaluate(scenario_run, tasks, variant, model_name, mcp_urls, factory, repeat)
                        records.extend(_records(report, scenario_name, variant, model_name))
                        logger.info(
                            'Ran %d tasks of %s under %s with %s', len(tasks), scenario_name, variant, model_name
                        )
    return records


async def _evaluate(
    scenario_run: ScenarioRun,
    tasks: typing.Sequence[Task],
    variant: str,
    model_name: str,
    mcp_urls: typing.Mapping[str, str],
    factory: ModelFactory,
    repeat: int,
) -> EvaluationReport[Task, RunOutcome, None]:
    model_spec = scenario_run.benchmark.manifest.models[model_name]

    async def run_case(task: Task) -> RunOutcome:
        agent = scenario_run.agent(factory(model_name, task), model_spec, mcp_urls)
        return await scenario_run.run_task(agent, task)

    dataset = Dataset[Task, RunOutcome, None](
        name=f'{scenario_run.scenario.name}/{variant}/{model_name}',
        cases=[Case(name=task.id, inputs=task) for task in tasks],
        evaluators=[ExpectedCallsMade()],
    )
    retry: RetryConfig = {
        'stop': tenacity.stop_after_attempt(TRANSIENT_ATTEMPTS),
        'wait': tenacity.wait_exponential(multiplier=2, max=60),
        'retry': tenacity.retry_if_exception_type(TRANSIENT_ERRORS),
        'reraise': True,
    }
    return await dataset.evaluate(run_case, repeat=repeat, max_concurrency=1, retry_task=retry, progress=False)


def _records(
    report: EvaluationReport[Task, RunOutcome, None], scenario: str, variant: str, model: str
) -> list[RunRecord]:
    records = []
    for case in report.cases:
        outcome = case.output
        records.append(
            RunRecord(
                scenario=scenario,
                variant=variant,
                model=model,
                task_id=case.inputs.id,
                split=case.inputs.split,
                in_intent=case.inputs.in_intent,
                repetition=_repetition(case.name),
                passed=outcome.grade.passed,
                errored=False,
                failure=outcome.failure,
                missing_calls=outcome.grade.missing_calls,
                unexpected_writes=outcome.grade.unexpected_writes,
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
                model_requests=outcome.model_requests,
                tool_calls=outcome.tool_calls,
                tool_errors=outcome.tool_errors,
            )
        )
    for failure in report.failures:
        records.append(
            RunRecord(
                scenario=scenario,
                variant=variant,
                model=model,
                task_id=failure.inputs.id,
                split=failure.inputs.split,
                in_intent=failure.inputs.in_intent,
                repetition=_repetition(failure.name),
                passed=False,
                errored=True,
                failure=failure.error_message,
                missing_calls=[],
                unexpected_writes=[],
                input_tokens=0,
                output_tokens=0,
                model_requests=0,
                tool_calls=0,
                tool_errors=0,
            )
        )
    return records


def _repetition(case_name: str) -> int:
    found = REPEAT_SUFFIX.search(case_name)
    return int(found.group(1)) if found else 1


def _chosen(
    available: typing.Mapping[str, ChosenType], names: typing.Sequence[str], kind: str
) -> dict[str, ChosenType]:
    unknown = [name for name in names if name not in available]
    if unknown:
        raise DatasetError(f'Unknown {kind}(s) {unknown}; the benchmark has {sorted(available)}')
    return {name: available[name] for name in names} if names else dict(available)
