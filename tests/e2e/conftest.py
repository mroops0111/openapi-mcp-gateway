import itertools
import socket
import typing

import pytest

from tests.e2e.keycloak import create_realm, run_keycloak
from tests.serving import bind_loopback_socket, build_socket_url


# A fresh realm name for each test, so no test sees another's agents or tokens.
_REALM_NUMBERS = itertools.count()


class GatewayAddress(typing.NamedTuple):
    """Where a test serves its gateway, settled before the realm that names it is created."""

    sock: socket.socket
    url: str


@pytest.fixture(scope='session')
def keycloak() -> typing.Iterator[str]:
    """Keycloak running in a container for the whole session, by its base URL."""
    with run_keycloak() as base_url:
        yield base_url


@pytest.fixture
def gateway_address() -> GatewayAddress:
    sock = bind_loopback_socket()
    return GatewayAddress(sock, build_socket_url(sock))


@pytest.fixture
def issuer(keycloak, gateway_address) -> typing.Iterator[str]:
    """The issuer of a realm made for this test's gateway, removed afterwards."""
    with create_realm(keycloak, f'mcp-{next(_REALM_NUMBERS)}', f'{gateway_address.url}/petstore/mcp') as issuer_url:
        yield issuer_url
