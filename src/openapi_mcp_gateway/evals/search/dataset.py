"""A search benchmark on disk: a manifest, and per spec its queries and relevance judgements.

The per-spec files follow BEIR's layout, so other retrieval tools can read them unchanged::

    benchmark.yaml
    <spec>/queries.jsonl        {"_id": ..., "text": ..., "metadata": {"style": ...}} per line
    <spec>/qrels/<split>.tsv    query-id, corpus-id, score, with a header row

A corpus id is an operation name as ``search_operations`` returns it.
"""

import csv
import dataclasses
import json
import pathlib
import typing

import pydantic
import yaml

from ..corpus import SpecPin
from .metrics import ANSWER_GRADE, METRICS


MANIFEST = 'benchmark.yaml'
BASELINE = 'baseline.json'

Style = typing.Literal['literal', 'paraphrase', 'indirect']
STYLES: tuple[Style, ...] = typing.get_args(Style)
Split = typing.Literal['dev', 'test']
SPLITS: tuple[Split, ...] = typing.get_args(Split)
_GRADES = (1, ANSWER_GRADE)


class Gate(pydantic.BaseModel):
    """What CI holds a change to: these metrics of one variant, on one split, against the committed baseline."""

    model_config = pydantic.ConfigDict(extra='forbid')

    variant: str
    split: Split = 'test'
    metrics: list[str] = pydantic.Field(default_factory=lambda: ['ndcg@10', 'recall@20'])
    # How far a metric may fall below the baseline before the gate fails.
    tolerance: float = pydantic.Field(default=0.01, ge=0)

    @pydantic.field_validator('metrics')
    @classmethod
    def _known_metrics(cls, metrics: list[str]) -> list[str]:
        known = {metric.name for metric in METRICS}
        unknown = sorted(set(metrics) - known)
        if unknown:
            raise ValueError(f'Unknown metric(s) {unknown}; choose from {sorted(known)}')
        return metrics


class Manifest(pydantic.BaseModel):
    """``benchmark.yaml``: which specs, which gateway configurations to compare, and what CI gates on.

    A variant is the part of a server's config that differs between systems, such as ``exposure``.
    The runner fills in each spec's ``name`` and ``spec``, so one variant applies to every spec.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    version: int = pydantic.Field(ge=1)
    specs: dict[str, SpecPin] = pydantic.Field(min_length=1)
    variants: dict[str, dict[str, typing.Any]] = pydantic.Field(min_length=1)
    gate: Gate

    @pydantic.model_validator(mode='after')
    def _gate_names_a_variant(self) -> typing.Self:
        if self.gate.variant not in self.variants:
            raise ValueError(f'gate.variant {self.gate.variant!r} is not one of the variants {sorted(self.variants)}')
        for name, variant in self.variants.items():
            if {'name', 'spec'} & set(variant):
                raise ValueError(f'Variant {name!r} cannot set name or spec, which come from each spec')
        return self


@dataclasses.dataclass(frozen=True)
class Query:
    id: str
    text: str
    style: Style


@dataclasses.dataclass(frozen=True)
class SpecSet:
    """One spec's queries and the judgements for each split."""

    name: str
    queries: dict[str, Query]
    # Split, then query id, then operation name, to its grade.
    qrels: dict[Split, dict[str, dict[str, int]]]

    def split(self, split: Split) -> list[tuple[Query, dict[str, int]]]:
        """The queries of ``split`` with their judgements, in file order."""
        return [(self.queries[query_id], judgements) for query_id, judgements in self.qrels[split].items()]


@dataclasses.dataclass(frozen=True)
class Benchmark:
    root: pathlib.Path
    manifest: Manifest
    specs: dict[str, SpecSet]

    @property
    def baseline_path(self) -> pathlib.Path:
        return self.root / BASELINE


class DatasetError(ValueError):
    """The benchmark's files are inconsistent."""


def load(root: pathlib.Path) -> Benchmark:
    """Load and validate the benchmark in ``root``, raising ``DatasetError`` on any inconsistency."""
    manifest = load_manifest(root)
    return Benchmark(root=root, manifest=manifest, specs={name: _load_spec_set(root, name) for name in manifest.specs})


