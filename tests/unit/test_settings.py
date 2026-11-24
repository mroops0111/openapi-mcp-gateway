import pathlib
import re
import typing

import pydantic
import pytest
import yaml

from openapi_mcp_gateway.settings import (
    AuthConfig,
    ClientConfig,
    DNSRebindingProtectionConfig,
    GatewayConfig,
    McpAuthConfig,
    OtherClientsConfig,
    ServerConfig,
    UpstreamAuthConfig,
    build_gateway_config,
    single_spec_layer,
    yaml_layer,
)
from tests.constants import API_URL, BROWSER_ORIGIN, ISSUER


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

    def test_headers_resolve_env_vars_and_skip_unset(self, monkeypatch):
        """Server headers resolve ``${VAR}``, and an unset variable leaves the header out."""
        monkeypatch.setenv('API_VERSION', '2026-03-11')
        monkeypatch.delenv('UNSET_HEADER', raising=False)
        server = ServerConfig(
            name='api', spec='x.json', headers={'Api-Version': '${API_VERSION}', 'X-Opt': '${UNSET_HEADER}'}
        )
        assert server.resolve_headers() == {'Api-Version': '2026-03-11'}

    def test_headers_refuse_authorization(self):
        """Authorization belongs to ``auth``, so a static header cannot set it."""
        with pytest.raises(pydantic.ValidationError, match='Authorization'):
            ServerConfig(name='api', spec='x.json', headers={'authorization': 'Bearer x'})

    def test_instructions_drop_the_trailing_newline_of_a_block_scalar(self):
        """A YAML ``|`` block ends in a newline, which is not part of what the author wrote."""
        server = ServerConfig(name='api', spec='x.json', instructions='Use read_doc first.\nThen search.\n')
        assert server.instructions == 'Use read_doc first.\nThen search.'

    @pytest.mark.parametrize('instructions', [None, '', '  \n'])
    def test_blank_instructions_are_unset(self, instructions):
        """Blank text would send an empty ``instructions`` field, so it is treated as not set."""
        assert ServerConfig(name='api', spec='x.json', instructions=instructions).instructions is None


class TestAuthConfig:
    """Env-var substitution on ``AuthConfig``. How a token becomes a header belongs to the type handler."""

    def test_numeric_client_id_coerced_to_string(self):
        """An unquoted numeric ``client_id`` in YAML, such as Asana's or Facebook's, parses without raising."""
        auth = AuthConfig.model_validate(
            {'upstream': {'type': 'oauth2', 'client_id': 1214443090174193, 'client_secret': 'abc'}}
        )
        assert auth.upstream.client_id == '1214443090174193'
        assert isinstance(auth.upstream.client_id, str)

    def test_numeric_token_coerced_to_string(self):
        """Same coercion applies to ``token`` and ``client_secret`` for consistency."""
        auth = AuthConfig.model_validate({'upstream': {'type': 'bearer', 'token': 99999}})
        assert auth.upstream.token == '99999'
        assert isinstance(auth.upstream.token, str)

    def test_resolve_bearer_from_env(self, monkeypatch):
        """``${VAR}`` placeholders are resolved against the environment."""
        monkeypatch.setenv('TEST_TOKEN', 'env-token')
        auth = AuthConfig(upstream=UpstreamAuthConfig(type='bearer', token='${TEST_TOKEN}'))
        assert auth.upstream.resolve_token() == 'env-token'

    def test_resolve_env_var_with_default(self):
        """``${VAR:-default}`` falls back to the default when the var is unset."""
        auth = AuthConfig(upstream=UpstreamAuthConfig(type='bearer', token='${NONEXISTENT_VAR:-fallback-token}'))
        assert auth.upstream.resolve_token() == 'fallback-token'

    def test_resolve_env_var_unset_no_default(self):
        """An unset env var with no default resolves to ``None``."""
        auth = AuthConfig(upstream=UpstreamAuthConfig(type='bearer', token='${NONEXISTENT_VAR}'))
        assert auth.upstream.resolve_token() is None

    def test_resolve_no_token(self):
        """A config without a token resolves to ``None``."""
        auth = AuthConfig(upstream=UpstreamAuthConfig(type='bearer'))
        assert auth.upstream.resolve_token() is None

    def test_resolve_client_id(self, monkeypatch):
        """OAuth ``client_id`` honours ``${VAR}`` substitution."""
        monkeypatch.setenv('CID', 'client-123')
        auth = AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', client_id='${CID}'))
        assert auth.upstream.resolve_client_id() == 'client-123'

    @pytest.mark.parametrize('field', ['access_token_ttl', 'refresh_token_ttl'])
    def test_mcp_token_ttl_rejects_non_positive(self, field):
        """A zero or negative TTL fails validation."""
        with pytest.raises(pydantic.ValidationError, match='greater than 0'):
            AuthConfig.model_validate({'upstream': {'type': 'oauth2'}, 'mcp': {field: 0}})

    def test_resolve_client_id_direct(self):
        """A literal ``client_id`` is returned verbatim."""
        auth = AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', client_id='direct-id'))
        assert auth.upstream.resolve_client_id() == 'direct-id'

    def test_resolve_client_secret(self, monkeypatch):
        """OAuth ``client_secret`` honours ``${VAR}`` substitution."""
        monkeypatch.setenv('SEC', 'secret-456')
        auth = AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', client_secret='${SEC}'))
        assert auth.upstream.resolve_client_secret() == 'secret-456'


