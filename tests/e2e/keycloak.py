import contextlib
import itertools
import typing

import httpx
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy


# The Keycloak release the end-to-end tests run, the first with standard RFC 8693 token exchange.
KEYCLOAK_IMAGE = 'quay.io/keycloak/keycloak:26.2'

# The upstream API's client, which every exchanged token names as its audience.
UPSTREAM_AUDIENCE = 'pets-api'

# The secret every confidential client in a realm shares.
CLIENT_SECRET = 'secret'

# The agents a realm declares, by the MCP scopes each may be granted.
AGENT_SCOPES = {'reader': ['pets:read'], 'writer': ['pets:read', 'pets:write', 'pets:delete']}

# The upstream scopes the gateway may exchange for, each adding the upstream API to the token's audience.
UPSTREAM_SCOPES = ['read:pets', 'write:pets']


# The bootstrap administrator, who creates each test's realm.
_ADMIN_USERNAME = 'admin'
_ADMIN_PASSWORD = 'admin'

# A fresh realm name for each realm, so no test sees another's agents or tokens.
_REALM_NUMBERS = itertools.count()


@contextlib.contextmanager
def run_keycloak() -> typing.Iterator[str]:
    """Run Keycloak in a container for the length of the block, and yield its base URL."""
    container = (
        DockerContainer(KEYCLOAK_IMAGE)
        .with_command('start-dev')
        .with_env('KC_BOOTSTRAP_ADMIN_USERNAME', _ADMIN_USERNAME)
        .with_env('KC_BOOTSTRAP_ADMIN_PASSWORD', _ADMIN_PASSWORD)
        .with_exposed_ports(8080)
        .waiting_for(HttpWaitStrategy(8080, '/realms/master').for_status_code(200))
    )
    with container:
        yield f'http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}'


@contextlib.contextmanager
def create_realm(
    base_url: str, mcp_endpoint: str, exchangeable_scopes: typing.Sequence[str] = tuple(UPSTREAM_SCOPES)
) -> typing.Iterator[str]:
    """Create a realm of its own for a gateway serving ``mcp_endpoint``, and yield its issuer.

    The gateway exchanges tokens as a client named by its endpoint's canonical URI,
    which an audience mapper also puts in every agent's token, as docs/guides/identity-providers.md describes.
    That client may exchange for ``exchangeable_scopes`` only.
    """
    name = f'mcp-{next(_REALM_NUMBERS)}'
    realm = _build_realm(name, mcp_endpoint, exchangeable_scopes)
    with httpx.Client(base_url=base_url, headers={'Authorization': f'Bearer {_admin_token(base_url)}'}) as admin:
        admin.post('/admin/realms', json=realm).raise_for_status()
        try:
            yield f'{base_url}/realms/{name}'
        finally:
            admin.delete(f'/admin/realms/{name}')


def _admin_token(base_url: str) -> str:
    response = httpx.post(
        f'{base_url}/realms/master/protocol/openid-connect/token',
        data={
            'grant_type': 'password',
            'client_id': 'admin-cli',
            'username': _ADMIN_USERNAME,
            'password': _ADMIN_PASSWORD,
        },
    )
    response.raise_for_status()
    return response.json()['access_token']


def _build_realm(name: str, mcp_endpoint: str, exchangeable_scopes: typing.Sequence[str]) -> dict[str, typing.Any]:
    mcp_scopes = sorted({scope for scopes in AGENT_SCOPES.values() for scope in scopes})
    return {
        'realm': name,
        'enabled': True,
        'clientScopes': [
            *(_client_scope(scope) for scope in mcp_scopes),
            *(_client_scope(scope, audience=UPSTREAM_AUDIENCE) for scope in UPSTREAM_SCOPES),
            _client_scope('mcp-audience', audience=mcp_endpoint),
        ],
        'clients': [
            {
                **_confidential_client(mcp_endpoint),
                'attributes': {'standard.token.exchange.enabled': 'true'},
                'optionalClientScopes': list(exchangeable_scopes),
            },
            _confidential_client(UPSTREAM_AUDIENCE),
            *(
                {
                    **_confidential_client(agent),
                    'serviceAccountsEnabled': True,
                    'defaultClientScopes': ['mcp-audience'],
                    'optionalClientScopes': scopes,
                }
                for agent, scopes in AGENT_SCOPES.items()
            ),
        ],
    }


def _client_scope(name: str, audience: str | None = None) -> dict[str, typing.Any]:
    """A client scope named in the token's ``scope``, adding ``audience`` to its ``aud`` when given."""
    mappers = (
        [
            {
                'name': f'{name} audience',
                'protocol': 'openid-connect',
                'protocolMapper': 'oidc-audience-mapper',
                'config': {'included.client.audience': audience, 'access.token.claim': 'true'},
            }
        ]
        if audience
        else []
    )
    return {
        'name': name,
        'protocol': 'openid-connect',
        'attributes': {'include.in.token.scope': 'true', 'display.on.consent.screen': 'false'},
        'protocolMappers': mappers,
    }


def _confidential_client(client_id: str) -> dict[str, typing.Any]:
    """A confidential client signing no user in, with no default scopes of its own."""
    return {
        'clientId': client_id,
        'secret': CLIENT_SECRET,
        'publicClient': False,
        'standardFlowEnabled': False,
        'serviceAccountsEnabled': False,
        'defaultClientScopes': [],
    }
