import typing

import pytest

from tests.e2e.keycloak import create_realm, run_keycloak
from tests.serving import ServingAddress, bind_serving_address


@pytest.fixture(scope='session')
def keycloak() -> typing.Iterator[str]:
    """Keycloak running in a container for the whole session, by its base URL."""
    with run_keycloak() as base_url:
        yield base_url


@pytest.fixture
def gateway_address() -> ServingAddress:
    """Where the test serves its gateway, settled before the realm that names it is created."""
    return bind_serving_address()


@pytest.fixture
def issuer(keycloak, gateway_address) -> typing.Iterator[str]:
    """The issuer of a realm made for this test's gateway, removed afterwards."""
    with create_realm(keycloak, f'{gateway_address.url}/petstore/mcp') as issuer_url:
        yield issuer_url
