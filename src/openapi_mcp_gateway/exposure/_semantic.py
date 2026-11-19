import array
import asyncio
import base64
import collections
import dataclasses
import hashlib
import json
import logging
import math
import operator
import os
import pathlib
import tempfile
import time
import typing


logger = logging.getLogger(__name__)

# How many operations the embedding ranks for one query, enough to fuse with the word ranking.
# Past that, the nearest vectors share little meaning with the query, and ranking all of them only adds noise.
SEMANTIC_DEPTH = 50

# How many operations one embedding request carries.
# With each text cut to _EMBEDDING_TEXT_LIMIT, a full batch stays under OpenAI's 300,000 tokens per request.
_EMBEDDING_BATCH_SIZE = 64

# The longest text sent to the model, in characters, about 2,000 tokens, under every provider's input limit.
# Not every provider truncates on its own, and one text over the limit would fail its whole batch.
_EMBEDDING_TEXT_LIMIT = 8000

# How long one embedding request may take, in seconds, before search ranks by words instead.
# A provider's own client waits up to ten minutes and retries, far too long for a tool call.
_QUERY_TIMEOUT_SECONDS = 10
_DOCUMENTS_TIMEOUT_SECONDS = 60

# How long after a failed request search skips the model, in seconds, so an outage costs one wait, not one per search.
_RETRY_AFTER_SECONDS = 60

# How many query vectors are kept, so paging through one search embeds its query once.
# Embeddings are not bit-for-bit repeatable, and embedding again could reorder close results between pages.
_QUERY_CACHE_SIZE = 256

# Bumped when the cache file's layout changes, so an older file is ignored rather than misread.
_CACHE_VERSION = 1


class Embedder(typing.Protocol):
    """What search needs from an embedding model, as Pydantic AI's ``Embedder`` provides it.

    Each call returns a result whose ``embeddings`` hold one vector per text, in order.
    """

    async def embed_query(self, query: typing.Sequence[str], *, settings: typing.Any = None) -> typing.Any: ...

    async def embed_documents(self, documents: typing.Sequence[str], *, settings: typing.Any = None) -> typing.Any: ...


@dataclasses.dataclass(frozen=True)
class SemanticSearchOptions:
    """How a server searches by meaning, as its config's ``exposure.search.semantic`` sets it.

    ``model`` names ``embedder`` in the cache, so vectors from another model are never reused.
    """

    model: str
    embedder: Embedder
    dimensions: int | None = None
    cache_directory: pathlib.Path | None = None
    # semantic ranks by the embedding alone, and hybrid merges that ranking with the word ranking.
    # semantic is the default, since on the search benchmark's dev split it ranked better than hybrid.
    retrieval: typing.Literal['semantic', 'hybrid'] = 'semantic'