def load_manifest(root: pathlib.Path) -> Manifest:
    """Load ``benchmark.yaml`` alone, for steps that run before the queries exist."""
    manifest_path = root / MANIFEST
    if not manifest_path.exists():
        raise DatasetError(f'{manifest_path} not found')
    try:
        return Manifest.model_validate(yaml.safe_load(manifest_path.read_text(encoding='utf-8')))
    except pydantic.ValidationError as error:
        raise DatasetError(f'{manifest_path}: {error}') from error


def _load_spec_set(root: pathlib.Path, name: str) -> SpecSet:
    directory = root / name
    queries = _load_queries(directory / 'queries.jsonl')
    qrels: dict[Split, dict[str, dict[str, int]]] = {
        split: _load_qrels(directory / 'qrels' / f'{split}.tsv', queries) for split in SPLITS
    }
    in_both = set(qrels['dev']) & set(qrels['test'])
    if in_both:
        raise DatasetError(f'{name}: queries {sorted(in_both)} are judged in both dev and test')
    unjudged = set(queries) - set(qrels['dev']) - set(qrels['test'])
    if unjudged:
        raise DatasetError(f'{name}: queries {sorted(unjudged)} have no judgements')
    return SpecSet(name=name, queries=queries, qrels=qrels)


def _load_queries(path: pathlib.Path) -> dict[str, Query]:
    queries: dict[str, Query] = {}
    for line_number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        if not line.strip():
            continue
        where = f'{path}:{line_number}'
        record = json.loads(line)
        query_id, text = record.get('_id'), record.get('text')
        style = (record.get('metadata') or {}).get('style')
        if not isinstance(query_id, str) or not isinstance(text, str) or not text.strip():
            raise DatasetError(f'{where}: a query needs a string _id and a non-empty text')
        if style not in STYLES:
            raise DatasetError(f'{where}: style {style!r} is not one of {list(STYLES)}')
        if query_id in queries:
            raise DatasetError(f'{where}: duplicate query id {query_id!r}')
        queries[query_id] = Query(id=query_id, text=text, style=style)
    return queries


def _load_qrels(path: pathlib.Path, queries: typing.Mapping[str, Query]) -> dict[str, dict[str, int]]:
    qrels: dict[str, dict[str, int]] = {}
    with path.open(encoding='utf-8', newline='') as file:
        reader = csv.DictReader(file, delimiter='\t')
        if reader.fieldnames != ['query-id', 'corpus-id', 'score']:
            raise DatasetError(f'{path}: the header must be query-id, corpus-id, score')
        for row in reader:
            where = f'{path}:{reader.line_num}'
            query_id, operation, grade = row['query-id'], row['corpus-id'], int(row['score'])
            if query_id not in queries:
                raise DatasetError(f'{where}: unknown query id {query_id!r}')
            if grade not in _GRADES:
                raise DatasetError(f'{where}: grade {grade} is not one of {list(_GRADES)}')
            qrels.setdefault(query_id, {})[operation] = grade
    without_answer = sorted(query_id for query_id, judged in qrels.items() if ANSWER_GRADE not in judged.values())
    if without_answer:
        raise DatasetError(f'{path}: queries {without_answer} have no operation graded {ANSWER_GRADE}')
    return qrels


def write_spec_set(root: pathlib.Path, spec_set: SpecSet) -> None:
    """Write ``spec_set`` in the layout ``load`` reads, for tools that build or revise a benchmark."""
    directory = root / spec_set.name
    (directory / 'qrels').mkdir(parents=True, exist_ok=True)
    with (directory / 'queries.jsonl').open('w', encoding='utf-8') as file:
        for query in spec_set.queries.values():
            record = {'_id': query.id, 'text': query.text, 'metadata': {'style': query.style}}
            file.write(json.dumps(record, ensure_ascii=False) + '\n')
    for split in SPLITS:
        with (directory / 'qrels' / f'{split}.tsv').open('w', encoding='utf-8', newline='') as file:
            writer = csv.writer(file, delimiter='\t', lineterminator='\n')
            writer.writerow(['query-id', 'corpus-id', 'score'])
            for query_id, judgements in spec_set.qrels[split].items():
                for operation, grade in sorted(judgements.items(), key=lambda item: (-item[1], item[0])):
                    writer.writerow([query_id, operation, grade])
