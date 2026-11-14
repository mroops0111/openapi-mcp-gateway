import dataclasses
import logging
import typing

from ...settings import ServerConfig
from .. import corpus, target
from .dataset import Benchmark, DatasetError, Split, Style
from .metrics import DEPTH, METRICS, score
from .retriever import GatewaySearch, RandomRanking, Retriever, list_operations


logger = logging.getLogger(__name__)

# The floor every run reports beside the configured variants. No variant may take the name.
RANDOM = 'random'


@dataclasses.dataclass(frozen=True)
class QueryResult:
    """One query's ranking under one system, and its score on every metric."""

    spec: str
    query_id: str
    style: Style
    ranking: list[str]
    scores: dict[str, float]


# Variant name, then every query result for it.
Results = dict[str, list[QueryResult]]


def server_config(benchmark: Benchmark, spec: str, variant: str) -> ServerConfig:
    """The server config that serves ``spec`` under ``variant``, with the pinned spec fetched to a local file."""
    pin = benchmark.manifest.specs[spec]
    path = corpus.fetch(pin, benchmark.root)
    return ServerConfig.model_validate({**benchmark.manifest.variants[variant], 'name': spec, 'spec': str(path)})


async def run(
    benchmark: Benchmark,
    split: Split,
    variants: typing.Sequence[str] | None = None,
    *,
    include_random: bool = True,
) -> Results:
    """Rank every query of ``split`` under each variant, and under the random floor unless told not to.

    Raises ``DatasetError`` when a judged operation is not one the gateway serves,
    since a judgement nothing can retrieve would quietly lower every score.
    """
    names = list(variants) if variants is not None else list(benchmark.manifest.variants)
    unknown = [name for name in names if name not in benchmark.manifest.variants]
    if unknown:
        raise DatasetError(f'Unknown variant(s) {unknown}; the benchmark defines {sorted(benchmark.manifest.variants)}')
    if RANDOM in benchmark.manifest.variants:
        raise DatasetError(f'No variant may be named {RANDOM!r}, which is the built-in floor')

    results: Results = {name: [] for name in names}
    if include_random:
        results[RANDOM] = []
    for spec, spec_set in benchmark.specs.items():
        queries = spec_set.split(split)
        for name in names:
            async with target.connect(server_config(benchmark, spec, name)) as client:
                operations = await list_operations(client)
                missing = _unserved_judgements(queries, set(operations))
                if not missing:
                    results[name].extend(await _rank_all(GatewaySearch(client), spec, queries))
            # Raised outside the client, whose task group would otherwise wrap it in an exception group.
            if missing:
                raise DatasetError(f'{spec} under {name} does not serve the judged operation(s) {missing}')
            if include_random and name == names[0]:
                results[RANDOM].extend(await _rank_all(RandomRanking(operations), spec, queries))
            logger.info('Ranked %d %s queries of %s under %s', len(queries), split, spec, name)
    return results


async def _rank_all(
    retriever: Retriever, spec: str, queries: typing.Sequence[tuple[typing.Any, dict[str, int]]]
) -> list[QueryResult]:
    results = []
    for query, judgements in queries:
        ranking = await retriever.rank(query.text, DEPTH)
        results.append(
            QueryResult(
                spec=spec,
                query_id=query.id,
                style=query.style,
                ranking=ranking,
                scores={metric.name: score(metric, ranking, judgements) for metric in METRICS},
            )
        )
    return results


def _unserved_judgements(queries: typing.Sequence[tuple[typing.Any, dict[str, int]]], served: set[str]) -> list[str]:
    return sorted({name for _, judgements in queries for name in judgements} - served)