class TestGatewayConfig:
    """Top-level gateway config, covering defaults, YAML loading, and the single-spec builder."""

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
    """How ``build_gateway_config(*layers)`` merges its layers, and which one wins."""

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
        """Pydantic validation runs once on the merged dict, so bad input surfaces immediately."""
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

    def test_single_spec_layer_minimal(self):
        """The layer carries only what the caller supplied, so defaults stay defaults.

        Materialising every field would make each one look explicitly chosen,
        which is what ``AuthConfig`` reads to tell a real setting from a default it can ignore.
        """
        layer = single_spec_layer(spec='api.json', name='api')

        assert layer == {'servers': [{'name': 'api', 'spec': 'api.json', 'auth': {}}]}

    def test_single_spec_layer_carries_base_url_and_auth(self):
        """``base_url`` and a custom ``auth`` flow through into the server dict."""
        layer = single_spec_layer(
            spec='api.json',
            name='api',
            base_url='https://example.com',
            auth=AuthConfig(upstream=UpstreamAuthConfig(type='bearer', token='${TOK}')),
        )
        server = layer['servers'][0]
        assert server['base_url'] == 'https://example.com'
        assert server['auth']['upstream'] == {'type': 'bearer', 'token': '${TOK}'}


class TestAuthLayout:
    """``auth`` holds only ``upstream`` and ``mcp``, so each field means one thing in one place."""

    @pytest.mark.parametrize(
        ('old_key', 'new_place'),
        [
            ('type', 'auth.upstream.type'),
            ('token', 'auth.upstream.token'),
            ('flow', 'auth.upstream.flow'),
            ('issuer', 'auth.mcp.issuer'),
            ('clients', 'auth.mcp.clients'),
            ('mcp_access_token_ttl', 'auth.mcp.access_token_ttl'),
        ],
    )
    def test_a_key_from_the_old_layout_names_its_new_place(self, old_key, new_place):
        """A config written before the split is refused with where each key went, not with a bare unknown key."""
        with pytest.raises(pydantic.ValidationError, match=rf'auth\.{old_key} is now {re.escape(new_place)}'):
            AuthConfig.model_validate({old_key: 'x'})

    def test_every_moved_key_is_named_at_once(self):
        """An operator migrating a config sees every key to move in one error."""
        with pytest.raises(pydantic.ValidationError) as raised:
            AuthConfig.model_validate({'type': 'oauth2', 'clients': {}})
        assert 'auth.upstream.type' in str(raised.value)
        assert 'auth.mcp.clients' in str(raised.value)


