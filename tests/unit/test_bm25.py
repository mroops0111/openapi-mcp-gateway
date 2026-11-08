from openapi_mcp_gateway.exposure._bm25 import BM25FIndex, tokenize


# One plain-text field with unit weight, for tests that are not about field weighting.
_TEXT_ONLY = {'text': 1.0}


def _index(documents: dict[str, str]) -> BM25FIndex:
    """Index each document as a single ``text`` field."""
    return BM25FIndex({name: {'text': text} for name, text in documents.items()}, _TEXT_ONLY)


class TestTokenize:
    """Splitting operation text and queries into comparable terms."""

    def test_camel_snake_and_path_agree(self):
        """``pullsMerge``, ``pulls_merge``, and ``/pulls/merge`` produce the same terms."""
        assert tokenize('pullsMerge') == tokenize('pulls_merge') == tokenize('/pulls/merge') == ['pull', 'merg']

    def test_stems_inflections(self):
        """Plurals, including ``-es`` ones, and verb forms reduce to one stem."""
        assert tokenize('branch branches') == ['branch', 'branch']
        assert tokenize('status statuses') == ['status', 'status']
        assert tokenize('merge merged merging') == ['merg', 'merg', 'merg']

    def test_drops_stopwords(self):
        """Stopwords disappear before stemming."""
        assert tokenize('Merge a pull request') == ['merg', 'pull', 'request']

    def test_acronyms(self):
        """A run of capitals stays one term, and a trailing lone ``s`` stays with it as its plural."""
        assert tokenize('getSSHKey') == ['get', 'ssh', 'key']
        assert tokenize('getUserIDs') == tokenize('get user ids') == ['get', 'user', 'id']
        assert tokenize('listIDsByOwner') == ['list', 'id', 'owner']

    def test_letters_in_any_script(self):
        """Accented and non-Latin letters stay part of their word."""
        assert tokenize('café menü') == ['café', 'menü']
        assert tokenize('取得 使用者') == ['取得', '使用者']

    def test_digits_split_from_letters(self):
        """A digit run is its own term."""
        assert tokenize('v2Api') == ['v', '2', 'api']


class TestBM25FIndex:
    """BM25F ranking over operation fields."""

    def test_rarer_term_ranks_higher(self):
        """The document matching the rarer query term comes first."""
        index = _index({'list_issues': 'list issues', 'list_pulls': 'list pulls', 'merge_pull': 'merge pull'})
        assert index.search('merge pulls') == ['merge_pull', 'list_pulls']

    def test_heavier_field_ranks_higher(self):
        """A term in a heavier field outranks the same term in a lighter one."""
        index = BM25FIndex(
            {
                'in_description': {'name': 'other', 'description': 'merge'},
                'in_name': {'name': 'merge', 'description': 'other'},
            },
            {'name': 2.0, 'description': 1.0},
        )
        assert index.search('merge') == ['in_name', 'in_description']

    def test_unweighted_field_is_not_searched(self):
        """A field without a weight contributes nothing."""
        index = BM25FIndex({'only_path': {'path': '/merge'}}, {'name': 1.0})
        assert index.search('merge') == []

    def test_no_shared_term_returns_nothing(self):
        """A query sharing no term with any document finds nothing."""
        index = _index({'list_issues': 'list issues'})
        assert index.search('deploy') == []
        assert index.search('the') == []

    def test_candidates_limit_the_result(self):
        """Only candidate names are ranked."""
        index = _index({'list_issues': 'list issues', 'list_pulls': 'list pulls'})
        assert index.search('list', candidates={'list_pulls'}) == ['list_pulls']

    def test_ties_keep_document_order(self):
        """Equal scores keep the order the documents were given in."""
        index = _index({'b_item': 'item', 'a_item': 'item'})
        assert index.search('item') == ['b_item', 'a_item']

    def test_empty_index(self):
        """An index without documents answers every query with nothing."""
        assert _index({}).search('anything') == []
