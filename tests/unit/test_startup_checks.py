import pathlib
import typing

import pytest
from fastapi import FastAPI

from openapi_mcp_gateway.fastapi import mcp_tool
from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.openapi import ResourceOverride
from openapi_mcp_gateway.settings import (
    ExposureConfig,
    GatewayConfig,
    OperationOverride,
    PolicyConfig,
    ServerConfig,
)
from tests.constants import (
    GATEWAY_URL,
    SHARED_CREDENTIAL_SECURITY,
    SHARED_CREDENTIAL_UPSTREAM,
    SHOP_URL,
    TOKEN_URL,
)
from tests.oauth import authorization_code_gateway
from tests.spec import TRELLO_SCHEMES, write_board_spec, write_spec


# The pre-registered clients a scoped shop server declares, by what each may do to products.
READER = 'reader'
CLIENTS = {
    READER: {'scopes': ['products:read']},
    'deleter': {'scopes': ['products:read', 'products:delete']},
    'writer': {'scopes': ['products:write']},
    'stepper': {'scopes': ['products:read', 'products:write', 'products:delete'], 'default_scopes': ['products:write']},
}


@pytest.fixture
def make_gateway(shop_json_path: pathlib.Path) -> typing.Callable[..., Gateway]:
    """Build a shop gateway whose authorization_code server defines tag-derived scopes for clients."""

    def make(
        clients: dict[str, typing.Any] | None = None, other_clients: dict[str, typing.Any] | None = None
    ) -> Gateway:
        return authorization_code_gateway(
            shop_json_path,
            clients or CLIENTS,
            operation_scopes={'auto': 'tags'},
            exposure={'style': 'static', 'promote_resources': False},
            other_clients=other_clients,
        )

    return make


class TestPolicy:
    """A policy that leaves a server nothing to serve refuses to start."""

    def test_empty_operations_raises(self, shop_json_path):
        """A policy that filters every operation out fails fast at assembly time."""
        config = GatewayConfig(
            servers=[
                ServerConfig(
                    name='test',
                    spec=str(shop_json_path),
                    policy=PolicyConfig(allow=['NONEXISTENT_OPERATION']),
                ),
            ],
        )
        with pytest.raises(ValueError):
            Gateway.from_config(config)


class TestResourceExposure:
    """A resource declaration the gateway cannot honour refuses to start."""

    def test_post_with_expose_resource_raises(self, tmp_path):
        """A POST opted into ``expose.resource`` aborts ``Gateway.from_config`` with a clear error."""
        spec_path = write_spec(tmp_path, _spec_with_misconfig_post_resource())
        with pytest.raises(ValueError, match='method is POST'):
            Gateway.from_config(
                GatewayConfig(
                    servers=[
                        ServerConfig(name='shop', spec=str(spec_path), exposure=ExposureConfig(promote_resources=True))
                    ]
                )
            )

    def test_unknown_operation_id_in_yaml_aborts(self, tmp_path):
        """A YAML override targeting an op that the spec does not expose fails ``Gateway.from_config``."""
        spec_path = write_spec(tmp_path, _vanilla_spec())
        with pytest.raises(ValueError, match='not_a_real_op'):
            Gateway.from_config(
                GatewayConfig(
                    servers=[
                        ServerConfig(
                            name='shop',
                            spec=str(spec_path),
                            exposure=ExposureConfig(promote_resources=True),
                            operations={
                                'not_a_real_op': OperationOverride(
                                    resource=ResourceOverride(name='ghost'),
                                ),
                            },
                        )
                    ]
                )
            )


class TestFastapiIntegration:
    """A FastAPI app the gateway cannot serve refuses to start."""

    def test_no_marked_routes_raises(self):
        """A FastAPI app without any ``@mcp_tool`` route is a configuration error."""
        app = FastAPI()

        @app.get('/ping')
        def ping():
            return {'pong': True}

        with pytest.raises(ValueError, match='no routes are decorated with @mcp_tool'):
            Gateway.from_fastapi(app, name='svc')

    def test_unsupported_oauth_only_rejected(self):
        """A spec declaring only password/implicit flows is rejected with a clear error."""
        app = FastAPI()

        @app.get('/ping')
        @mcp_tool()
        def ping():
            return {'pong': True}

        original_openapi = app.openapi

        def _openapi_with_password():
            schema = original_openapi()
            schema.setdefault('components', {})['securitySchemes'] = {
                'oauth2': {
                    'type': 'oauth2',
                    'flows': {
                        'password': {
                            'tokenUrl': TOKEN_URL,
                            'scopes': {},
                        },
                    },
                },
            }
            return schema

        app.openapi = _openapi_with_password  # type: ignore[method-assign]

        with pytest.raises(ValueError, match='unsupported OAuth2 flows'):
            Gateway.from_fastapi(app, name='svc')


class TestSharedCredentials:
    """Security a config's credentials cannot meet, or name wrongly, refuses to start."""

    def test_root_security_naming_an_undeclared_scheme_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match=r"security names schemes the spec does not declare \['missing'\]"):
            _board_gateway(write_board_spec(tmp_path), security=[{'missing': []}])

    def test_a_type_that_takes_no_credentials_is_named_before_the_schemes(self, tmp_path):
        """The scheme is undeclared too, but the type is what to fix first."""
        upstream = {'type': 'passthrough', 'credentials': {'missing': {'value': 'k'}}}

        with pytest.raises(
            ValueError, match=r'credentials needs auth.upstream.type none, or auth.upstream.type oauth2'
        ):
            _board_gateway(write_board_spec(tmp_path), upstream=upstream)

    def test_an_operation_its_credentials_cannot_meet_is_refused(self, tmp_path):
        spec = write_board_spec(tmp_path, schemes=TRELLO_SCHEMES, security=[{'APIKey': [], 'APIToken': []}])

        with pytest.raises(ValueError, match=r'meets the security of getBoard \(needs APIKey \+ APIToken\)'):
            _board_gateway(spec, credentials={'APIKey': {'value': 'k'}})


