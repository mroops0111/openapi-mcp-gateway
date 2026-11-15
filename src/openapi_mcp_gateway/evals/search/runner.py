import dataclasses
import logging
import pathlib
import typing

from ...settings import ServerConfig
from .. import corpus
from .dataset import Benchmark, DatasetError, Query, Split, Style
from .scoring import DEPTH, score_all
from .systems import SpecCorpus, System, served_operations


logger = logging.getLogger(__name__)

# The server config a spec's operations are listed under, before any system narrows or renames them.
_CORPUS_SERVER = {'exposure': {'style': 'dynamic'}}

# System name, then every query result for it.
Results = dict[str, list['QueryResult']]


@dataclasses.dataclass(frozen=True)
class QueryResult:
    """One query's ranking under one system, and its score on every metric."""

    spec: str
    query_id: str
    style: Style
    ranking: list[str]
    scores: dict[str, float]


def build_systems(benchmark: Benchmark, names: typing.Sequence[str] | None = None) -> list[System]:
    """The systems the manifest declares, or only those ``names``, raising ``DatasetError`` for an unknown one."""
    declared = benchmark.manifest.systems
    chosen = list(names) if names else list(declared)
    unknown = [name for name in chosen if name not in declared]
    if unknown:
        raise DatasetError(f'Unknown system(s) {unknown}; the benchmark declares {sorted(declared)}')
    return [System.from_spec(name, declared[name].kind, declared[name].config) for name in chosen]


def corpus_server(benchmark: Benchmark, spec: str) -> ServerConfig:
    """The server config that lists ``spec``'s operations, with its pinned document fetched to a local file."""
    path = corpus.fetch(benchmark.manifest.specs[spec], benchmark.root)
    return ServerConfig.model_validate({**_CORPUS_SERVER, 'name': spec, 'spec': str(path)})


async def load_corpus(benchmark: Benchmark, spec: str) -> SpecCorpus:
    """List ``spec``'s operations, checking every judged one is among them.

    A judgement no system could retrieve would quietly lower every score, so it is refused instead.
    """
    server = corpus_server(benchmark, spec)
    operations = await served_operations(server)
    missing = sorted(benchmark.specs[spec].judged_operations() - set(operations))
    if missing:
        raise DatasetError(f'{spec} does not serve the judged operation(s) {missing}')
    return SpecCorpus(name=spec, spec_path=pathlib.Path(server.spec), operations=operations)


async def run(benchmark: Benchmark, split: Split, systems: typing.Sequence[System]) -> Results:
    """Rank every query of ``split`` under each system, spec by spec."""
    results: Results = {system.name: [] for system in systems}
    for spec, spec_set in benchmark.specs.items():
        spec_corpus = await load_corpus(benchmark, spec)
        queries = spec_set.split(split)
        for system in systems:
            async with system.open(spec_corpus) as retriever:
                for query, judgements in queries:
                    ranking = await retriever.rank(query.text, DEPTH)
                    results[system.name].append(_result(spec, query, ranking, judgements))
            logger.info('Ranked %d %s queries of %s under %s', len(queries), split, spec, system.name)
    return results


def _result(spec: str, query: Query, ranking: list[str], judgements: dict[str, int]) -> QueryResult:
    return QueryResult(
        spec=spec, query_id=query.id, style=query.style, ranking=ranking, scores=score_all(ranking, judgements)
    )