class TestAuthConfigUpstreamAudience:
    """``upstream.resolve_audience_params`` names the API an upstream token is minted for."""

    def test_empty_when_neither_configured(self):
        """An upstream that issues its own tokens needs no audience named, so nothing is sent."""
        auth = AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', client_id='cid', client_secret='sec'))

        assert auth.upstream.resolve_audience_params() == {}

    def test_resource_emits_rfc_8707_parameter(self):
        """``resource`` is passed through under its RFC 8707 name."""
        auth = AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', resource=API_URL))

        assert auth.upstream.resolve_audience_params() == {'resource': API_URL}

    def test_audience_emits_auth0_parameter(self):
        """``audience`` is passed through under the spelling Auth0 expects."""
        auth = AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', audience=API_URL))

        assert auth.upstream.resolve_audience_params() == {'audience': API_URL}

    def test_both_are_sent_when_both_configured(self):
        """Setting both is allowed, for an authorization server that accepts either spelling."""
        auth = AuthConfig(
            upstream=UpstreamAuthConfig(
                type='oauth2', resource='https://res.example.com', audience='https://aud.example.com'
            )
        )

        assert auth.upstream.resolve_audience_params() == {
            'resource': 'https://res.example.com',
            'audience': 'https://aud.example.com',
        }

    def test_env_var_substitution(self, monkeypatch):
        """Both fields resolve ``${ENV_VAR}`` like the other credential fields do."""
        monkeypatch.setenv('UPSTREAM_AUD', 'https://from-env.example.com')
        auth = AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', audience='${UPSTREAM_AUD}'))

        assert auth.upstream.resolve_audience_params() == {'audience': 'https://from-env.example.com'}


class TestAuthConfigUpstreamIssuer:
    """``upstream.resolve_issuer`` names the upstream authorization server an ``iss`` is checked against."""

    def test_unset_by_default(self):
        """Without it the gateway has nothing to compare a present ``iss`` with."""
        assert AuthConfig(upstream=UpstreamAuthConfig(type='oauth2')).upstream.resolve_issuer() is None

    def test_resolves_env_var(self, monkeypatch):
        """Accepts ``${ENV_VAR}`` like the other upstream fields."""
        monkeypatch.setenv('UPSTREAM_ISSUER', ISSUER)
        auth = AuthConfig(upstream=UpstreamAuthConfig(type='oauth2', issuer='${UPSTREAM_ISSUER}'))
        assert auth.upstream.resolve_issuer() == ISSUER


class TestUnknownKeysAreRefused:
    """A key the model does not know is a mistake, and dropping it silently can widen access.

    ``policy.annotated_only`` decides which operations a caller may invoke.
    When it was renamed, configs carrying the old spelling kept starting and reporting valid,
    while exposing the whole spec rather than the curated subset, with nothing to read but a longer tool list.
    """

    def test_a_renamed_policy_key_is_refused_rather_than_dropped(self):
        """The exact case that shipped. The old name must fail loudly, not widen the surface."""
        with pytest.raises(pydantic.ValidationError, match='marked_only'):
            build_gateway_config({'servers': [{'name': 'b', 'spec': 'x.json', 'policy': {'marked_only': True}}]})

    @pytest.mark.parametrize(
        ('section', 'payload'),
        [
            ('auth', {'scopes': ['read']}),
            ('auth.upstream', {'upstream_audience': API_URL}),
            ('exposure', {'mode': 'auto'}),
            ('policy', {'marked_only': True}),
        ],
    )
    def test_every_renamed_key_is_refused(self, section, payload):
        """Each rename from the 0.7.0 batch fails closed rather than silently doing nothing."""
        server: dict[str, typing.Any] = {'name': 'b', 'spec': 'x.json'}
        if section == 'auth.upstream':
            server['auth'] = {'upstream': {'type': 'oauth2', **payload}}
        else:
            server[section] = payload

        with pytest.raises(pydantic.ValidationError):
            build_gateway_config({'servers': [server]})

    def test_a_typo_at_the_top_level_is_refused(self):
        """The rule covers the whole config tree, not only the sections that were renamed."""
        with pytest.raises(pydantic.ValidationError, match='transprot'):
            build_gateway_config({'transprot': 'stdio', 'servers': []})

    def test_valid_configs_are_unaffected(self):
        """Refusing the unknown must not refuse the known."""
        config = build_gateway_config(
            {
                'servers': [
                    {
                        'name': 'b',
                        'spec': 'x.json',
                        'policy': {'annotated_only': True, 'allow': ['safe_*']},
                        'exposure': {'style': 'static', 'promote_resources': True},
                    }
                ]
            }
        )

        assert config.servers[0].policy.annotated_only is True
        assert config.servers[0].exposure.promote_resources is True


