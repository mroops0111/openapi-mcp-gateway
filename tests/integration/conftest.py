import pytest
from starlette.testclient import TestClient

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import (
    AuthConfig,
    GatewayConfig,
    ServerConfig,
    UpstreamAuthConfig,
)
from tests.constants import AUTHORIZE_URL, GATEWAY_URL, TOKEN_URL


@pytest.fixture
def gateway(shop_json_path):
    """Single-server shop gateway with no upstream auth configured."""
    config = GatewayConfig(
        servers=[
            ServerConfig(name='shop', spec=str(shop_json_path)),
        ],
    )
    return Gateway.from_config(config)


@pytest.fixture
def app(gateway):
    """Starlette app built from the no-auth gateway over streamable-http."""
    return gateway.build_app()


@pytest.fixture
def client(app):
    """Test client over the no-auth gateway app."""
    return TestClient(app)


@pytest.fixture
def oauth_gateway(shop_json_path):
    """Gateway whose shop server uses the OAuth2 authorization-code flow."""
    config = GatewayConfig(
        url=GATEWAY_URL,
        servers=[
            ServerConfig(
                name='shop',
                spec=str(shop_json_path),
                auth=AuthConfig(
                    upstream=UpstreamAuthConfig(
                        type='oauth2',
                        client_id='test-client-id',
                        client_secret='test-client-secret',
                        authorization_url=AUTHORIZE_URL,
                        token_url=TOKEN_URL,
                        scopes=['read'],
                    )
                ),
            ),
        ],
    )
    return Gateway.from_config(config)
