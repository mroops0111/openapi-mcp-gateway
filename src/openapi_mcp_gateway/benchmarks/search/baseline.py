import pathlib
import typing

import pydantic

from .dataset import Gate, Split
from .report import Report


class Baseline(pydantic.BaseModel):
    """The gated metrics of the gated system from one run, over all specs and for each spec.

    It is committed beside the benchmark, and CI holds every later run to it.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    benchmark_version: int
    system: str
    split: Split
    gateway_version: str
    macro: dict[str, float]
    specs: dict[str, dict[str, float]]

    @classmethod
    def from_report(cls, report: Report, gate: Gate) -> typing.Self:
        """Take the numbers ``gate`` names out of ``report``."""
        summary = report.systems[gate.system]
        return cls(
            benchmark_version=report.benchmark_version,
            system=gate.system,
            split=report.split,
            gateway_version=report.gateway_version,
            macro={metric: summary.macro[metric] for metric in gate.metrics},
            specs={
                spec: {metric: spec_summary.overall[metric] for metric in gate.metrics}
                for spec, spec_summary in summary.specs.items()
            },
        )

    @classmethod
    def load(cls, path: pathlib.Path) -> typing.Self:
        return cls.model_validate_json(path.read_text(encoding='utf-8'))

    def save(self, path: pathlib.Path) -> None:
        path.write_text(self.model_dump_json(indent=2) + '\n', encoding='utf-8')

    def regressions(self, current: 'Baseline', tolerance: float) -> list[str]:
        """Each number of ``current`` more than ``tolerance`` below this baseline, described in one line.

        A run of another benchmark version, system, or split measures something else, so it fails outright,
        and the baseline is rewritten with ``search check --update`` in the PR that makes the change.
        """
        identity = ('benchmark_version', 'system', 'split')
        mismatches = [
            f'{field} is {getattr(current, field)!r} in this run but {getattr(self, field)!r} in the baseline'
            for field in identity
            if getattr(current, field) != getattr(self, field)
        ]
        if mismatches:
            return mismatches
        expected_by_scope = {'macro': self.macro, **self.specs}
        measured_by_scope = {'macro': current.macro, **current.specs}
        failures = []
        for scope, expected_scores in expected_by_scope.items():
            for metric, expected in expected_scores.items():
                measured = measured_by_scope.get(scope, {}).get(metric)
                if measured is None:
                    failures.append(f'{scope} {metric}: missing from this run')
                elif measured < expected - tolerance:
                    failures.append(f'{scope} {metric}: {measured:.4f}, down from {expected:.4f}')
        return failures
