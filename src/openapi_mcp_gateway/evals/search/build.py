"""Turn two annotators' judgements, and the settled disagreements, into a benchmark's queries and qrels.

The annotations stay in the benchmark directory, so anyone can see where each judgement came from::

    annotations/<spec>/annotator_a.jsonl   {"id", "text", "style", "relevant": {name: grade}} per line
    annotations/<spec>/annotator_b.jsonl   {"id", "relevant"} per line, judged without seeing annotator A
    annotations/<spec>/adjudication.jsonl  {"id", "relevant", "note"} per line, for the queries settled by a third look

Merging follows from what each grade means.
An operation is the answer, grade 2, only when both annotators call it the answer,
and an acceptable lesser one, grade 1, when either annotator judges it relevant at all.
A query whose annotators share no answer cannot be merged that way and needs an adjudication,
which replaces both annotators' judgements for that query.
"""

import collections
import dataclasses
import json
import pathlib
import random
import typing

from .dataset import SPLITS, STYLES, DatasetError, Query, SpecSet, Split
from .metrics import ANSWER_GRADE


ANNOTATIONS = 'annotations'

# How many queries of each style go to dev per spec. The rest are test, which alone is reported.
DEV_PER_STYLE = 10

_SPLIT_SEED = 'search-v1'


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
    directory = root / ANNOTATIONS / spec
    first = _read_jsonl(directory / 'annotator_a.jsonl')
    second = {record['id']: record['relevant'] for record in _read_jsonl(directory / 'annotator_b.jsonl')}
    settled = {record['id']: record['relevant'] for record in _read_jsonl(directory / 'adjudication.jsonl')}

    queries: dict[str, Query] = {}
    judgements: dict[str, dict[str, int]] = {}
    pairs: list[tuple[int, int]] = []
    shared = 0
    for record in first:
        query_id = record['id']
        if query_id not in second:
            raise DatasetError(f'{spec}: annotator B did not judge {query_id}')
        a, b = record['relevant'], second[query_id]
        pairs.extend((a.get(name, 0), b.get(name, 0)) for name in a.keys() | b.keys())
        has_shared_answer = bool(_answers(a) & _answers(b))
        shared += has_shared_answer
        if query_id in settled:
            judgements[query_id] = dict(settled[query_id])
        elif has_shared_answer:
            judgements[query_id] = _merge(a, b)
        else:
            raise DatasetError(f'{spec}: the annotators share no answer for {query_id}, so it needs an adjudication')
        queries[query_id] = Query(id=query_id, text=record['text'], style=record['style'])

    split_of = assign_splits(queries.values())
    qrels: dict[Split, dict[str, dict[str, int]]] = {split: {} for split in SPLITS}
    for query_id, judged in judgements.items():
        qrels[split_of[query_id]][query_id] = judged
    agreement = Agreement(
        queries=len(queries), shared_answer=shared, kappa=cohens_kappa(pairs), adjudicated=len(settled)
    )
    return SpecSet(name=spec, queries=queries, qrels=qrels), agreement


def assign_splits(queries: typing.Iterable[Query]) -> dict[str, Split]:
    """Put ``DEV_PER_STYLE`` queries of each style in dev and the rest in test, drawn by a fixed seed."""
    by_style: dict[str, list[str]] = collections.defaultdict(list)
    for query in queries:
        by_style[query.style].append(query.id)
    split_of: dict[str, Split] = {}
    for style in STYLES:
        ids = sorted(by_style.get(style, []))
        random.Random(f'{_SPLIT_SEED}:{style}').shuffle(ids)
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


def _merge(a: typing.Mapping[str, int], b: typing.Mapping[str, int]) -> dict[str, int]:
    merged = {}
    for name in a.keys() | b.keys():
        grades = (a.get(name, 0), b.get(name, 0))
        merged[name] = ANSWER_GRADE if min(grades) == ANSWER_GRADE else 1
    return dict(sorted(merged.items()))


def _answers(judgements: typing.Mapping[str, int]) -> set[str]:
    return {name for name, grade in judgements.items() if grade == ANSWER_GRADE}


def _read_jsonl(path: pathlib.Path) -> list[dict[str, typing.Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
