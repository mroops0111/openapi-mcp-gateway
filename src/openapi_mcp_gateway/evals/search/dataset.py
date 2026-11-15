import csv
import dataclasses
import json
import pathlib
import typing

import pydantic
import yaml

from ..corpus import SpecPin
from .metrics import ANSWER_GRADE
from .scoring import METRIC_NAMES


MANIFEST = 'benchmark.yaml'
BASELINE = 'baseline.json'
QUERIES = 'queries.jsonl'
QRELS = 'qrels'
QRELS_HEADER = ['query-id', 'corpus-id', 'score']

Style = typing.Literal['literal', 'paraphrase', 'indirect']
Split = typing.Literal['dev', 'test']
STYLES: tuple[Style, ...] = typing.get_args(Style)
SPLITS: tuple[Split, ...] = typing.get_args(Split)
GRADES = (1, ANSWER_GRADE)


class DatasetError(ValueError):
    """The benchmark's files are inconsistent."""


class SystemSpec(pydantic.BaseModel):
    """One system to compare, written in the manifest as a single key that names its kind, such as ``random``."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    kind: str
    config: dict[str, typing.Any] = pydantic.Field(default_factory=dict)

    @pydantic.model_validator(mode='before')
    @classmethod
    def _from_single_key(cls, value: typing.Any) -> typing.Any:
        if isinstance(value, dict) and set(value) - {'kind', 'config'}:
            if len(value) != 1:
                raise ValueError(f'A system is one key naming its kind, such as gateway: {{...}}, not {sorted(value)}')
            kind, config = next(iter(value.items()))
            return {'kind': kind, 'config': config or {}}
        return value


class Gate(pydantic.BaseModel):
    """What CI holds a change to, which is these metrics of one system, on one split, against the baseline."""

    model_config = pydantic.ConfigDict(extra='forbid')

    system: str
    split: Split = 'test'
    metrics: list[str] = pydantic.Field(default_factory=lambda: ['ndcg@10', 'recall@20'])
    # How far a metric may fall below the baseline before the gate fails.
    tolerance: float = pydantic.Field(default=0.01, ge=0)

    @pydantic.field_validator('metrics')
    @classmethod
    def _known_metrics(cls, metrics: list[str]) -> list[str]:
        unknown = sorted(set(metrics) - set(METRIC_NAMES))
        if unknown:
            raise ValueError(f'Unknown metric(s) {unknown}; choose from {list(METRIC_NAMES)}')
        return metrics


class Manifest(pydantic.BaseModel):
    """The manifest in ``benchmark.yaml``, naming the specs, the systems to compare on them, and the CI gate."""

    model_config = pydantic.ConfigDict(extra='forbid')

    version: int = pydantic.Field(ge=1)
    specs: dict[str, SpecPin] = pydantic.Field(min_length=1)
    systems: dict[str, SystemSpec] = pydantic.Field(min_length=1)
    gate: Gate

    @pydantic.model_validator(mode='after')
    def _gate_names_a_system(self) -> typing.Self:
        if self.gate.system not in self.systems:
            raise ValueError(f'gate.system {self.gate.system!r} is not one of the systems {sorted(self.systems)}')
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

    def judged_operations(self) -> set[str]:
        """Every operation any split judges relevant."""
        return {name for split in self.qrels.values() for judgements in split.values() for name in judgements}


@dataclasses.dataclass(frozen=True)
class Benchmark:
    root: pathlib.Path
    manifest: Manifest
    specs: dict[str, SpecSet]

    @property
    def baseline_path(self) -> pathlib.Path:
        return self.root / BASELINE


def load(root: pathlib.Path) -> Benchmark:
    """Load and validate the benchmark in ``root``, raising ``DatasetError`` on any inconsistency.

    Each spec's files follow BEIR's layout, so other retrieval tools read them unchanged.
    ``<spec>/queries.jsonl`` holds one query per line, and ``<spec>/qrels/<split>.tsv`` holds its graded judgements,
    each corpus id an operation name as ``search_operations`` returns it.
    """
    manifest = load_manifest(root)
    return Benchmark(root=root, manifest=manifest, specs={name: _load_spec_set(root, name) for name in manifest.specs})


def load_manifest(root: pathlib.Path) -> Manifest:
    """Load ``benchmark.yaml`` alone, for steps that run before the queries exist."""
    path = root / MANIFEST
    if not path.exists():
        raise DatasetError(f'{path} not found')
    try:
        return Manifest.model_validate(yaml.safe_load(path.read_text(encoding='utf-8')))
    except pydantic.ValidationError as error:
        raise DatasetError(f'{path}: {error}') from error


def write_spec_set(root: pathlib.Path, spec_set: SpecSet) -> None:
    """Write ``spec_set`` in the layout ``load`` reads."""
    directory = root / spec_set.name
    (directory / QRELS).mkdir(parents=True, exist_ok=True)
    with (directory / QUERIES).open('w', encoding='utf-8') as file:
        for query in spec_set.queries.values():
            record = {'_id': query.id, 'text': query.text, 'metadata': {'style': query.style}}
            file.write(json.dumps(record, ensure_ascii=False) + '\n')
    for split in SPLITS:
        with (directory / QRELS / f'{split}.tsv').open('w', encoding='utf-8', newline='') as file:
            writer = csv.writer(file, delimiter='\t', lineterminator='\n')
            writer.writerow(QRELS_HEADER)
            for query_id, judgements in spec_set.qrels[split].items():
                for operation, grade in sorted(judgements.items(), key=lambda item: (-item[1], item[0])):
                    writer.writerow([query_id, operation, grade])


def _load_spec_set(root: pathlib.Path, name: str) -> SpecSet:
    directory = root / name
    queries = _load_queries(directory / QUERIES)
    qrels: dict[Split, dict[str, dict[str, int]]] = {
        split: _load_qrels(directory / QRELS / f'{split}.tsv', queries) for split in SPLITS
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
        if reader.fieldnames != QRELS_HEADER:
            raise DatasetError(f'{path}: the header must be {", ".join(QRELS_HEADER)}')
        for row in reader:
            where = f'{path}:{reader.line_num}'
            query_id, operation, grade = row['query-id'], row['corpus-id'], int(row['score'])
            if query_id not in queries:
                raise DatasetError(f'{where}: unknown query id {query_id!r}')
            if grade not in GRADES:
                raise DatasetError(f'{where}: grade {grade} is not one of {list(GRADES)}')
            qrels.setdefault(query_id, {})[operation] = grade
    without_answer = sorted(query_id for query_id, judged in qrels.items() if ANSWER_GRADE not in judged.values())
    if without_answer:
        raise DatasetError(f'{path}: queries {without_answer} have no operation graded {ANSWER_GRADE}')
    return qrels
