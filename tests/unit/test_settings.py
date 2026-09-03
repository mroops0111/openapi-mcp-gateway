import pathlib

import pydantic
import pytest
import yaml

from openapi_mcp_gateway.settings import (
    AuthConfig,
    GatewayConfig,
    ServerConfig,
    UpstreamAuthConfig,
    build_gateway_config,
    single_spec_layer,
    yaml_layer,
)


EXAMPLES_DIR = pathlib.Path(__file__).resolve().parents[2] / 'examples'


class TestServerConfig:
    """Mount path derivation and name validation on ``ServerConfig``."""

    def test_mount_path_default(self):
        """Mount path defaults to ``/<name>`` when no prefix is set."""
        server = ServerConfig(name='petstore', spec='petstore.json')
        assert server.mount_path == '/petstore'

    def test_mount_path_custom(self):
        """``path_prefix`` overrides the name-based default."""
        server = ServerConfig(name='petstore', spec='petstore.json', path_prefix='pets')
        assert server.mount_path == '/pets'

    def test_mount_path_strips_slashes(self):
        """Surrounding slashes on ``path_prefix`` are normalised away."""
        server = ServerConfig(name='petstore', spec='petstore.json', path_prefix='/pets/')
        assert server.mount_path == '/pets'

    def test_name_validation_valid(self):
        """Alphanumerics, dashes and underscores are accepted in ``name``."""
        ServerConfig(name='my-api', spec='x.json')
        ServerConfig(name='my_api', spec='x.json')
        ServerConfig(name='api123', spec='x.json')

    def test_name_validation_invalid(self):
        """Spaces or punctuation in ``name`` raise ``ValueError``."""
        with pytest.raises(ValueError, match='alphanumeric'):
            ServerConfig(name='my api!', spec='x.json')


class TestAuthConfig:
    """Env-var substitution on ``AuthConfig``. How a token becomes a header belongs to the type handler."""

    def test_numeric_client_id_coerced_to_string(self):
        """Unquoted numeric ``client_id`` in YAML (Asana 18-digit, Facebook etc.) parses without raising."""
        auth = AuthConfig.model_validate(
            {'type': 'oauth2', 'upstream': {'client_id': 1214443090174193, 'client_secret': 'abc'}}
        )
        assert auth.upstream.client_id == '1214443090174193'
        assert isinstance(auth.upstream.client_id, str)

    def test_numeric_token_coerced_to_string(self):
        """Same coercion applies to ``token`` and ``client_secret`` for consistency."""
        auth = AuthConfig.model_validate({'type': 'bearer', 'token': 99999})
        assert auth.token == '99999'
        assert isinstance(auth.token, str)

    def test_resolve_bearer_from_env(self, monkeypatch):
        """``${VAR}`` placeholders are resolved against the environment."""
        monkeypatch.setenv('TEST_TOKEN', 'env-token')
        auth = AuthConfig(type='bearer', token='${TEST_TOKEN}')
        assert auth.resolve_token() == 'env-token'

    def test_resolve_env_var_with_default(self):
        """``${VAR:-default}`` falls back to the default when the var is unset."""
        auth = AuthConfig(type='bearer', token='${NONEXISTENT_VAR:-fallback-token}')
        assert auth.resolve_token() == 'fallback-token'

    def test_resolve_env_var_unset_no_default(self):
        """An unset env var with no default resolves to ``None``."""
        auth = AuthConfig(type='bearer', token='${NONEXISTENT_VAR}')
        assert auth.resolve_token() is None

    def test_resolve_no_token(self):
        """A config without a token resolves to ``None``."""
        auth = AuthConfig(type='bearer')
        assert auth.resolve_token() is None

    def test_resolve_client_id(self, monkeypatch):
        """OAuth ``client_id`` honours ``${VAR}`` substitution."""
        monkeypatch.setenv('CID', 'client-123')
        auth = AuthConfig(type='oauth2', upstream=UpstreamAuthConfig(client_id='${CID}'))
        assert auth.upstream.resolve_client_id() == 'client-123'

    def test_mcp_token_ttl_defaults(self):
        """MCP token TTLs default to 1 hour access and 24 hours refresh."""
        auth = AuthConfig(type='oauth2')
        assert auth.mcp_access_token_ttl == 3600
        assert auth.mcp_refresh_token_ttl == 86400

    @pytest.mark.parametrize('field', ['mcp_access_token_ttl', 'mcp_refresh_token_ttl'])
    def test_mcp_token_ttl_rejects_non_positive(self, field):
        """A zero or negative TTL fails validation."""
        with pytest.raises(pydantic.ValidationError):
            AuthConfig.model_validate({'type': 'oauth2', field: 0})

    def test_resolve_client_id_direct(self):
        """A literal ``client_id`` is returned verbatim."""
        auth = AuthConfig(type='oauth2', upstream=UpstreamAuthConfig(client_id='direct-id'))
        assert auth.upstream.resolve_client_id() == 'direct-id'

    def test_resolve_client_secret(self, monkeypatch):
        """OAuth ``client_secret`` honours ``${VAR}`` substitution."""
        monkeypatch.setenv('SEC', 'secret-456')
        auth = AuthConfig(type='oauth2', upstream=UpstreamAuthConfig(client_secret='${SEC}'))
        assert auth.upstream.resolve_client_secret() == 'secret-456'


