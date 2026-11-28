import pathlib
import typing

import pytest

from openapi_mcp_gateway.gateway import Gateway
from tests.constants import GATEWAY_URL
from tests.integration.scopes.clients import CLIENTS
from tests.oauth import authorization_code_gateway


@pytest.fixture
def make_gateway(shop_json_path: pathlib.Path) -> typing.Callable[..., Gateway]:
    """Build a shop gateway whose ``authorization_code`` server defines ``scopes`` for ``clients``.

    ``upstream`` is merged into ``auth.upstream``, and ``spec`` replaces the shop fixture.
    """

    def make(
        operation_scopes: dict[str, typing.Any] | None = None,
        clients: dict[str, typing.Any] | None = None,
        style: str = 'static',
        promote_resources: bool = False,
        upstream: dict[str, typing.Any] | None = None,
        spec: pathlib.Path = shop_json_path,
        url: str = GATEWAY_URL,
        other_clients: dict[str, typing.Any] | None = None,
    ) -> Gateway:
        return authorization_code_gateway(
            spec,
            clients or CLIENTS,
            operation_scopes=operation_scopes if operation_scopes is not None else {'auto': 'tags'},
            upstream=upstream,
            exposure={'style': style, 'promote_resources': promote_resources},
            url=url,
            other_clients=other_clients,
        )

    return make


@pytest.fixture
def gateway(make_gateway) -> Gateway:
    """The shop gateway with tag-derived scopes and static exposure."""
    return make_gateway()


@pytest.fixture
def make_enforcing_gateway(make_gateway, secured_spec) -> typing.Callable[..., Gateway]:
    """A gateway enforcing the upstream grant, asking for at most both product scopes unless ``upstream`` says otherwise."""

    def make(style: str = 'static', url: str = GATEWAY_URL, **upstream: typing.Any) -> Gateway:
        settings = {'enforce_scopes': True, 'scopes': ['read:products', 'write:products'], **upstream}
        return make_gateway(spec=secured_spec, style=style, upstream=settings, url=url)

    return make
