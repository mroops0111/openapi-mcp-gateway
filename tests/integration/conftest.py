import pathlib

import pytest
from starlette.testclient import TestClient

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import (
    GatewayConfig,
    ServerConfig,
)
from tests.spec import write_secured_petstore


@pytest.fixture
def gateway(petstore_json_path):
    """Single-server petstore gateway with no upstream auth configured."""
    config = GatewayConfig(
        servers=[
            ServerConfig(name='petstore', spec=str(petstore_json_path)),
        ],
    )
    return Gateway.from_config(config)


@pytest.fixture
def app(gateway):
    """Starlette app built from the no-auth gateway over streamable-http."""
    return gateway._build_app(transport='streamable-http')


@pytest.fixture
def client(app):
    """Test client over the no-auth gateway app."""
    return TestClient(app)


@pytest.fixture
def secured_spec(petstore_json_path: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
    """The petstore fixture with each operation's upstream scopes declared."""
    return write_secured_petstore(petstore_json_path, tmp_path)