class SemanticIndex:
    """Rank documents by how close their embeddings are to a query's, through the options' embedding model.

    The documents are embedded on the first search rather than at startup, since that needs the network.
    Each vector is cached in memory, and in ``cache_directory`` when given,
    keyed by the model, the dimensions, and the document's text, so a restart embeds only the documents that changed.
    """

    def __init__(self, documents: typing.Mapping[str, str], options: SemanticSearchOptions):
        self._documents = {name: text[:_EMBEDDING_TEXT_LIMIT] for name, text in documents.items()}
        self._model = options.model
        self._embedder = options.embedder
        self._dimensions = options.dimensions
        self._cache_directory = options.cache_directory
        # Vectors embedded or read so far, by cache key, kept across a failed attempt so a retry resumes.
        self._known: dict[str, array.array[float]] | None = None
        self._vectors: dict[str, array.array[float]] | None = None
        self._query_vectors: collections.OrderedDict[str, array.array[float]] = collections.OrderedDict()
        self._retry_at = 0.0
        self._lock = asyncio.Lock()

    async def search(
        self, queries: typing.Sequence[str], candidates: typing.Container[str] | None = None
    ) -> list[list[str]]:
        """Return, for each of ``queries``, the names of the ``SEMANTIC_DEPTH`` documents nearest it, best first.

        ``candidates``, when given, limits each ranking to those names.
        Ties keep the order the documents were given in.
        Raises when the model fails or timed out within the last ``_RETRY_AFTER_SECONDS``.
        """
        if time.monotonic() < self._retry_at:
            raise RuntimeError('the embedding model failed recently, and is tried again shortly')
        try:
            vectors = await self._document_vectors()
            query_vectors = await self._embed_queries(queries)
        except Exception:
            self._retry_at = time.monotonic() + _RETRY_AFTER_SECONDS
            raise
        # A few thousand dot products in pure Python take tens of milliseconds, so they run off the event loop.
        return await asyncio.to_thread(_rank_all, query_vectors, vectors, candidates)

    async def _embed_queries(self, queries: typing.Sequence[str]) -> 'list[array.array[float]]':
        """Each query's normalized vector, embedding the ones not cached in one request."""
        texts = [query[:_EMBEDDING_TEXT_LIMIT] for query in queries]
        missing = list(dict.fromkeys(text for text in texts if text not in self._query_vectors))
        if missing:
            result = await asyncio.wait_for(
                self._embedder.embed_query(missing, settings=self._settings()), _QUERY_TIMEOUT_SECONDS
            )
            for text, embedding in zip(missing, result.embeddings, strict=True):
                self._query_vectors[text] = _normalized(embedding)
        for text in texts:
            self._query_vectors.move_to_end(text)
        while len(self._query_vectors) > _QUERY_CACHE_SIZE:
            self._query_vectors.popitem(last=False)
        return [self._query_vectors[text] for text in texts]

    async def _document_vectors(self) -> 'dict[str, array.array[float]]':
        """Each document's normalized vector, embedding on first use whatever the cache does not hold."""
        if self._vectors is not None:
            return self._vectors
        async with self._lock:
            if self._vectors is not None:
                return self._vectors
            keys = {name: self._cache_key(text) for name, text in self._documents.items()}
            if self._known is None:
                self._known = await asyncio.to_thread(self._read_cache)
            known = self._known
            missing = [name for name, key in keys.items() if key not in known]
            embedded = 0
            try:
                for start in range(0, len(missing), _EMBEDDING_BATCH_SIZE):
                    batch = missing[start : start + _EMBEDDING_BATCH_SIZE]
                    result = await asyncio.wait_for(
                        self._embedder.embed_documents(
                            [self._documents[name] for name in batch], settings=self._settings()
                        ),
                        _DOCUMENTS_TIMEOUT_SECONDS,
                    )
                    for name, embedding in zip(batch, result.embeddings, strict=True):
                        known[keys[name]] = _normalized(embedding)
                    embedded += len(batch)
            finally:
                # The batches that succeeded are kept on disk too, so a restart after a failure resumes as well.
                if embedded:
                    logger.info('Embedded %d operation(s) with %s', embedded, self._model)
                    current = {key: known[key] for key in keys.values() if key in known}
                    await asyncio.to_thread(self._write_cache, current)
            self._vectors = {name: known[key] for name, key in keys.items()}
            return self._vectors

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
            if not isinstance(stored, dict) or stored.get('version') != _CACHE_VERSION:
                return {}
            vectors = stored.get('vectors')
            if not isinstance(vectors, dict):
                raise ValueError('vectors is not an object')
            return {key: _decode_vector(value) for key, value in vectors.items()}
        except (OSError, ValueError, TypeError) as error:
            logger.warning('Ignoring the unreadable embedding cache %s: %s', path, error)
            return {}

    def _write_cache(self, vectors: 'dict[str, array.array[float]]') -> None:
        """Keep ``vectors`` in the cache file, replacing it whole, so it holds only the current operations.

        Each writer has its own temporary file, so replicas sharing the directory never mix their writes.
        """
        path = self._cache_path()
        if path is None:
            return
        stored = {'version': _CACHE_VERSION, 'vectors': {key: _encode_vector(value) for key, value in vectors.items()}}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'{path.stem}-', suffix='.tmp')
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
                    json.dump(stored, file)
                os.replace(temporary, path)
            except BaseException:
                pathlib.Path(temporary).unlink(missing_ok=True)
                raise
        except OSError as error:
            logger.warning('Could not write the embedding cache %s: %s', path, error)


def embeddings_available() -> bool:
    """Whether the ``embeddings`` extra is installed, which semantic search needs."""
    try:
        import pydantic_ai  # noqa: F401
    except ImportError:
        return False
    return True


def build_embedder(model: str) -> Embedder:
    """The Pydantic AI embedding model ``model`` names, such as ``openai:text-embedding-3-small``.

    It is checked now, so a wrong name, a missing key or address, or a missing provider package raises at load.
    """
    from pydantic_ai import Embedder as PydanticAIEmbedder

    return PydanticAIEmbedder(model, defer_model_check=False)


def _rank_all(
    query_vectors: 'list[array.array[float]]',
    vectors: 'dict[str, array.array[float]]',
    candidates: typing.Container[str] | None,
) -> list[list[str]]:
    names = [name for name in vectors if candidates is None or name in candidates]
    rankings = []
    for query_vector in query_vectors:
        # sorted is stable, so ties keep the order the documents were given in.
        ranked = sorted(names, key=lambda name: -_dot(query_vector, vectors[name]))
        rankings.append(ranked[:SEMANTIC_DEPTH])
    return rankings


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
