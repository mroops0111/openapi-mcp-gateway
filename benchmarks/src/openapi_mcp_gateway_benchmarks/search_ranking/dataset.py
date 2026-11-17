import csv
import json
import pathlib
import typing

import pydantic
import yaml

from ..errors import BenchmarkDataError
from ..spec_pins import SpecPin
from .metrics import ANSWER_GRADE
from .scoring import METRIC_NAMES
from .systems import GatewaySystem, PydanticAIKeywordSystem, RandomSystem


MANIFEST_FILE = 'benchmark.yaml'
BASELINE_FILE = 'baseline.json'
QUERIES_FILE = 'queries.jsonl'
# BEIR names its judgement files qrels, for query relevance judgements, and keeps the name here so its tools read them.
QRELS_DIRECTORY = 'qrels'
QRELS_HEADER = ['query-id', 'corpus-id', 'score']

Style = typing.Literal['literal', 'paraphrase', 'indirect']
Split = typing.Literal['dev', 'test']
STYLES: tuple[Style, ...] = typing.get_args(Style)
SPLITS: tuple[Split, ...] = typing.get_args(Split)
GRADES = (1, ANSWER_GRADE)


class DatasetError(BenchmarkDataError):
    """The search ranking benchmark's files are inconsistent."""


class Gate(pydantic.BaseModel):
    """What CI holds a change to, which is these metrics of one system, on one split, against the baseline."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    system: str
    split: Split = 'test'
    metrics: tuple[str, ...] = ('ndcg@10', 'recall@20')
    # How far a metric may fall below the baseline before the gate fails.
    tolerance: float = pydantic.Field(default=0.01, ge=0)

    @pydantic.field_validator('metrics')
    @classmethod
    def _validate_metrics(cls, metrics: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(metrics) - set(METRIC_NAMES))
        if unknown:
            raise ValueError(f'Unknown metric(s) {unknown}; choose from {list(METRIC_NAMES)}')
        return metrics


class Manifest(pydantic.BaseModel):
    """The manifest in ``benchmark.yaml``, naming the specs, the systems to compare on them, and the CI gate.

    Each system's ``type`` picks its class, so a new kind of system joins the union below.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    version: int = pydantic.Field(ge=1)
    specs: dict[str, SpecPin] = pydantic.Field(min_length=1)
    systems: dict[
        str,
        typing.Annotated[GatewaySystem | RandomSystem | PydanticAIKeywordSystem, pydantic.Field(discriminator='type')],
    ] = pydantic.Field(min_length=1)
    gate: Gate

    @pydantic.model_validator(mode='after')
    def _validate_gate_system(self) -> typing.Self:
        if self.gate.system not in self.systems:
            raise ValueError(f'gate.system {self.gate.system!r} is not one of the systems {sorted(self.systems)}')
        return self


