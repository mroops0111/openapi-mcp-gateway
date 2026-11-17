import collections
import importlib.metadata
import statistics
import typing

import pydantic

from ..significance import paired_bootstrap
from .dataset import BASELINE_VARIANT, Benchmark
from .runner import RunRecord


# Report values are rounded to this many places, more than any table shows and few enough to diff.
DECIMAL_PLACES = 4

RoundedValue = typing.Annotated[float, pydantic.AfterValidator(lambda value: round(value, DECIMAL_PLACES))]

# What each comparison with the baseline measures, read off a run.
COMPARED_MEASURES: dict[str, typing.Callable[[RunRecord], float]] = {
    'success_rate': lambda record: float(record.passed),
    'input_tokens': lambda record: float(record.input_tokens),
}


class CellSummary(pydantic.BaseModel):
    """One scenario under one variant and one model, averaged over its runs."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    scenario: str
    variant: str
    model: str
    run_count: int
    # Runs that failed for reasons outside the model and the variant, left out of every mean below.
    errored_count: int
    success_rate: RoundedValue
    # The standard deviation of the success rate across repetitions, how much a rerun moves it.
    success_rate_spread: RoundedValue
    in_intent_success_rate: RoundedValue | None
    out_of_intent_success_rate: RoundedValue | None
    mean_input_tokens: RoundedValue
    mean_output_tokens: RoundedValue
    mean_tool_calls: RoundedValue
    mean_tool_errors: RoundedValue


class Comparison(pydantic.BaseModel):
    """How far a variant moves one measure from the baseline under one model, with its 95% bootstrap interval."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    model: str
    variant: str
    measure: str
    baseline_mean: RoundedValue
    difference: RoundedValue
    interval_low: RoundedValue
    interval_high: RoundedValue
    significant: bool


class AgentReport(pydantic.BaseModel):
    """Everything one tool calling benchmark run found, written as JSON by ``tool-calling run --output``."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    benchmark_version: int
    gateway_version: str
    pydantic_ai_version: str
    # Each model's name in the manifest to the model Pydantic AI ran.
    models: dict[str, str]
    cells: list[CellSummary]
    comparisons: list[Comparison]
    records: list[RunRecord]


def build_report(benchmark: Benchmark, records: typing.Sequence[RunRecord]) -> AgentReport:
    """Summarize ``records`` per scenario, variant, and model, and compare each variant with the baseline."""
    cells: dict[tuple[str, str, str], list[RunRecord]] = collections.defaultdict(list)
    for record in records:
        cells[(record.scenario, record.variant, record.model)].append(record)
    return AgentReport(
        benchmark_version=benchmark.manifest.version,
        gateway_version=importlib.metadata.version('openapi-mcp-gateway'),
        pydantic_ai_version=importlib.metadata.version('pydantic-ai-slim'),
        models={name: spec.model for name, spec in benchmark.manifest.models.items()},
        cells=[_cell_summary(key, cell_records) for key, cell_records in cells.items()],
        comparisons=_comparisons(records),
        records=list(records),
    )


def _cell_summary(key: tuple[str, str, str], records: typing.Sequence[RunRecord]) -> CellSummary:
    scenario, variant, model = key
    scored = [record for record in records if not record.errored]
    success_by_repetition: dict[int, list[float]] = collections.defaultdict(list)
    for record in scored:
        success_by_repetition[record.repetition].append(float(record.passed))
    repetition_rates = [statistics.fmean(rates) for rates in success_by_repetition.values()]
    return CellSummary(
        scenario=scenario,
        variant=variant,
        model=model,
        run_count=len(records),
        errored_count=len(records) - len(scored),
        success_rate=_mean(float(record.passed) for record in scored),
        success_rate_spread=statistics.pstdev(repetition_rates) if len(repetition_rates) > 1 else 0.0,
        in_intent_success_rate=_optional_mean(float(record.passed) for record in scored if record.in_intent),
        out_of_intent_success_rate=_optional_mean(float(record.passed) for record in scored if not record.in_intent),
        mean_input_tokens=_mean(record.input_tokens for record in scored),
        mean_output_tokens=_mean(record.output_tokens for record in scored),
        mean_tool_calls=_mean(record.tool_calls for record in scored),
        mean_tool_errors=_mean(record.tool_errors for record in scored),
    )


def _comparisons(records: typing.Sequence[RunRecord]) -> list[Comparison]:
    """Each variant against the baseline, per model, paired by task and stratified by scenario.

    A task's value is its mean over repetitions, so the bootstrap resamples tasks, not reruns of one task.
    """
    task_means: dict[tuple[str, str, str], dict[tuple[str, str], list[RunRecord]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    for record in records:
        if not record.errored:
            task_means[(record.model, record.variant, record.scenario)][(record.scenario, record.task_id)].append(
                record
            )
    comparisons = []
    models = sorted({record.model for record in records})
    variants = sorted({record.variant for record in records} - {BASELINE_VARIANT})
    scenarios = sorted({record.scenario for record in records})
    for model in models:
        for variant in variants:
            for measure, read in COMPARED_MEASURES.items():
                differences: dict[str, list[float]] = {}
                baseline_values: list[float] = []
                for scenario in scenarios:
                    baseline = task_means.get((model, BASELINE_VARIANT, scenario), {})
                    candidate = task_means.get((model, variant, scenario), {})
                    shared = sorted(set(baseline) & set(candidate))
                    if not shared:
                        continue
                    baseline_by_task = {task: statistics.fmean(map(read, baseline[task])) for task in shared}
                    differences[scenario] = [
                        statistics.fmean(map(read, candidate[task])) - baseline_by_task[task] for task in shared
                    ]
                    baseline_values.extend(baseline_by_task.values())
                if not differences:
                    continue
                difference = paired_bootstrap(differences)
                comparisons.append(
                    Comparison(
                        model=model,
                        variant=variant,
                        measure=measure,
                        baseline_mean=statistics.fmean(baseline_values),
                        difference=difference.mean,
                        interval_low=difference.low,
                        interval_high=difference.high,
                        significant=difference.significant,
                    )
                )
    return comparisons


def _mean(values: typing.Iterable[float]) -> float:
    collected = list(values)
    return statistics.fmean(collected) if collected else 0.0


def _optional_mean(values: typing.Iterable[float]) -> float | None:
    collected = list(values)
    return statistics.fmean(collected) if collected else None
