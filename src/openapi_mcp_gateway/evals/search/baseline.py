import json
import pathlib
import typing

import pydantic

from .dataset import Benchmark


class Baseline(pydantic.BaseModel):
    """The gated metrics of the gated system from one run, over all specs and for each spec.

    It is committed beside the benchmark, and CI holds every later run to it.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    version: int
    system: str
    split: str
    gateway_version: str
    macro: dict[str, float]
    specs: dict[str, dict[str, float]]

    @classmethod
    def from_report(cls, report: dict[str, typing.Any], benchmark: Benchmark) -> typing.Self:
        """Take the numbers ``benchmark``'s gate names out of ``report``."""
        gate = benchmark.manifest.gate
        summary = report['systems'][gate.system]
        return cls(
            version=benchmark.manifest.version,
            system=gate.system,
            split=gate.split,
            gateway_version=report['gateway_version'],
            macro={metric: summary['macro'][metric] for metric in gate.metrics},
            specs={spec: {metric: s['all'][metric] for metric in gate.metrics} for spec, s in summary['specs'].items()},
        )

    @classmethod
    def load(cls, path: pathlib.Path) -> typing.Self:
        return cls.model_validate_json(path.read_text(encoding='utf-8'))

    def save(self, path: pathlib.Path) -> None:
        path.write_text(json.dumps(self.model_dump(), indent=2) + '\n', encoding='utf-8')

    def regressions(self, current: 'Baseline', tolerance: float) -> list[str]:
        """Each number of ``current`` more than ``tolerance`` below this baseline, described in one line."""
        expected = {'macro': self.macro, **self.specs}
        measured = {'macro': current.macro, **current.specs}
        failures = []
        for scope, metrics in expected.items():
            for metric, value in metrics.items():
                now = measured.get(scope, {}).get(metric)
                if now is None:
                    failures.append(f'{scope} {metric}: missing from this run')
                elif now < value - tolerance:
                    failures.append(f'{scope} {metric}: {now:.4f}, down from {value:.4f}')
        return failures
