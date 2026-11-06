from openapi_mcp_gateway.exposure._search import OperationSearchIndex, tokenize


class TestTokenize:
    """Splitting operation text and queries into comparable terms."""

    def test_camel_snake_and_path_agree(self):
        """``pullsMerge``, ``pulls_merge``, and ``/pulls/merge`` produce the same terms."""
        assert tokenize('pullsMerge') == tokenize('pulls_merge') == tokenize('/pulls/merge') == ['pull', 'merge']

    def test_drops_stopwords_and_plurals(self):
        """Stopwords disappear and a plural ``s`` is stripped, while ``status`` keeps its ending."""
        assert tokenize('Merge a pull request') == ['merge', 'pull', 'request']
        assert tokenize('Get the status of all checks') == ['get', 'status', 'check']

    def test_keeps_acronyms_whole(self):
        """A run of capitals stays one term."""
        assert tokenize('getSSHKey') == ['get', 'ssh', 'key']


class TestOperationSearchIndex:
    """BM25 ranking over operation text."""

    def test_rarer_term_ranks_higher(self):
        """The operation matching the rarer query term comes first."""
        index = OperationSearchIndex(
            {
                'list_issues': 'list issues',
                'list_pulls': 'list pulls',
                'merge_pull': 'merge pull',
            }
        )
        assert index.search('merge pulls') == ['merge_pull', 'list_pulls']

    def test_no_shared_term_returns_nothing(self):
        """A query sharing no term with any operation finds nothing."""
        index = OperationSearchIndex({'list_issues': 'list issues'})
        assert index.search('deploy') == []
        assert index.search('the') == []

    def test_candidates_limit_the_result(self):
        """Only candidate names are ranked."""
        index = OperationSearchIndex({'list_issues': 'list issues', 'list_pulls': 'list pulls'})
        assert index.search('list', candidates={'list_pulls'}) == ['list_pulls']

    def test_ties_keep_document_order(self):
        """Equal scores keep the order the documents were given in."""
        index = OperationSearchIndex({'b_item': 'item', 'a_item': 'item'})
        assert index.search('item') == ['b_item', 'a_item']

    def test_empty_index(self):
        """An index without documents answers every query with nothing."""
        assert OperationSearchIndex({}).search('anything') == []
