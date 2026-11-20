import logging
import pathlib
import types
import typing

import pytest
from mcp import Client
from mcp.server import MCPServer

from openapi_mcp_gateway.exposure import MetaToolGenerator, SemanticSearchOptions, UpstreamConnection
from openapi_mcp_gateway.exposure.meta_tool import _OperationRegistry, _RegisteredOperation
from openapi_mcp_gateway.gateway import Gateway, _semantic_search_options
from openapi_mcp_gateway.openapi import OperationInfo
from openapi_mcp_gateway.settings import GatewayConfig, SemanticSearchConfig
from tests.constants import API_URL


# Each concept is one axis of the fake embedding, and the words that mean it, so synonyms land together.
_CONCEPTS = (
    ('close', 'shut', 'resolve', 'update'),
    ('issue', 'ticket', 'bug'),
    ('comment', 'note', 'reply'),
    ('pet', 'dog', 'animal'),
)


@pytest.fixture
def embedder() -> '_ConceptEmbedder':
    return _ConceptEmbedder()


class TestSemanticSearch:
    """``exposure.search.semantic`` ranks by meaning, alone or merged with the word ranking."""

    async def test_hybrid_finds_a_synonym_words_miss(self, embedder):
        """A request in other words finds its operation through the embedding, while a literal one still ranks first."""
        mcp = _register(SemanticSearchOptions(model='stand-in', embedder=embedder, retrieval='hybrid'))
        assert (await _names(mcp, 'shut a ticket'))[0] == 'update_issue'
        assert (await _names(mcp, 'adopt pet'))[0] == 'adopt_pet'

    async def test_semantic_retrieval_ranks_by_meaning_alone(self, embedder):
        """By default, a request sharing no word with any operation still ranks them by meaning."""
        mcp = _register(SemanticSearchOptions(model='stand-in', embedder=embedder))
        assert (await _names(mcp, 'leave a note'))[0] == 'add_comment'

    async def test_tags_limit_the_semantic_ranking(self, embedder):
        """Only operations carrying the tag are ranked, by meaning as by words."""
        mcp = _register(SemanticSearchOptions(model='stand-in', embedder=embedder))
        assert await _names(mcp, 'shut a ticket', tags=['pets']) == ['adopt_pet']

    async def test_cache_file_spares_a_second_start(self, embedder, tmp_path: pathlib.Path):
        """A second server reading the same cache embeds no operation again."""
        options = SemanticSearchOptions(model='stand-in', embedder=embedder, cache_directory=tmp_path)
        await _names(_register(options), 'shut a ticket')
        assert len(embedder.embedded_documents) == 3
        assert len(list(tmp_path.glob('embeddings-*.json'))) == 1
        await _names(_register(options), 'shut a ticket')
        assert len(embedder.embedded_documents) == 3

    async def test_unreachable_model_falls_back_to_words(self, embedder, caplog):
        """When the embedding model fails, search answers by words and says why in the log."""
        embedder.failing = True
        mcp = _register(SemanticSearchOptions(model='stand-in', embedder=embedder, retrieval='hybrid'))
        with caplog.at_level(logging.WARNING):
            assert await _names(mcp, 'adopt pet') == ['adopt_pet']
        assert 'ranks by words alone' in caplog.text

    async def test_failed_model_is_skipped_for_a_while(self, embedder):
        """After the model fails, the next searches rank by words at once rather than wait on it again."""
        embedder.failing = True
        mcp = _register(SemanticSearchOptions(model='stand-in', embedder=embedder))
        await _names(mcp, 'adopt pet')
        await _names(mcp, 'adopt pet')
        assert embedder.query_requests == 1

    async def test_several_queries_share_one_request(self, embedder):
        """A list of phrasings is embedded in one request, and paging through it embeds nothing again."""
        mcp = _register(SemanticSearchOptions(model='stand-in', embedder=embedder))
        await _names(mcp, ['shut a ticket', 'leave a note'])
        await _names(mcp, ['shut a ticket', 'leave a note'])
        assert embedder.query_requests == 1

    async def test_hidden_operations_do_not_crowd_out_visible_ones(self, embedder):
        """Operations the token cannot see are dropped before the top of the ranking is kept, not after."""
        hidden = [_operation(f'note_{number}', 'Add a note') for number in range(60)]
        registry = _OperationRegistry(
            [*hidden, _operation('adopt_pet', 'Adopt a pet')],
            {'name': 1.0},
            SemanticSearchOptions(model='stand-in', embedder=embedder),
        )
        page = await registry.search('leave a note', None, None, lambda name: name == 'adopt_pet')
        assert [entry['name'] for entry in page['operations']] == ['adopt_pet']

    def test_servers_sharing_a_cache_directory_keep_their_own_files(self, monkeypatch, tmp_path: pathlib.Path):
        """Servers sharing a cache directory keep their vectors apart, under each server's name."""
        monkeypatch.setattr('openapi_mcp_gateway.gateway.build_embedder', lambda model: _ConceptEmbedder())
        config = SemanticSearchConfig(model='stand-in', cache_directory=tmp_path)
        options = [_semantic_search_options(name, config) for name in ('one', 'two')]
        directories = {option.cache_directory for option in options if option}
        assert directories == {tmp_path / 'one', tmp_path / 'two'}

    async def test_unreadable_cache_file_is_replaced(self, embedder, tmp_path: pathlib.Path):
        """A cache file that is valid JSON but not the expected shape is ignored, then written anew."""
        options = SemanticSearchOptions(model='stand-in', embedder=embedder, cache_directory=tmp_path)
        await _names(_register(options), 'shut a ticket')
        (cache_file,) = tmp_path.glob('embeddings-*.json')
        cache_file.write_text('[]')
        assert (await _names(_register(options), 'shut a ticket'))[0] == 'update_issue'
        assert cache_file.read_text().startswith('{')

    async def test_description_follows_how_the_server_matches(self, embedder):
        """The tool tells the model to use the API's words when matching by words, and plain words by meaning."""
        by_meaning = _register(SemanticSearchOptions(model='stand-in', embedder=embedder))
        by_words = MCPServer('test')
        MetaToolGenerator(mcp=by_words, connection=UpstreamConnection(base_url=API_URL)).register([])
        assert 'Matching is by meaning' in await _description(by_meaning)
        assert 'Matching is by shared words' in await _description(by_words)

    async def test_too_many_queries_are_refused(self, embedder):
        """A list longer than the limit is refused before any ranking or embedding."""
        mcp = _register(SemanticSearchOptions(model='stand-in', embedder=embedder))
        async with Client(mcp) as client:
            result = await client.call_tool('search_operations', {'query': [f'query {n}' for n in range(11)]})
        assert result.is_error
        assert embedder.query_requests == 0

    def test_unusable_model_is_refused_at_load(self, petstore_json_path):
        """A model name the provider cannot resolve fails the config at load, naming the server."""
        config = _semantic_config(petstore_json_path, 'text-embedding-3-small')
        with pytest.raises(ValueError, match=r'Server "petstore".*provider prefix'):
            Gateway.from_config(config)

    def test_refused_without_the_embeddings_extra(self, monkeypatch, petstore_json_path):
        """A config asking for semantic search fails to load when the extra is missing, naming it."""
        monkeypatch.setattr('openapi_mcp_gateway.gateway.embeddings_available', lambda: False)
        config = _semantic_config(petstore_json_path, 'openai:x')
        with pytest.raises(ValueError, match=r'openapi-mcp-gateway\[embeddings\]'):
            Gateway.from_config(config)


