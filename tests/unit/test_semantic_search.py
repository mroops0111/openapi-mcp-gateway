import logging
import pathlib
import types
import typing

import pytest
from mcp import Client
from mcp.server import MCPServer

from openapi_mcp_gateway.exposure import MetaToolGenerator, SemanticSearchOptions, UpstreamConnection, _semantic
from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.openapi import OperationInfo
from openapi_mcp_gateway.settings import GatewayConfig
from tests.constants import API_URL


# Each concept is one axis of the fake embedding, and the words that mean it, so synonyms land together.
_CONCEPTS = (
    ('close', 'shut', 'resolve', 'update'),
    ('issue', 'ticket', 'bug'),
    ('comment', 'note', 'reply'),
    ('pet', 'dog', 'animal'),
)


class _ConceptEmbedder:
    """A stand-in embedding model that places text by the concepts its words name, and counts its calls."""

    def __init__(self, *, failing: bool = False):
        self.failing = failing
        self.embedded_documents: list[str] = []

    async def embed_documents(self, documents: typing.Sequence[str], settings: typing.Any = None) -> typing.Any:
        self.embedded_documents.extend(documents)
        return types.SimpleNamespace(embeddings=[_place(text) for text in documents])

    async def embed_query(self, query: str, settings: typing.Any = None) -> typing.Any:
        if self.failing:
            raise ConnectionError('embedding service unreachable')
        return types.SimpleNamespace(embeddings=[_place(query)])


@pytest.fixture
def embedder(monkeypatch: pytest.MonkeyPatch) -> _ConceptEmbedder:
    stand_in = _ConceptEmbedder()
    monkeypatch.setattr(_semantic, '_build_embedder', lambda model: stand_in)
    return stand_in


class TestSemanticSearch:
    """``exposure.search.semantic`` ranks by meaning, alone or merged with the word ranking."""

    async def test_hybrid_finds_a_synonym_words_miss(self, embedder):
        """A request in other words finds its operation through the embedding, while a literal one still ranks first."""
        mcp = _register(SemanticSearchOptions(model='stand-in', retrieval='hybrid'))
        assert (await _names(mcp, 'shut a ticket'))[0] == 'update_issue'
        assert (await _names(mcp, 'adopt pet'))[0] == 'adopt_pet'

    async def test_semantic_retrieval_ranks_by_meaning_alone(self, embedder):
        """By default, a request sharing no word with any operation still ranks them by meaning."""
        mcp = _register(SemanticSearchOptions(model='stand-in'))
        assert (await _names(mcp, 'leave a note'))[0] == 'add_comment'

    async def test_tags_limit_the_semantic_ranking(self, embedder):
        """Only operations carrying the tag are ranked, by meaning as by words."""
        mcp = _register(SemanticSearchOptions(model='stand-in'))
        assert await _names(mcp, 'shut a ticket', tags=['pets']) == ['adopt_pet']

    async def test_cache_file_spares_a_second_start(self, embedder, tmp_path: pathlib.Path):
        """A second server reading the same cache embeds no operation again."""
        options = SemanticSearchOptions(model='stand-in', cache_directory=tmp_path)
        await _names(_register(options), 'shut a ticket')
        assert len(embedder.embedded_documents) == 3
        assert len(list(tmp_path.glob('embeddings-*.json'))) == 1
        await _names(_register(options), 'shut a ticket')
        assert len(embedder.embedded_documents) == 3

    async def test_unreachable_model_falls_back_to_words(self, embedder, caplog):
        """When the embedding model fails, search answers by words and says why in the log."""
        embedder.failing = True
        mcp = _register(SemanticSearchOptions(model='stand-in', retrieval='hybrid'))
        with caplog.at_level(logging.WARNING):
            assert await _names(mcp, 'adopt pet') == ['adopt_pet']
        assert 'ranks by words alone' in caplog.text

    def test_refused_without_the_embeddings_extra(self, monkeypatch, petstore_json_path):
        """A config asking for semantic search fails to load when the extra is missing, naming it."""
        monkeypatch.setattr('openapi_mcp_gateway.gateway.embeddings_available', lambda: False)
        config = GatewayConfig.model_validate(
            {
                'servers': [
                    {
                        'name': 'petstore',
                        'spec': str(petstore_json_path),
                        'exposure': {'style': 'dynamic', 'search': {'semantic': {'model': 'openai:x'}}},
                    }
                ]
            }
        )
        with pytest.raises(ValueError, match=r'openapi-mcp-gateway\[embeddings\]'):
            Gateway.from_config(config)


def _place(text: str) -> list[float]:
    words = text.lower().split()
    return [float(sum(word.startswith(synonyms) for word in words)) + 0.01 for synonyms in _CONCEPTS]


def _register(options: SemanticSearchOptions) -> MCPServer:
    mcp = MCPServer('test')
    MetaToolGenerator(mcp=mcp, connection=UpstreamConnection(base_url=API_URL), semantic_search=options).register(
        [
            OperationInfo(
                operation_id='updateIssue',
                method='patch',
                path='/issues/{n}',
                summary='Update an issue',
                tags=['issues'],
            ),
            OperationInfo(
                operation_id='addComment',
                method='post',
                path='/issues/{n}/comments',
                summary='Add a comment',
                tags=['issues'],
            ),
            OperationInfo(operation_id='adoptPet', method='post', path='/pets', summary='Adopt a pet', tags=['pets']),
        ]
    )
    return mcp


async def _names(mcp: MCPServer, query: str, **arguments: typing.Any) -> list[str]:
    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool('search_operations', {'query': query, **arguments})
    return [entry['name'] for entry in result.structured_content['operations']]
