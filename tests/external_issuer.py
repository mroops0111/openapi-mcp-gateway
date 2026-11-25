import contextlib
import pathlib
import typing

import pytest
from mcp import Client
from mcp.client.auth.exceptions import OAuthTokenError

from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.settings import GatewayConfig
from tests.mcp_client import client_credentials_auth, connect


# What every caller's token must carry, which the protected resource metadata tells a client to ask for first.
REQUIRED_SCOPES = ['pets:read']

# Every tool an operation scope covers on the petstore fixture, which the operations without a tag are not.
SCOPED_TOOLS = ['create_pet', 'delete_pet', 'get_pet_by_id', 'list_pets']

# The one credential every call carries upstream on a shared-credential server.
UPSTREAM_TOKEN = 'upstream-pat'
SHARED_CREDENTIAL_UPSTREAM = {'type': 'bearer', 'token': UPSTREAM_TOKEN}


def external_issuer_gateway(
    spec: pathlib.Path, url: str, issuer: str, upstream: typing.Mapping[str, typing.Any]
) -> Gateway:
    """A petstore gateway at ``url`` accepting the tokens ``issuer`` mints, scoped per operation by its tags."""
    mcp = {'issuer': issuer, 'required_scopes': REQUIRED_SCOPES, 'operation_scopes': {'auto': 'tags'}}
    server = {'name': 'petstore', 'spec': str(spec), 'auth': {'upstream': dict(upstream), 'mcp': mcp}}
    return Gateway.from_config(GatewayConfig.model_validate({'url': url, 'servers': [server]}))


def token_exchange_upstream(
    audience: str, client_id: str, client_secret: str, scopes: typing.Sequence[str]
) -> dict[str, typing.Any]:
    """An upstream reached by exchanging each caller's token for ``audience``, enforcing the grant it returns."""
    return {
        'type': 'oauth2',
        'flow': 'token_exchange',
        'audience': audience,
        'client_id': client_id,
        'client_secret': client_secret,
        'scopes': list(scopes),
        'enforce_scopes': True,
    }


@contextlib.asynccontextmanager
async def connect_as(
    base_url: str, issuer: str, agent: str, secret: str, scope: str | None = None
) -> typing.AsyncIterator[Client]:
    """The MCP SDK's own client of the petstore, holding a token ``issuer`` minted to ``agent``, for ``scope``."""
    endpoint = f'{base_url}/petstore/mcp'
    async with connect(
        endpoint, client_credentials_auth(endpoint, agent, secret, scope=scope, issuer=issuer)
    ) as client:
        yield client


def issuer_declines_the_step_up() -> pytest.RaisesGroup[OAuthTokenError]:
    """What the SDK client raises once the issuer refuses the scope a challenge named, out of its task group."""
    return pytest.RaisesGroup(pytest.RaisesExc(OAuthTokenError, match='invalid_scope'), flatten_subgroups=True)