class _ConceptEmbedder:
    """A stand-in embedding model that places text by the concepts its words name, and counts its calls."""

    def __init__(self, *, failing: bool = False):
        self.failing = failing
        self.embedded_documents: list[str] = []
        self.query_requests = 0

    async def embed_documents(self, documents: typing.Sequence[str], *, settings: typing.Any = None) -> typing.Any:
        self.embedded_documents.extend(documents)
        return types.SimpleNamespace(embeddings=[_place(text) for text in documents])

    async def embed_query(self, queries: typing.Sequence[str], *, settings: typing.Any = None) -> typing.Any:
        self.query_requests += 1
        if self.failing:
            raise ConnectionError('embedding service unreachable')
        return types.SimpleNamespace(embeddings=[_place(query) for query in queries])


def _place(text: str) -> list[float]:
    words = text.lower().split()
    return [float(sum(word.startswith(synonyms) for word in words)) + 0.01 for synonyms in _CONCEPTS]


def _semantic_config(spec_path: pathlib.Path, model: str) -> GatewayConfig:
    return GatewayConfig.model_validate(
        {
            'servers': [
                {
                    'name': 'petstore',
                    'spec': str(spec_path),
                    'exposure': {'style': 'dynamic', 'search': {'semantic': {'model': model}}},
                }
            ]
        }
    )


def _operation(name: str, summary: str) -> _RegisteredOperation:
    async def never_called(**arguments: typing.Any) -> typing.Any:
        raise AssertionError

    return _RegisteredOperation(
        name=name, summary=summary, description=summary, path='/', tags=[], input_schema={}, callable_=never_called
    )


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


async def _description(mcp: MCPServer) -> str:
    async with Client(mcp) as client:
        tools = await client.list_tools()
    return next(tool.description or '' for tool in tools.tools if tool.name == 'search_operations')


async def _names(mcp: MCPServer, query: str | list[str], **arguments: typing.Any) -> list[str]:
    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool('search_operations', {'query': query, **arguments})
    return [entry['name'] for entry in result.structured_content['operations']]
