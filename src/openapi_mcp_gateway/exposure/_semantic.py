import array
import asyncio
import base64
import dataclasses
import hashlib
import json
import logging
import math
import operator
import pathlib
import typing


if typing.TYPE_CHECKING:
    from pydantic_ai import Embedder


logger = logging.getLogger(__name__)

# How many operations the embedding ranks for one query, enough to fuse with the word ranking.
# Past that, the nearest vectors share little meaning with the query, and ranking all of them only adds noise.
SEMANTIC_DEPTH = 50

# How many operations one embedding request carries, under every provider's own limit.
_EMBEDDING_BATCH_SIZE = 256

# Bumped when the cache file's layout changes, so an older file is ignored rather than misread.
_CACHE_VERSION = 1


@dataclasses.dataclass(frozen=True)
class SemanticSearchOptions:
    """How a server searches by meaning, as its config's ``exposure.search.semantic`` sets it."""

    model: str
    dimensions: int | None = None
    cache_directory: pathlib.Path | None = None
    # semantic ranks by the embedding alone, and hybrid merges that ranking with the word ranking.
    # semantic is the default, since on the search benchmark's dev split it ranked better than hybrid.
    retrieval: typing.Literal['semantic', 'hybrid'] = 'semantic'


class SemanticIndex:
    """Rank documents by how close their embeddings are to a query's, through any Pydantic AI embedding model.

    The documents are embedded on the first search rather than at startup, since that needs the network.
    Each vector is cached in memory, and in ``cache_directory`` when given,
    keyed by the model, the dimensions, and the document's text, so a restart embeds only the documents that changed.
    """

    def __init__(
        self,
        documents: typing.Mapping[str, str],
        model: str,
        *,
        dimensions: int | None = None,
        cache_directory: pathlib.Path | None = None,
    ):
        self._documents = dict(documents)
        self._model = model
        self._dimensions = dimensions
        self._cache_directory = cache_directory
        self._vectors: dict[str, array.array[float]] | None = None
        self._lock = asyncio.Lock()
        self._embedder: Embedder | None = None

    async def search(self, query: str, candidates: typing.Container[str] | None = None) -> list[str]:
        """Return the names of the ``SEMANTIC_DEPTH`` documents nearest ``query``, best first.

        ``candidates``, when given, limits the result to those names.
        Ties keep the order the documents were given in.
        """
        vectors = await self._document_vectors()
        result = await self._get_embedder().embed_query(query, settings=self._settings())
        query_vector = _normalized(result.embeddings[0])
        scored = [
            (-_dot(query_vector, vector), position)
            for position, (name, vector) in enumerate(vectors.items())
            if candidates is None or name in candidates
        ]
        scored.sort()
        names = list(vectors)
        return [names[position] for _, position in scored[:SEMANTIC_DEPTH]]

    async def _document_vectors(self) -> 'dict[str, array.array[float]]':
        """Each document's normalized vector, embedding on first use whatever the cache does not hold."""
        if self._vectors is not None:
            return self._vectors
        async with self._lock:
            if self._vectors is not None:
                return self._vectors
            keys = {name: self._cache_key(text) for name, text in self._documents.items()}
            cached = self._read_cache()
            missing = [name for name, key in keys.items() if key not in cached]
            for start in range(0, len(missing), _EMBEDDING_BATCH_SIZE):
                batch = missing[start : start + _EMBEDDING_BATCH_SIZE]
                result = await self._get_embedder().embed_documents(
                    [self._documents[name] for name in batch], settings=self._settings()
                )
                for name, embedding in zip(batch, result.embeddings, strict=True):
                    cached[keys[name]] = _normalized(embedding)
            if missing:
                logger.info('Embedded %d operation(s) with %s', len(missing), self._model)
                self._write_cache({key: cached[key] for key in keys.values()})
            self._vectors = {name: cached[key] for name, key in keys.items()}
            return self._vectors

    def _get_embedder(self) -> 'Embedder':
        if self._embedder is None:
            self._embedder = _build_embedder(self._model)
        return self._embedder

    def _settings(self) -> typing.Any:
        return {'dimensions': self._dimensions} if self._dimensions else None

    def _cache_key(self, text: str) -> str:
        return hashlib.sha256(f'{self._model}\0{self._dimensions}\0{text}'.encode()).hexdigest()

    def _cache_path(self) -> pathlib.Path | None:
        if self._cache_directory is None:
            return None
        model_key = hashlib.sha256(f'{self._model}\0{self._dimensions}'.encode()).hexdigest()[:16]
        return self._cache_directory / f'embeddings-{model_key}.json'

    def _read_cache(self) -> 'dict[str, array.array[float]]':
        """The vectors the cache file holds, or none when there is no usable file."""
        path = self._cache_path()
        if path is None or not path.exists():
            return {}
        try:
            stored = json.loads(path.read_text(encoding='utf-8'))
            if stored.get('version') != _CACHE_VERSION:
                return {}
            return {key: _decode_vector(value) for key, value in stored['vectors'].items()}
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning('Ignoring the unreadable embedding cache %s: %s', path, error)
            return {}

    def _write_cache(self, vectors: 'dict[str, array.array[float]]') -> None:
        """Keep ``vectors`` in the cache file, replacing it whole, so it holds only the current operations."""
        path = self._cache_path()
        if path is None:
            return
        stored = {'version': _CACHE_VERSION, 'vectors': {key: _encode_vector(value) for key, value in vectors.items()}}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(stored), encoding='utf-8')
            temporary.replace(path)
        except OSError as error:
            logger.warning('Could not write the embedding cache %s: %s', path, error)


def embeddings_available() -> bool:
    """Whether the ``embeddings`` extra is installed, which semantic search needs."""
    try:
        import pydantic_ai  # noqa: F401
    except ImportError:
        return False
    return True


def _build_embedder(model: str) -> 'Embedder':
    from pydantic_ai import Embedder

    return Embedder(model)


def _normalized(embedding: typing.Sequence[float]) -> 'array.array[float]':
    """``embedding`` scaled to length 1, so a dot product is the cosine similarity."""
    norm = math.sqrt(sum(value * value for value in embedding)) or 1.0
    return array.array('f', (value / norm for value in embedding))


def _dot(left: 'array.array[float]', right: 'array.array[float]') -> float:
    return sum(map(operator.mul, left, right))


def _encode_vector(vector: 'array.array[float]') -> str:
    return base64.b64encode(vector.tobytes()).decode()


def _decode_vector(encoded: str) -> 'array.array[float]':
    vector = array.array('f')
    vector.frombytes(base64.b64decode(encoded))
    return vector
