import collections
import dataclasses
import json
import pathlib
import random
import typing

import pydantic

from .dataset import SPLITS, STYLES, DatasetError, Query, SpecSet, Split, Style
from .metrics import ANSWER_GRADE, answers_of


ANNOTATIONS = 'annotations'

# How many queries of each style go to dev per spec. The rest are test, which alone is reported.
DEV_PER_STYLE = 10

# Seeds the draw of dev queries, so rebuilding from the same annotations gives the same split.
SPLIT_SEED = 'search'


class Adjudication(pydantic.BaseModel):
    """The judgements a third look settled on, for a query the two annotators share no answer for."""

    model_config = pydantic.ConfigDict(extra='forbid')

    relevant: dict[str, int]
    note: str


class Annotation(pydantic.BaseModel):
    """One query as annotated, with its text and style, both annotators' judgements, and any adjudication."""

    model_config = pydantic.ConfigDict(extra='forbid')

    id: str
    text: str
    style: Style
    annotator_a: dict[str, int]
    annotator_b: dict[str, int]
    adjudication: Adjudication | None = None

    @property
    def has_shared_answer(self) -> bool:
        return bool(answers_of(self.annotator_a) & answers_of(self.annotator_b))

    def judgements(self) -> dict[str, int]:
        """The merged judgements, or the adjudicated ones, raising ``DatasetError`` when neither exists.

        An operation is the answer only when both annotators call it the answer.
        It is an acceptable lesser one when either annotator judges it relevant at all.
        A query whose annotators share no answer cannot be merged that way, so it needs an adjudication.
        """
        if self.adjudication is not None:
            return dict(self.adjudication.relevant)
        if not self.has_shared_answer:
            raise DatasetError(f'The annotators share no answer for {self.id}, so it needs an adjudication')
        merged = {}
        for name in self.annotator_a.keys() | self.annotator_b.keys():
            both_answer = min(self.annotator_a.get(name, 0), self.annotator_b.get(name, 0)) == ANSWER_GRADE
            merged[name] = ANSWER_GRADE if both_answer else 1
        return dict(sorted(merged.items()))

    def grade_pairs(self) -> list[tuple[int, int]]:
        """The two annotators' grades for every operation either of them judged."""
        names = self.annotator_a.keys() | self.annotator_b.keys()
        return [(self.annotator_a.get(name, 0), self.annotator_b.get(name, 0)) for name in names]


@dataclasses.dataclass(frozen=True)
class Agreement:
    """How far two annotators agreed on one spec, before adjudication."""

    queries: int
    # Queries where the annotators named at least one answer in common.
    shared_answer: int
    # Cohen's kappa on the grade of every operation either annotator judged.
    kappa: float
    adjudicated: int


def build_spec(root: pathlib.Path, spec: str) -> tuple[SpecSet, Agreement]:
    """Merge the annotations of ``spec`` under ``root`` into its queries and qrels, with the agreement measured."""
    annotations = read_annotations(root / ANNOTATIONS / f'{spec}.jsonl')
    split_of = assign_splits(annotations)
    qrels: dict[Split, dict[str, dict[str, int]]] = {split: {} for split in SPLITS}
    for annotation in annotations:
        qrels[split_of[annotation.id]][annotation.id] = annotation.judgements()
    spec_set = SpecSet(
        name=spec,
        queries={a.id: Query(id=a.id, text=a.text, style=a.style) for a in annotations},
        qrels=qrels,
    )
    agreement = Agreement(
        queries=len(annotations),
        shared_answer=sum(annotation.has_shared_answer for annotation in annotations),
        kappa=cohens_kappa([pair for annotation in annotations for pair in annotation.grade_pairs()]),
        adjudicated=sum(annotation.adjudication is not None for annotation in annotations),
    )
    return spec_set, agreement


def read_annotations(path: pathlib.Path) -> list[Annotation]:
    """Read one spec's annotations, one query per line, raising ``DatasetError`` on a malformed line.

    The file stays in the benchmark directory, so anyone can see where each judgement came from.
    """
    annotations = []
    for line_number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        if not line.strip():
            continue
        try:
            annotations.append(Annotation.model_validate(json.loads(line)))
        except (json.JSONDecodeError, pydantic.ValidationError) as error:
            raise DatasetError(f'{path}:{line_number}: {error}') from error
    return annotations


def assign_splits(annotations: typing.Iterable[Annotation]) -> dict[str, Split]:
    """Put ``DEV_PER_STYLE`` queries of each style in dev and the rest in test, drawn by a fixed seed."""
    by_style: dict[str, list[str]] = collections.defaultdict(list)
    for annotation in annotations:
        by_style[annotation.style].append(annotation.id)
    split_of: dict[str, Split] = {}
    for style in STYLES:
        ids = sorted(by_style.get(style, []))
        random.Random(f'{SPLIT_SEED}:{style}').shuffle(ids)
        for position, query_id in enumerate(ids):
            split_of[query_id] = 'dev' if position < DEV_PER_STYLE else 'test'
    return split_of


def cohens_kappa(pairs: typing.Sequence[tuple[int, int]]) -> float:
    """Cohen's kappa of two raters over the same items, ``1`` for perfect agreement and ``0`` for chance."""
    if not pairs:
        return 1.0
    total = len(pairs)
    observed = sum(a == b for a, b in pairs) / total
    first = collections.Counter(a for a, _ in pairs)
    second = collections.Counter(b for _, b in pairs)
    expected = sum(first[grade] * second[grade] for grade in first.keys() | second.keys()) / total**2
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)
