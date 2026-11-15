import logging
import pathlib
import typing

import pydantic

from ...settings import ServerConfig
from .dataset import Benchmark, DatasetError, JudgedQuery, Split, Style
from .scoring import RANKING_DEPTH, score_all
from .systems import SpecCorpus, System, served_operations


logger = logging.getLogger(__name__)

# The server config a spec's operations are listed under, before any system narrows or renames them.
CORPUS_SERVER = {'exposure': {'style': 'dynamic'}}

# System name, then every query result for it.
Results = dict[str, list['QueryResult']]


class QueryResult(pydantic.BaseModel):
    """One query's ranking under one system, and its score on every metric."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    spec: str
    query_id: str
    style: Style
    ranking: list[str]
    scores: dict[str, float]


def select_systems(benchmark: Benchmark, names: typing.Sequence[str] = ()) -> dict[str, System]:
    """The systems the manifest declares, or only those ``names``, raising ``DatasetError`` for an unknown one."""
    declared = benchmark.manifest.systems
    unknown = [name for name in names if name not in declared]
    if unknown:
        raise DatasetError(f'Unknown system(s) {unknown}; the benchmark declares {sorted(declared)}')
    return {name: declared[name] for name in names} if names else dict(declared)


def corpus_server(benchmark: Benchmark, spec: str) -> ServerConfig:
    """The server config that lists ``spec``'s operations, with its pinned document fetched to a local file."""
    path = benchmark.manifest.specs[spec].fetch(benchmark.root)
    return ServerConfig.model_validate({**CORPUS_SERVER, 'name': spec, 'spec': str(path)})


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


async def run(benchmark: Benchmark, split: Split, systems: typing.Mapping[str, System]) -> Results:
    """Rank every query of ``split`` under each system, spec by spec."""
    results: Results = {name: [] for name in systems}
    for spec, spec_dataset in benchmark.specs.items():
        corpus = await load_corpus(benchmark, spec)
        judged_queries = spec_dataset.judged_queries(split)
        for name, system in systems.items():
            async with system.open(corpus) as retriever:
                for judged_query in judged_queries:
                    ranking = await retriever.rank(judged_query.query.text, RANKING_DEPTH)
                    results[name].append(_query_result(spec, judged_query, ranking))
            logger.info('Ranked %d %s queries of %s under %s', len(judged_queries), split, spec, name)
    return results


def _query_result(spec: str, judged_query: JudgedQuery, ranking: list[str]) -> QueryResult:
    return QueryResult(
        spec=spec,
        query_id=judged_query.query.id,
        style=judged_query.query.style,
        ranking=ranking,
        scores=score_all(ranking, judged_query.judgements),
    )