class TestClientConfig:
    """A client declared under ``auth.mcp.clients`` refuses a default it could never be granted."""

    def test_defaults_beyond_the_set_are_refused(self):
        with pytest.raises(ValueError, match=r"auth\.mcp\.clients\.<client>\.default_scopes names \['issues:write'\]"):
            ClientConfig(scopes=['issues:read'], default_scopes=['issues:write'])


class TestOtherClients:
    """``auth.mcp.other_clients`` admits unlisted metadata document clients.

    It also decides whether clients may register.
    """

    @pytest.mark.parametrize(
        ('mcp', 'admits_others', 'registration'),
        [
            ({}, True, True),
            ({'clients': {'agent': {}}}, False, False),
            ({'other_clients': {}}, True, False),
            ({'clients': {'agent': {}}, 'other_clients': {}}, True, False),
            ({'clients': {'agent': {}}, 'other_clients': False}, False, False),
        ],
        ids=['nothing set', 'clients only', 'other clients', 'both', 'other clients off'],
    )
    def test_the_defaults_keep_a_listed_set_closed(self, mcp, admits_others, registration):
        """Registration stays only for a config naming neither, since it cannot be held to ``trusted_hosts``."""
        config = McpAuthConfig.model_validate(mcp)
        assert (config.resolve_other_clients() is not None) is admits_others
        assert config.accepts_dynamic_registration is registration

    def test_trusted_hosts_compare_without_case(self):
        assert OtherClientsConfig(trusted_hosts=['Claude.AI']).trusted_hosts == ['claude.ai']

    @pytest.mark.parametrize(
        'host', ['https://claude.ai', 'claude.ai:443', '*.example.com', 'claude.ai.', 'bücher.example', 'claude.ai/x']
    )
    def test_a_trusted_host_no_url_host_could_equal_is_refused(self, host):
        """It would never match, and every sign-in it was meant for would fail with nothing pointing at it."""
        with pytest.raises(pydantic.ValidationError, match=r'auth\.mcp\.other_clients\.trusted_hosts names'):
            OtherClientsConfig(trusted_hosts=[host])

    @pytest.mark.parametrize(
        ('mcp', 'refusal'),
        [
            ({'other_clients': True}, 'other_clients'),
            ({'other_clients': False}, 'so no client could sign in'),
            ({'other_clients': {'scopes': ['pets:read']}}, 'other_clients grants scopes but'),
            (
                {'other_clients': {'scopes': ['pets:read'], 'default_scopes': ['pets:write']}},
                r'other_clients\.default_scopes names .* not in its scopes',
            ),
            ({'other_clients': {'trusted_hosts': []}}, 'at least 1 item'),
        ],
    )
    def test_unusable_settings_are_refused(self, mcp, refusal):
        with pytest.raises(pydantic.ValidationError, match=refusal):
            McpAuthConfig.model_validate(mcp)

    def test_other_clients_alone_can_hold_operation_scopes(self):
        """A server with operation scopes can then be offered to clients the operator never listed."""
        config = McpAuthConfig.model_validate(
            {'operation_scopes': {'auto': 'tags'}, 'other_clients': {'scopes': ['pets:read']}}
        )
        assert config.resolve_other_clients() == OtherClientsConfig(scopes=['pets:read'])


class TestScopeSettings:
    """``auth.mcp.operation_scopes`` and ``auth.mcp.clients`` refuse what could never grant or restrict anything."""

    @pytest.mark.parametrize(
        ('auth', 'refusal'),
        [
            ({'mcp': {'clients': {'agent': {'scopes': ['pets:read']}}}}, r'clients \(agent\) grant scopes'),
            (
                {'mcp': {'clients': {'agent': {'scopes': ['pets read']}}}},
                r'auth\.mcp\.clients\.<client>\.scopes names invalid scopes',
            ),
            ({'upstream': {'scope_implies': {'repo': ['public repo']}}}, 'invalid scopes'),
            (
                {
                    'mcp': {
                        'operation_scopes': {'manual': {'pets admin': {'description': 'd', 'operations': ['*']}}},
                        'clients': {'a': {}},
                    }
                },
                'invalid scopes',
            ),
            (
                {
                    'mcp': {
                        'operation_scopes': {'manual': {'admin': {'description': 'd', 'operations': []}}},
                        'clients': {'a': {}},
                    }
                },
                'at least 1 item',
            ),
        ],
    )
    def test_unusable_scope_settings_are_refused(self, auth, refusal):
        with pytest.raises(pydantic.ValidationError, match=refusal):
            AuthConfig.model_validate({**auth, 'upstream': {'type': 'oauth2', **auth.get('upstream', {})}})