class TestGatewayConfig:
    """Top-level gateway config: defaults, YAML loading, single-spec builder."""

    def test_default_url(self):
        """Default public URL is ``http://localhost:8000``."""
        config = GatewayConfig()
        assert config.url == 'http://localhost:8000'

    def test_custom_url(self):
        """Explicit ``url`` is preserved."""
        config = GatewayConfig(url='https://mcp.example.com')
        assert config.url == 'https://mcp.example.com'

    def test_from_yaml(self, tmp_path):
        """``from_yaml`` loads host/port/servers from a YAML file."""
        data = {
            'host': '127.0.0.1',
            'port': 9000,
            'servers': [
                {'name': 'test', 'spec': 'test.json'},
            ],
        }
        yaml_path = tmp_path / 'config.yml'
        yaml_path.write_text(yaml.dump(data))
        config = GatewayConfig.from_yaml(yaml_path)
        assert config.host == '127.0.0.1'
        assert config.port == 9000
        assert len(config.servers) == 1
        assert config.servers[0].name == 'test'

    def test_from_yaml_not_found(self):
        """Missing YAML files surface as ``FileNotFoundError``."""
        with pytest.raises(FileNotFoundError):
            GatewayConfig.from_yaml('/nonexistent/path.yml')

    def test_from_single_spec(self):
        """``from_single_spec`` produces a one-server config with overridden port."""
        config = GatewayConfig.from_single_spec('petstore.json', name='pets', port=3000)
        assert len(config.servers) == 1
        assert config.servers[0].name == 'pets'
        assert config.servers[0].spec == 'petstore.json'
        assert config.port == 3000

    def test_store_config_default(self):
        """Default token store is in-memory."""
        config = GatewayConfig()
        assert config.store.type == 'memory'

    def test_store_config_redis(self, tmp_path):
        """``store: redis`` block in YAML hydrates url and key prefix."""
        data = {
            'store': {
                'type': 'redis',
                'redis_url': 'redis://redis:6379',
                'key_prefix': 'custom',
            },
            'servers': [],
        }
        yaml_path = tmp_path / 'config.yml'
        yaml_path.write_text(yaml.dump(data))
        config = GatewayConfig.from_yaml(yaml_path)
        assert config.store.type == 'redis'
        assert config.store.redis_url == 'redis://redis:6379'
        assert config.store.key_prefix == 'custom'


@pytest.mark.parametrize('yaml_path', sorted(EXAMPLES_DIR.glob('*.yml')))
def test_example_yaml_parses(yaml_path: pathlib.Path):
    """Every shipped example YAML must parse into a valid GatewayConfig."""
    config = GatewayConfig.from_yaml(yaml_path)
    assert config.servers, f'{yaml_path.name} declares no servers'
    for server in config.servers:
        assert server.name
        assert server.spec


class TestBuildGatewayConfig:
    """Layered composition: ``build_gateway_config(*layers)`` precedence and merging."""

    def test_no_layers_returns_pydantic_defaults(self):
        """With no layers, every field falls back to its Pydantic default."""
        config = build_gateway_config()
        assert config.host == '0.0.0.0'
        assert config.port == 8000
        assert config.transport == 'streamable-http'

    def test_layer_order_later_wins(self):
        """When two layers set the same scalar, the later one in the argument list wins."""
        config = build_gateway_config({'port': 9000}, {'port': 9100})
        assert config.port == 9100

    def test_unset_field_in_later_layer_does_not_clobber_earlier(self):
        """A later layer that omits a field leaves the earlier value intact (the bug we fixed)."""
        config = build_gateway_config({'port': 9000}, {'host': '127.0.0.1'})
        assert config.port == 9000
        assert config.host == '127.0.0.1'

    def test_sub_tree_merges_recursively(self):
        """Setting only ``logging.level`` in a later layer keeps ``logging.format`` from the earlier one."""
        config = build_gateway_config(
            {'logging': {'level': 'DEBUG', 'format': 'json'}},
            {'logging': {'level': 'WARNING'}},
        )
        assert config.logging.level == 'WARNING'
        assert config.logging.format == 'json'

    def test_lists_are_replaced_not_concatenated(self):
        """``servers`` (and any list field) is overwritten wholesale by a later layer."""
        config = build_gateway_config(
            {'servers': [{'name': 'a', 'spec': 'a.json'}]},
            {'servers': [{'name': 'b', 'spec': 'b.json'}]},
        )
        assert [s.name for s in config.servers] == ['b']

    def test_invalid_field_raises_validation_error(self):
        """Pydantic validation runs once on the merged dict; bad input surfaces immediately."""
        with pytest.raises(ValueError):
            build_gateway_config({'port': 'not-an-int'})


