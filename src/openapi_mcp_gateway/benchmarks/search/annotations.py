import collections
import json
import pathlib
import random
import typing

import pydantic

from .dataset import GRADES, SPLITS, STYLES, DatasetError, Query, SpecDataset, Split, Style
from .metrics import ANSWER_GRADE, answers_in


ANNOTATIONS_DIRECTORY = 'annotations'

# The grade an operation gets when only one annotator calls it the answer, or either judges it a lesser one.
LESSER_GRADE = 1

# How many queries of each style go to dev per spec. The rest are test, which alone is reported.
DEV_QUERIES_PER_STYLE = 10

# Seeds the draw of dev queries, so rebuilding from the same annotations gives the same split.
SPLIT_SEED = 'search'


class Adjudication(pydantic.BaseModel):
    """The judgements a third look settled on, for a query the two annotators share no answer for."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    relevant: dict[str, int]
    note: str

    @pydantic.field_validator('relevant')
    @classmethod
    def _validate_relevant(cls, relevant: dict[str, int]) -> dict[str, int]:
        """Refuse a grade outside 1 and 2, or judgements with no answer, as a merged query would be refused."""
        wrong_grades = sorted(grade for grade in relevant.values() if grade not in GRADES)
        if wrong_grades:
            raise ValueError(f'grades {wrong_grades} are not one of {list(GRADES)}')
        if ANSWER_GRADE not in relevant.values():
            raise ValueError(f'no operation is graded {ANSWER_GRADE}')
        return relevant


class Annotation(pydantic.BaseModel):
    """One query as annotated, with its text and style, both annotators' judgements, and any adjudication."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    id: str
    text: str
    style: Style
    annotator_a: dict[str, int]
    annotator_b: dict[str, int]
    adjudication: Adjudication | None = None

    @property
    def has_shared_answer(self) -> bool:
        return bool(answers_in(self.annotator_a) & answers_in(self.annotator_b))

    @property
    def query(self) -> Query:
        return Query(id=self.id, text=self.text, style=self.style)

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
            both_call_it_the_answer = min(self.annotator_a.get(name, 0), self.annotator_b.get(name, 0)) == ANSWER_GRADE
            merged[name] = ANSWER_GRADE if both_call_it_the_answer else LESSER_GRADE
        return dict(sorted(merged.items()))

    def grade_pairs(self) -> list[tuple[int, int]]:
        """The two annotators' grades for every operation either of them judged."""
        names = self.annotator_a.keys() | self.annotator_b.keys()
        return [(self.annotator_a.get(name, 0), self.annotator_b.get(name, 0)) for name in names]


class Agreement(pydantic.BaseModel):
    """How far two annotators agreed on one spec, before adjudication."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    query_count: int
    # Queries where the annotators named at least one answer in common.
    shared_answer_count: int
    # Cohen's kappa on the grade of every operation either annotator judged.
    kappa: float
    adjudicated_count: int


def build_spec_dataset(root: pathlib.Path, spec: str) -> tuple[SpecDataset, Agreement]:
    """Merge the annotations of ``spec`` under ``root`` into its queries and judgements, with the agreement measured."""
    annotations = read_annotations(root / ANNOTATIONS_DIRECTORY / f'{spec}.jsonl')
    split_by_query = assign_splits(annotations)
    judgements: dict[Split, dict[str, dict[str, int]]] = {split: {} for split in SPLITS}
    for annotation in annotations:
        judgements[split_by_query[annotation.id]][annotation.id] = annotation.judgements()
    spec_dataset = SpecDataset(
        name=spec,
        queries={annotation.id: annotation.query for annotation in annotations},
        judgements=judgements,
    )
    agreement = Agreement(
        query_count=len(annotations),
        shared_answer_count=sum(annotation.has_shared_answer for annotation in annotations),
        kappa=cohens_kappa([pair for annotation in annotations for pair in annotation.grade_pairs()]),
        adjudicated_count=sum(annotation.adjudication is not None for annotation in annotations),
    )
    return spec_dataset, agreement


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
    """Put ``DEV_QUERIES_PER_STYLE`` queries of each style in dev and the rest in test, drawn by a fixed seed."""
    query_ids_by_style: dict[str, list[str]] = collections.defaultdict(list)
    for annotation in annotations:
        query_ids_by_style[annotation.style].append(annotation.id)
    split_by_query: dict[str, Split] = {}
    for style in STYLES:
        query_ids = sorted(query_ids_by_style.get(style, []))
        random.Random(f'{SPLIT_SEED}:{style}').shuffle(query_ids)
        for position, query_id in enumerate(query_ids):
            split_by_query[query_id] = 'dev' if position < DEV_QUERIES_PER_STYLE else 'test'
    return split_by_query


def cohens_kappa(grade_pairs: typing.Sequence[tuple[int, int]]) -> float:
    """Cohen's kappa of two raters over the same items, ``1`` for perfect agreement and ``0`` for chance."""
    if not grade_pairs:
        return 1.0
    total = len(grade_pairs)
    observed = sum(first == second for first, second in grade_pairs) / total
    first_counts = collections.Counter(first for first, _ in grade_pairs)
    second_counts = collections.Counter(second for _, second in grade_pairs)
    grades = first_counts.keys() | second_counts.keys()
    expected = sum(first_counts[grade] * second_counts[grade] for grade in grades) / total**2
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)