class TestSearchFieldWeights:
    """``exposure.search.field_weights`` overrides search ranking field by field, and refuses what cannot work."""

    def test_unset_fields_stay_unset(self):
        """Only the fields written in the config are carried, so the rest keep the gateway's defaults."""
        config = build_gateway_config(
            {
                'servers': [
                    {
                        'name': 'b',
                        'spec': 'x.json',
                        'exposure': {'style': 'dynamic', 'search': {'field_weights': {'summary': 4}}},
                    }
                ]
            }
        )
        search = config.servers[0].exposure.search
        assert search is not None
        assert search.field_weights.model_dump(exclude_none=True) == {'summary': 4}

    @pytest.mark.parametrize(
        ('field_weights', 'message'),
        [
            ({'sumary': 4}, 'sumary'),
            ({'summary': -1}, 'greater than or equal to 0'),
            ({'name': 0, 'summary': 0, 'description': 0, 'path': 0, 'tags': 0}, 'every field to 0'),
        ],
    )
    def test_refuses_weights_that_cannot_work(self, field_weights, message):
        """A misspelt field, a negative weight, and weights that leave nothing to search are refused."""
        with pytest.raises(pydantic.ValidationError, match=message):
            build_gateway_config(
                {
                    'servers': [
                        {
                            'name': 'b',
                            'spec': 'x.json',
                            'exposure': {'style': 'dynamic', 'search': {'field_weights': field_weights}},
                        }
                    ]
                }
            )

    def test_refuses_search_under_static_exposure(self):
        """Only the dynamic meta-tools search, so ``search`` under static exposure would have no effect."""
        with pytest.raises(pydantic.ValidationError, match=r'exposure.search.*no effect'):
            build_gateway_config(
                {'servers': [{'name': 'b', 'spec': 'x.json', 'exposure': {'search': {'field_weights': {'name': 1}}}}]}
            )

    @pytest.mark.parametrize(
        ('server', 'message'),
        [
            pytest.param(
                {'exposure': {'style': 'dynamic', 'promote_resources': True}},
                r'exposure\.promote_resources needs exposure\.style static, so it would have no effect',
                id='promote_resources',
            ),
            pytest.param(
                {'exposure': {'style': 'dynamic'}, 'operations': {'getPet': {'resource': {}}}},
                r'operations\.getPet\.resource needs exposure\.style static, so it would have no effect',
                id='an operation resource',
            ),
        ],
    )
    def test_refuses_resources_under_dynamic_exposure(self, server, message):
        """The meta-tools surface every operation uniformly, so dynamic exposure makes no resource."""
        with pytest.raises(pydantic.ValidationError, match=message):
            build_gateway_config({'servers': [{'name': 'b', 'spec': 'x.json', **server}]})

    def test_refuses_an_operation_resource_without_promotion(self):
        """Without ``exposure.promote_resources`` every operation is a tool, so a resource override has no effect."""
        with pytest.raises(
            pydantic.ValidationError,
            match=r'operations\.getPet\.resource needs exposure\.promote_resources true, so it would have no effect',
        ):
            build_gateway_config(
                {'servers': [{'name': 'b', 'spec': 'x.json', 'operations': {'getPet': {'resource': {}}}}]}
            )

    def test_accepts_an_operation_resource_under_promotion(self):
        """Static exposure with ``exposure.promote_resources`` makes the resource an operation override declares."""
        config = build_gateway_config(
            {
                'servers': [
                    {
                        'name': 'b',
                        'spec': 'x.json',
                        'exposure': {'promote_resources': True},
                        'operations': {'getPet': {'resource': {}}},
                    }
                ]
            }
        )
        assert config.servers[0].operations['getPet'].resource is not None


class TestDNSRebindingProtectionConfig:
    """Which ``Host`` and ``Origin`` values the MCP endpoints accept."""

    def test_origins_without_hosts_are_refused(self):
        """Origins alone would turn on a check that refuses every ``Host``, so the config is refused instead."""
        with pytest.raises(ValueError, match='allowed_hosts'):
            DNSRebindingProtectionConfig(allowed_origins=[BROWSER_ORIGIN])
