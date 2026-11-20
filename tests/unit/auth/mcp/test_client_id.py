import pytest
from pydantic import AnyUrl

from openapi_mcp_gateway.auth.mcp.client_id import client_metadata_host, is_client_metadata_url, is_loopback_redirect
from tests.client_metadata import CLIENT_METADATA_HOST, CLIENT_METADATA_URL


class TestClientMetadataUrl:
    """Only a URL the CIMD draft §3 allows is read as a metadata document's address."""

    @pytest.mark.parametrize(
        'client_id',
        [CLIENT_METADATA_URL, 'https://app.example.com:8443/client.json', 'https://app.example.com/c?v=1'],
    )
    def test_an_https_url_with_a_path_is_one(self, client_id):
        assert is_client_metadata_url(client_id)

    @pytest.mark.parametrize(
        'client_id',
        [
            'agent',
            'http://app.example.com/client.json',
            'https://app.example.com',
            'https://app.example.com/',
            'https://app.example.com/client.json#frag',
            'https://user@app.example.com/client.json',
            'https://app.example.com/a/../client.json',
            'https://app.example.com/./client.json',
            'https://app.example.com:bad/client.json',
            'https://bücher.example/client.json',
            'https://app.example.com/a\x01b',
            'https://app.example.com/a b',
        ],
    )
    def test_anything_else_is_not(self, client_id):
        assert not is_client_metadata_url(client_id)


class TestClientMetadataHost:
    def test_a_metadata_document_url_gives_its_host(self):
        assert client_metadata_host(CLIENT_METADATA_URL) == CLIENT_METADATA_HOST

    @pytest.mark.parametrize('client_id', ['agent', 'http://app.example.com/client.json'])
    def test_any_other_client_id_gives_none(self, client_id):
        assert client_metadata_host(client_id) is None


class TestLoopbackRedirect:
    @pytest.mark.parametrize(
        ('uri', 'loopback'),
        [
            ('http://localhost/callback', True),
            ('http://127.0.0.1:3000/', True),
            ('http://[::1]:9000/cb', True),
            ('https://localhost/callback', False),
            ('http://app.example.com/callback', False),
        ],
    )
    def test_only_http_to_this_machine_is_loopback(self, uri, loopback):
        assert is_loopback_redirect(AnyUrl(uri)) is loopback