class Query(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    id: str
    text: str = pydantic.Field(min_length=1)
    style: Style


class JudgedQuery(pydantic.BaseModel):
    """A query with its judgements, a grade per relevant operation name."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    query: Query
    judgements: dict[str, int]


class SpecDataset(pydantic.BaseModel):
    """One spec's queries and the judgements for each split."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    name: str
    queries: dict[str, Query]
    # Split, then query id, then operation name, to its grade.
    judgements: dict[Split, dict[str, dict[str, int]]]

    def judged_queries(self, split: Split) -> list[JudgedQuery]:
        """The queries of ``split`` with their judgements, in file order."""
        return [
            JudgedQuery(query=self.queries[query_id], judgements=judgements)
            for query_id, judgements in self.judgements[split].items()
        ]

    def judged_operations(self) -> set[str]:
        """Every operation any split judges relevant."""
        return {name for by_query in self.judgements.values() for judged in by_query.values() for name in judged}


class Benchmark(pydantic.BaseModel):
    """A benchmark directory, loaded and validated."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    root: pathlib.Path
    manifest: Manifest
    specs: dict[str, SpecDataset]

    @property
    def baseline_path(self) -> pathlib.Path:
        return self.root / BASELINE_FILE


def load(root: pathlib.Path) -> Benchmark:
    """Load and validate the benchmark in ``root``, raising ``DatasetError`` on any inconsistency.

    Each spec's files follow BEIR's layout, so other retrieval tools read them unchanged.
    ``<spec>/queries.jsonl`` holds one query per line, and ``<spec>/qrels/<split>.tsv`` holds its graded judgements,
    each corpus id an operation name as ``search_operations`` returns it.
    """
    manifest = load_manifest(root)
    return Benchmark(
        root=root, manifest=manifest, specs={name: _load_spec_dataset(root, name) for name in manifest.specs}
    )


def load_manifest(root: pathlib.Path) -> Manifest:
    """Load ``benchmark.yaml`` alone, for steps that run before the queries exist."""
    path = root / MANIFEST_FILE
    if not path.exists():
        raise DatasetError(f'{path} not found')
    try:
        return Manifest.model_validate(yaml.safe_load(path.read_text(encoding='utf-8')))
    except pydantic.ValidationError as error:
        raise DatasetError(f'{path}: {error}') from error


def write_spec_dataset(root: pathlib.Path, spec_dataset: SpecDataset) -> None:
    """Write ``spec_dataset`` in the layout ``load`` reads."""
    directory = root / spec_dataset.name
    (directory / QRELS_DIRECTORY).mkdir(parents=True, exist_ok=True)
    with (directory / QUERIES_FILE).open('w', encoding='utf-8') as file:
        for query in spec_dataset.queries.values():
            record = {'_id': query.id, 'text': query.text, 'metadata': {'style': query.style}}
            file.write(json.dumps(record, ensure_ascii=False) + '\n')
    for split in SPLITS:
        with (directory / QRELS_DIRECTORY / f'{split}.tsv').open('w', encoding='utf-8', newline='') as file:
            writer = csv.writer(file, delimiter='\t', lineterminator='\n')
            writer.writerow(QRELS_HEADER)
            for query_id, judgements in spec_dataset.judgements[split].items():
                for operation, grade in sorted(judgements.items(), key=lambda item: (-item[1], item[0])):
                    writer.writerow([query_id, operation, grade])


def _load_spec_dataset(root: pathlib.Path, name: str) -> SpecDataset:
    directory = root / name
    queries = _load_queries(directory / QUERIES_FILE)
    judgements: dict[Split, dict[str, dict[str, int]]] = {
        split: _load_judgements(directory / QRELS_DIRECTORY / f'{split}.tsv', queries) for split in SPLITS
    }
    in_both = set(judgements['dev']) & set(judgements['test'])
    if in_both:
        raise DatasetError(f'{name}: queries {sorted(in_both)} are judged in both dev and test')
    unjudged = set(queries) - set(judgements['dev']) - set(judgements['test'])
    if unjudged:
        raise DatasetError(f'{name}: queries {sorted(unjudged)} have no judgements')
    return SpecDataset(name=name, queries=queries, judgements=judgements)


def _load_queries(path: pathlib.Path) -> dict[str, Query]:
    queries: dict[str, Query] = {}
    for line_number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        if not line.strip():
            continue
        location = f'{path}:{line_number}'
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise DatasetError(f'{location}: {error}') from error
        try:
            query = Query.model_validate(
                {
                    'id': record.get('_id'),
                    'text': record.get('text'),
                    'style': (record.get('metadata') or {}).get('style'),
                }
            )
        except pydantic.ValidationError as error:
            raise DatasetError(f'{location}: {error}') from error
        if query.id in queries:
            raise DatasetError(f'{location}: duplicate query id {query.id!r}')
        queries[query.id] = query
    return queries


def _load_judgements(path: pathlib.Path, queries: typing.Mapping[str, Query]) -> dict[str, dict[str, int]]:
    judgements: dict[str, dict[str, int]] = {}
    with path.open(encoding='utf-8', newline='') as file:
        reader = csv.DictReader(file, delimiter='\t')
        if reader.fieldnames != QRELS_HEADER:
            raise DatasetError(f'{path}: the header must be {", ".join(QRELS_HEADER)}')
        for row in reader:
            location = f'{path}:{reader.line_num}'
            query_id, operation, score = row['query-id'], row['corpus-id'], row['score']
            if not score.isdigit():
                raise DatasetError(f'{location}: score {score!r} is not a whole number')
            grade = int(score)
            if query_id not in queries:
                raise DatasetError(f'{location}: unknown query id {query_id!r}')
            if grade not in GRADES:
                raise DatasetError(f'{location}: grade {grade} is not one of {list(GRADES)}')
            judgements.setdefault(query_id, {})[operation] = grade
    without_answer = sorted(query_id for query_id, judged in judgements.items() if ANSWER_GRADE not in judged.values())
    if without_answer:
        raise DatasetError(f'{path}: queries {without_answer} have no operation graded {ANSWER_GRADE}')
    return judgements
