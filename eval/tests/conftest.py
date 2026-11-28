import pytest


@pytest.fixture(autouse=True)
def spec_cache(tmp_path_factory, monkeypatch):
    """Keep every spec a test fetches or derives out of the user's own cache."""
    directory = tmp_path_factory.mktemp('spec-cache')
    monkeypatch.setenv('OPENAPI_MCP_GATEWAY_CACHE', str(directory))
    return directory