class TestLayerHelpers:
    """The pure-data layer producers consumed by ``build_gateway_config``."""

    def test_yaml_layer_reads_file(self, tmp_path):
        """``yaml_layer`` returns the parsed dict ready to compose."""
        yaml_path = tmp_path / 'c.yml'
        yaml_path.write_text(yaml.dump({'port': 9000, 'servers': []}))
        layer = yaml_layer(yaml_path)
        assert layer == {'port': 9000, 'servers': []}

    def test_yaml_layer_empty_file_yields_empty_dict(self, tmp_path):
        """An empty YAML file contributes nothing rather than crashing on ``None``."""
        yaml_path = tmp_path / 'empty.yml'
        yaml_path.write_text('')
        assert yaml_layer(yaml_path) == {}

    def test_yaml_layer_missing_raises(self):
        """Missing files surface as ``FileNotFoundError`` (matches ``from_yaml`` history)."""
        with pytest.raises(FileNotFoundError):
            yaml_layer('/nonexistent/path.yml')

    def test_single_spec_layer_minimal(self):
        """The minimum spec entry produces a well-shaped one-server layer."""
        layer = single_spec_layer(spec='petstore.json', name='pets')
        assert layer == {
            'servers': [
                {
                    'name': 'pets',
                    'spec': 'petstore.json',
                    'auth': AuthConfig().model_dump(),
                },
            ],
        }

    def test_single_spec_layer_carries_base_url_and_auth(self):
        """``base_url`` and a custom ``auth`` flow through into the server dict."""
        layer = single_spec_layer(
            spec='api.json',
            name='api',
            base_url='https://example.com',
            auth=AuthConfig(type='bearer', token='${TOK}'),
        )
        server = layer['servers'][0]
        assert server['base_url'] == 'https://example.com'
        assert server['auth']['type'] == 'bearer'
        assert server['auth']['token'] == '${TOK}'


class TestAuthConfigUpstreamAudience:
    """``upstream.resolve_audience_params`` names the API an upstream token is minted for."""

    def test_empty_when_neither_configured(self):
        """An upstream that issues its own tokens needs no audience named, so nothing is sent."""
        auth = AuthConfig(type='oauth2', upstream=UpstreamAuthConfig(client_id='cid', client_secret='sec'))

        assert auth.upstream.resolve_audience_params() == {}

    def test_resource_emits_rfc_8707_parameter(self):
        """``resource`` is passed through under its RFC 8707 name."""
        auth = AuthConfig(type='oauth2', upstream=UpstreamAuthConfig(resource='https://api.example.com'))

        assert auth.upstream.resolve_audience_params() == {'resource': 'https://api.example.com'}

    def test_audience_emits_auth0_parameter(self):
        """``audience`` is passed through under the spelling Auth0 expects."""
        auth = AuthConfig(type='oauth2', upstream=UpstreamAuthConfig(audience='https://api.example.com'))

        assert auth.upstream.resolve_audience_params() == {'audience': 'https://api.example.com'}

    def test_both_are_sent_when_both_configured(self):
        """Setting both is allowed, for an authorization server that accepts either spelling."""
        auth = AuthConfig(
            type='oauth2',
            upstream=UpstreamAuthConfig(resource='https://res.example.com', audience='https://aud.example.com'),
        )

        assert auth.upstream.resolve_audience_params() == {
            'resource': 'https://res.example.com',
            'audience': 'https://aud.example.com',
        }

    def test_env_var_substitution(self, monkeypatch):
        """Both fields resolve ``${ENV_VAR}`` like the other credential fields do."""
        monkeypatch.setenv('UPSTREAM_AUD', 'https://from-env.example.com')
        auth = AuthConfig(type='oauth2', upstream=UpstreamAuthConfig(audience='${UPSTREAM_AUD}'))

        assert auth.upstream.resolve_audience_params() == {'audience': 'https://from-env.example.com'}
