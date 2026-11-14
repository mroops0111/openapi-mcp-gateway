import dataclasses
import random
import statistics
import typing


# Enough resamples that the interval's ends move by well under a hundredth between seeds.
DEFAULT_RESAMPLES = 10_000
DEFAULT_CONFIDENCE = 0.95


@dataclasses.dataclass(frozen=True)
class Difference:
    """How much one system scores above another, with a bootstrap confidence interval around it."""

    mean: float
    low: float
    high: float

    @property
    def significant(self) -> bool:
        """Whether the interval excludes zero, so the difference is unlikely to be an accident of the sample."""
        return self.low > 0 or self.high < 0


def macro_mean(scores_by_stratum: typing.Mapping[str, typing.Sequence[float]]) -> float:
    """The mean of each stratum's mean, so every stratum counts the same however many items it holds."""
    return statistics.fmean(statistics.fmean(scores) for scores in scores_by_stratum.values())


def paired_bootstrap(
    differences_by_stratum: typing.Mapping[str, typing.Sequence[float]],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = 0,
) -> Difference:
    """Estimate the macro-averaged difference between two systems scored on the same items.

    Each value is one item's score under the first system minus its score under the second.
    Items are resampled with replacement within each stratum, such as a spec,
    so the interval reflects which items happened to be in the set, while every stratum keeps its weight.
    """
    if not differences_by_stratum or any(not values for values in differences_by_stratum.values()):
        raise ValueError('Every stratum needs at least one paired difference')
    rng = random.Random(seed)
    strata = [list(values) for values in differences_by_stratum.values()]
    estimates = sorted(
        statistics.fmean(statistics.fmean(rng.choices(values, k=len(values))) for values in strata)
        for _ in range(resamples)
    )
    tail = (1 - confidence) / 2
    return Difference(
        mean=macro_mean(differences_by_stratum),
        low=estimates[int(tail * (resamples - 1))],
        high=estimates[int((1 - tail) * (resamples - 1))],
    )
