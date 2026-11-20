import asyncio
import dataclasses
import json
import logging
import re
import typing
import urllib.parse

import pydantic
from pydantic_ai.models import Model
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext
from pydantic_evals.reporting import EvaluationReport

from .dataset import Benchmark, DatasetError, Split, Task
from .harness import RunOutcome, ScenarioRun
from .replay import RecordedRequest


logger = logging.getLogger(__name__)

# How often a run that hit a rate limit or an unavailable provider is tried before it counts as errored.
# The wait doubles after each attempt, up to the limit, in seconds.
TRANSIENT_ATTEMPTS = 4
RETRY_WAIT_LIMIT_SECONDS = 60

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
    repetition: int
    passed: bool
    # True when the run failed for reasons outside the model and the variant, after every retry.
    errored: bool
    failure: str | None
    missing_calls: list[str]
    forbidden_calls: list[str]
    extra_writes: list[str]
    input_tokens: int
    # The part of input_tokens read from or written to the provider's prompt cache.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int
    # In US dollars, None for a model with no known price.
    cost: float | None = None
    model_requests: int
    tool_calls: int
    tool_errors: int
    # Each request the upstream received, in order, with where its response came from.
    requests: list[str] = pydantic.Field(default_factory=list)
    # Reads the spec's sample answered, data the task never set, which can mislead a model on a correct route.
    unset_reads: list[str] = pydantic.Field(default_factory=list)


@dataclasses.dataclass
class ExpectedCallsMade(Evaluator[Task, RunOutcome]):
    """Passes a run whose upstream requests make every expected call and none the task forbids."""

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
    split: Split | None = 'test',
    repetitions: int | None = None,
    model_factory: ModelFactory | None = None,
) -> list[RunRecord]:
    """Run every chosen task under every chosen variant and model, as many times as the manifest says.

    Each scenario's tasks for one variant and one model are a Pydantic Evals dataset, evaluated one task at a time,
    since they share one replay upstream.
    An empty choice means all of them.
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
        """Run ``task``, trying again after a provider failure, and count what every attempt cost."""
        agent = scenario_run.agent(factory(model_name, task), model_spec, variant, mcp_urls)
        outcome = await scenario_run.run_task(agent, task)
        spent = outcome.cost
        for attempt in range(1, TRANSIENT_ATTEMPTS):
            if not outcome.errored:
                break
            await asyncio.sleep(min(RETRY_WAIT_LIMIT_SECONDS, 2 * 2**attempt))
            outcome = await scenario_run.run_task(agent, task)
            spent = None if spent is None or outcome.cost is None else spent + outcome.cost
        return outcome.model_copy(update={'cost': spent})

    dataset = Dataset[Task, RunOutcome, None](
        name=f'{scenario_run.scenario.name}/{variant}/{model_name}',
        cases=[Case(name=task.id, inputs=task) for task in tasks],
        evaluators=[ExpectedCallsMade()],
    )
    return await dataset.evaluate(run_case, repeat=repeat, max_concurrency=1, progress=False)


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
                repetition=_repetition(case.name),
                passed=outcome.grade.passed and not outcome.errored,
                errored=outcome.errored,
                failure=outcome.failure,
                missing_calls=outcome.grade.missing_calls,
                forbidden_calls=outcome.grade.forbidden_calls,
                extra_writes=outcome.grade.extra_writes,
                input_tokens=outcome.input_tokens,
                cache_read_tokens=outcome.cache_read_tokens,
                cache_write_tokens=outcome.cache_write_tokens,
                output_tokens=outcome.output_tokens,
                cost=outcome.cost,
                model_requests=outcome.model_requests,
                tool_calls=outcome.tool_calls,
                tool_errors=outcome.tool_errors,
                requests=[_request_line(request) for request in outcome.requests],
                unset_reads=outcome.unset_reads,
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
                repetition=_repetition(failure.name),
                passed=False,
                errored=True,
                failure=failure.error_message,
                missing_calls=[],
                forbidden_calls=[],
                extra_writes=[],
                input_tokens=0,
                output_tokens=0,
                model_requests=0,
                tool_calls=0,
                tool_errors=0,
            )
        )
    return records


def _request_line(request: RecordedRequest) -> str:
    query = f'?{urllib.parse.urlencode(request.query, doseq=True)}' if request.query else ''
    body = f' with {json.dumps(request.body, default=str)}' if request.body is not None else ''
    return f'{request.method} {request.path}{query} on {request.server}{body}, answered by {request.answer}'


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
