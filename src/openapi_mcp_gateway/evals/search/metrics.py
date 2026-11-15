import abc
import math
import typing


# The grade that marks the answer itself, as TREC grades it.
# Grade 1 marks an operation that would also get there, and anything unjudged is 0.
ANSWER_GRADE = 2


class Metric(abc.ABC):
    """A score from ``0`` to ``1`` for how well one ranking serves one query, looking at the top ``cutoff`` only.

    A metric that counts hits counts only ``ANSWER_GRADE``, so a lesser operation ranked first is not a hit.
    A new metric is a new subclass listed in ``scoring.METRICS``, and nothing that scores or reports changes.
    """

    # The metric's family, which its reported name starts with, such as ``ndcg`` in ``ndcg@10``.
    family: typing.ClassVar[str]

    def __init__(self, cutoff: int):
        self.cutoff = cutoff

    @property
    def name(self) -> str:
        return f'{self.family}@{self.cutoff}'

    @abc.abstractmethod
    def score(self, ranking: typing.Sequence[str], judgements: typing.Mapping[str, int]) -> float:
        """Score ``ranking`` against ``judgements``, a grade per relevant operation name."""


class NDCG(Metric):
    """Normalised discounted cumulative gain, which rewards every relevant operation by its grade and rank.

    Gain is linear in the grade, as ``trec_eval`` computes it.
    """

    family = 'ndcg'

    def score(self, ranking: typing.Sequence[str], judgements: typing.Mapping[str, int]) -> float:
        ideal = _dcg(sorted(judgements.values(), reverse=True)[: self.cutoff])
        if ideal == 0:
            return 0.0
        return _dcg([judgements.get(name, 0) for name in ranking[: self.cutoff]]) / ideal


class ReciprocalRank(Metric):
    """One over the rank of the first answer, or ``0`` when no answer is within the cutoff."""

    family = 'mrr'

    def score(self, ranking: typing.Sequence[str], judgements: typing.Mapping[str, int]) -> float:
        for rank, name in enumerate(ranking[: self.cutoff], start=1):
            if judgements.get(name, 0) >= ANSWER_GRADE:
                return 1 / rank
        return 0.0


class Recall(Metric):
    """The share of a query's answers ranked within the cutoff."""

    family = 'recall'

    def score(self, ranking: typing.Sequence[str], judgements: typing.Mapping[str, int]) -> float:
        answers = answers_of(judgements)
        if not answers:
            return 0.0
        return len(answers.intersection(ranking[: self.cutoff])) / len(answers)


def answers_of(judgements: typing.Mapping[str, int]) -> set[str]:
    """The operations graded as the answer itself."""
    return {name for name, grade in judgements.items() if grade >= ANSWER_GRADE}


def _dcg(grades: typing.Iterable[int]) -> float:
    return sum(grade / math.log2(rank + 1) for rank, grade in enumerate(grades, start=1))