class TestMcpScopes:
    """A client granted a scope no operation_scopes defines refuses to start."""

    def test_a_client_granted_an_undefined_scope_is_refused_at_startup(self, make_gateway):
        with pytest.raises(ValueError, match=r'reader: products:raed'):
            make_gateway(clients={READER: {'scopes': ['products:raed']}})

    def test_other_clients_granted_an_undefined_scope_are_refused_at_startup(self, make_gateway):
        with pytest.raises(ValueError, match=r'other_clients: products:raed'):
            make_gateway(other_clients={'scopes': ['products:raed']})


class TestUpstreamScopes:
    """Sign-in settings on a flow with no user sign-in refuse to start."""

    @pytest.mark.parametrize(('key', 'value'), [('enforce_scopes', True), ('required_scopes', ['offline_access'])])
    def test_a_flow_without_a_user_grant_refuses_the_sign_in_settings(self, client_credentials_spec_path, key, value):
        auth = {'upstream': {'type': 'oauth2', 'client_id': 'gateway', 'client_secret': 'secret', key: value}}
        server = {'name': 'svc', 'spec': str(client_credentials_spec_path), 'auth': auth}
        with pytest.raises(ValueError, match=rf'auth\.upstream\.{key} needs auth\.upstream\.flow authorization_code'):
            Gateway.from_config(GatewayConfig.model_validate({'url': GATEWAY_URL, 'servers': [server]}))


class TestStdio:
    """A stdio server presents no MCP token, so one that checks them refuses to start."""

    def test_stdio_refuses_a_server_that_checks_mcp_tokens(self, shop_json_path):
        """A stdio client presents no token, so such a server would refuse or hide everything without a word."""
        server = {
            'name': 'shop',
            'spec': str(shop_json_path),
            **SHARED_CREDENTIAL_SECURITY,
            'auth': {
                'upstream': SHARED_CREDENTIAL_UPSTREAM,
                'mcp': {'clients': {'agent': {'client_secret': 's'}}},
            },
        }
        gateway = Gateway.from_config(GatewayConfig.model_validate({'servers': [server]}))

        with pytest.raises(ValueError, match='never presents'):
            gateway.run(transport='stdio')


class TestSemanticSearch:
    """A semantic search the gateway cannot run refuses the config at load, naming what is missing."""

    def test_unusable_model_is_refused_at_load(self, shop_json_path):
        """A model name the provider cannot resolve fails the config at load, naming the server."""
        config = _semantic_config(shop_json_path, 'text-embedding-3-small')
        with pytest.raises(ValueError, match=r'Server "shop".*provider prefix'):
            Gateway.from_config(config)

    def test_refused_without_the_embeddings_extra(self, monkeypatch, shop_json_path):
        """A config asking for semantic search fails to load when the extra is missing, naming it."""
        monkeypatch.setattr('openapi_mcp_gateway.gateway.embeddings_available', lambda: False)
        config = _semantic_config(shop_json_path, 'openai:x')
        with pytest.raises(ValueError, match=r'openapi-mcp-gateway\[embeddings\]'):
            Gateway.from_config(config)


def _spec_with_misconfig_post_resource() -> dict:
    """Spec where ``createProduct`` (POST) wrongly declares ``expose.resource``."""
    return {
        'openapi': '3.0.0',
        'info': {'title': 'Bad Resource', 'version': '1.0.0'},
        'servers': [{'url': SHOP_URL}],
        'paths': {
            '/products': {
                'post': {
                    'operationId': 'createProduct',
                    'x-mcp-integration': {'resource': {}},
                    'responses': {'201': {'description': 'created'}},
                },
            },
        },
    }


def _vanilla_spec() -> dict:
    """Vanilla spec without any ``x-mcp-integration`` declarations, for YAML-override tests."""
    return {
        'openapi': '3.0.0',
        'info': {'title': 'Shop', 'version': '1.0.0'},
        'servers': [{'url': SHOP_URL}],
        'paths': {
            '/products/{productId}': {
                'get': {
                    'operationId': 'getProduct',
                    'parameters': [
                        {'name': 'productId', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                    ],
                    'responses': {'200': {'description': 'ok'}},
                },
            },
        },
    }


def _board_gateway(
    spec: pathlib.Path,
    *,
    credentials: dict[str, typing.Any] | None = None,
    upstream: dict[str, typing.Any] | None = None,
    **server: typing.Any,
) -> Gateway:
    upstream = upstream or ({'credentials': credentials} if credentials else {})
    config = {'servers': [{'name': 'boards', 'spec': str(spec), 'auth': {'upstream': upstream}, **server}]}
    return Gateway.from_config(GatewayConfig.model_validate(config))


def _semantic_config(spec_path: pathlib.Path, model: str) -> GatewayConfig:
    return GatewayConfig.model_validate(
        {
            'servers': [
                {
                    'name': 'shop',
                    'spec': str(spec_path),
                    'exposure': {'style': 'dynamic', 'search': {'semantic': {'model': model}}},
                }
            ]
        }
    )
