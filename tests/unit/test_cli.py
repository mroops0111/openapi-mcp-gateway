import json
import logging
import pathlib
import re
import typing
from unittest import mock

import click
import pytest
import yaml
from click.testing import CliRunner

from openapi_mcp_gateway import Gateway, cli
from openapi_mcp_gateway.cli import instructions_label
from openapi_mcp_gateway.gateway import describe_policy
from openapi_mcp_gateway.settings import GatewayConfig, PolicyConfig
from tests.constants import AUTHORIZE_URL, SHARED_CREDENTIAL_SECURITY, SHARED_CREDENTIAL_UPSTREAM, TOKEN_URL


# The logger every module of the package logs under.
PACKAGE_LOGGER = 'openapi_mcp_gateway'

# The specs the CLI is pointed at, from the shared fixtures directory.
FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures'
SHOP_SPEC = FIXTURES / 'shop.json'
UNDOCUMENTED_SPEC = FIXTURES / 'undocumented.json'
CLIENT_CREDENTIALS_SPEC = FIXTURES / 'client_credentials.json'


@pytest.fixture(autouse=True)
def _reset_loggers():
    """Clear handlers and level on package and root loggers between tests."""
    yield
    for name in (PACKAGE_LOGGER, ''):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


class TestLoggingFlags:
    """CLI behaviour around ``--log-*``, ``-v`` and ``-q`` flags."""

    def test_help_lists_logging_options(self):
        """``--help`` advertises every logging-related flag."""
        runner = CliRunner()
        result = runner.invoke(cli.main, ['serve', '--help'])
        assert result.exit_code == 0
        for flag in ('--log-level', '--log-format', '--log-file', '--verbose', '--quiet'):
            assert flag in result.output

    def test_default_level_is_info(self):
        """With no flags, the root logger ends up at ``INFO``."""
        result, _ = _run('--spec', str(SHOP_SPEC), '--name', 'products')
        assert result.exit_code == 0, result.output
        assert logging.getLogger().level == logging.INFO

    def test_log_level_explicit(self):
        """``--log-level ERROR`` is honoured on the root logger."""
        result, _ = _run('--spec', str(SHOP_SPEC), '--name', 'products', '--log-level', 'ERROR')
        assert result.exit_code == 0, result.output
        assert logging.getLogger().level == logging.ERROR

    def test_verbose_implies_debug(self):
        """``-v`` is a shortcut for ``--log-level DEBUG``."""
        result, _ = _run('--spec', str(SHOP_SPEC), '--name', 'products', '-v')
        assert result.exit_code == 0, result.output
        assert logging.getLogger().level == logging.DEBUG

    def test_quiet_implies_warning(self):
        """``-q`` is a shortcut for ``--log-level WARNING``."""
        result, _ = _run('--spec', str(SHOP_SPEC), '--name', 'products', '-q')
        assert result.exit_code == 0, result.output
        assert logging.getLogger().level == logging.WARNING

    def test_verbose_and_quiet_conflict(self):
        """Combining ``-v`` and ``-q`` is rejected as a usage error."""
        result, _ = _run('--spec', str(SHOP_SPEC), '--name', 'products', '-v', '-q')
        assert result.exit_code != 0
        assert '--verbose' in result.output and '--quiet' in result.output

    def test_log_file_writes(self, tmp_path: pathlib.Path):
        """``--log-file`` actually writes log records to the given path."""
        log_file = tmp_path / 'gateway.log'
        result, _ = _run('--spec', str(SHOP_SPEC), '--name', 'products', '-v', '--log-file', str(log_file))
        assert result.exit_code == 0, result.output
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert log_file.exists()
        content = log_file.read_text(encoding='utf-8')
        assert '[INFO]' in content

    def test_invalid_log_level(self):
        """``--log-level`` takes only the known levels, so an unknown one is a usage error before the gateway runs."""
        result, _ = _run('--spec', str(SHOP_SPEC), '--name', 'products', '--log-level', 'LOUD')
        assert result.exit_code != 0
        assert 'LOUD' in result.output


class TestAuthInference:
    """``build_auth_config`` derives ``AuthConfig`` from optional auth flags."""

    def test_no_flags_returns_default(self):
        """No auth flags at all yields a default ``AuthConfig`` (type='none')."""
        auth = cli.build_auth_config(
            auth_upstream_type=None,
            auth_upstream_credentials=(),
            auth_upstream_client_id=None,
            auth_upstream_client_secret=None,
            auth_upstream_scopes=None,
            auth_upstream_authorization_url=None,
            auth_upstream_token_url=None,
            auth_upstream_flow=None,
        )
        assert auth.upstream.type == 'none'
        assert auth.upstream.credentials == {}
        assert auth.upstream.client_id is None

    def test_credentials_alone_need_no_type(self):
        """``--auth-upstream-credential`` alone serves a server with no flow, each value keyed by its scheme."""
        auth = cli.build_auth_config(
            auth_upstream_type=None,
            auth_upstream_credentials=('APIKey=${TRELLO_KEY}', 'APIToken=tok=en'),
            auth_upstream_client_id=None,
            auth_upstream_client_secret=None,
            auth_upstream_scopes=None,
            auth_upstream_authorization_url=None,
            auth_upstream_token_url=None,
            auth_upstream_flow=None,
        )
        assert auth.upstream.type == 'none'
        assert {scheme: credential.value for scheme, credential in auth.upstream.credentials.items()} == {
            'APIKey': '${TRELLO_KEY}',
            'APIToken': 'tok=en',
        }

    def test_a_credential_without_a_scheme_is_a_usage_error(self):
        with pytest.raises(click.UsageError, match='SCHEME=VALUE'):
            cli.build_auth_config(
                auth_upstream_type=None,
                auth_upstream_credentials=('just-a-token',),
                auth_upstream_client_id=None,
                auth_upstream_client_secret=None,
                auth_upstream_scopes=None,
                auth_upstream_authorization_url=None,
                auth_upstream_token_url=None,
                auth_upstream_flow=None,
            )

    def test_client_id_infers_oauth2(self):
        """A bare ``--auth-upstream-client-id`` (no type) infers ``oauth2``."""
        auth = cli.build_auth_config(
            auth_upstream_type=None,
            auth_upstream_credentials=(),
            auth_upstream_client_id='cid',
            auth_upstream_client_secret='sec',
            auth_upstream_scopes=None,
            auth_upstream_authorization_url=None,
            auth_upstream_token_url=None,
            auth_upstream_flow=None,
        )
        assert auth.upstream.type == 'oauth2'
        assert auth.upstream.client_id == 'cid'
        assert auth.upstream.client_secret == 'sec'

    def test_explicit_type_wins(self):
        """An explicit ``--auth-upstream-type`` takes precedence over inference."""
        auth = cli.build_auth_config(
            auth_upstream_type='oauth2',
            auth_upstream_credentials=('api_key=k',),
            auth_upstream_client_id=None,
            auth_upstream_client_secret=None,
            auth_upstream_scopes=None,
            auth_upstream_authorization_url=None,
            auth_upstream_token_url=None,
            auth_upstream_flow=None,
        )
        assert auth.upstream.type == 'oauth2'

    def test_scopes_split_on_commas(self):
        """``--auth-scopes`` is split on commas and each entry stripped."""
        auth = cli.build_auth_config(
            auth_upstream_type='oauth2',
            auth_upstream_credentials=(),
            auth_upstream_client_id='cid',
            auth_upstream_client_secret='sec',
            auth_upstream_scopes='read , write,admin',
            auth_upstream_authorization_url=None,
            auth_upstream_token_url=None,
            auth_upstream_flow=None,
        )
        assert auth.upstream.scopes == ['read', 'write', 'admin']

    def test_ambiguous_flags_raise_usage_error(self):
        """Auth flags with no credential, no client_id and no explicit type are unrecoverable."""
        with pytest.raises(click.UsageError):
            cli.build_auth_config(
                auth_upstream_type=None,
                auth_upstream_credentials=(),
                auth_upstream_client_id=None,
                auth_upstream_client_secret=None,
                auth_upstream_scopes='read',
                auth_upstream_authorization_url=None,
                auth_upstream_token_url=None,
                auth_upstream_flow=None,
            )

    def test_oauth_urls_carried_through(self):
        """Explicit authorization/token URLs are forwarded onto the resulting ``AuthConfig``."""
        auth = cli.build_auth_config(
            auth_upstream_type='oauth2',
            auth_upstream_credentials=(),
            auth_upstream_client_id='cid',
            auth_upstream_client_secret='sec',
            auth_upstream_scopes=None,
            auth_upstream_authorization_url=AUTHORIZE_URL,
            auth_upstream_token_url=TOKEN_URL,
            auth_upstream_flow=None,
        )
        assert auth.upstream.authorization_url == AUTHORIZE_URL
        assert auth.upstream.token_url == TOKEN_URL


class TestConfigPrecedence:
    """End-to-end: yaml + cli flags compose with non-None-wins precedence."""

    def _yaml_with_port(self, tmp_path: pathlib.Path, **fields) -> pathlib.Path:
        data = {'servers': [{'name': 'products', 'spec': str(SHOP_SPEC)}], **fields}
        path = tmp_path / 'config.yml'
        path.write_text(yaml.dump(data))
        return path

    def test_yaml_port_preserved_when_no_cli_port(self, tmp_path):
        """Regression: omitting ``--port`` must not let a CLI default clobber the YAML value."""
        yaml_path = self._yaml_with_port(tmp_path, port=9000, host='127.0.0.1')
        result, config = _run_capture_config('--config', str(yaml_path))
        assert result.exit_code == 0, result.output
        assert config is not None
        assert config.port == 9000
        assert config.host == '127.0.0.1'

    def test_cli_port_overrides_yaml(self, tmp_path):
        """An explicit ``--port`` wins over the YAML value."""
        yaml_path = self._yaml_with_port(tmp_path, port=9000)
        result, config = _run_capture_config('--config', str(yaml_path), '--port', '7777')
        assert result.exit_code == 0, result.output
        assert config is not None
        assert config.port == 7777

    def test_cli_log_format_does_not_blow_away_yaml_log_level(self, tmp_path):
        """Sub-tree merge keeps YAML ``logging.level`` intact when the CLI only touches ``logging.format``."""
        yaml_path = self._yaml_with_port(
            tmp_path,
            logging={'level': 'WARNING', 'format': 'text'},
        )
        result, config = _run_capture_config('--config', str(yaml_path), '--log-format', 'json')
        assert result.exit_code == 0, result.output
        assert config is not None
        assert config.logging.level == 'WARNING'
        assert config.logging.format == 'json'

    def test_pydantic_default_used_when_neither_yaml_nor_cli_set(self, tmp_path):
        """With neither layer setting a field, the Pydantic default is the floor."""
        yaml_path = self._yaml_with_port(tmp_path)
        result, config = _run_capture_config('--config', str(yaml_path))
        assert result.exit_code == 0, result.output
        assert config is not None
        assert config.host == '0.0.0.0'
        assert config.port == 8000
        assert config.transport == 'streamable-http'


class TestDryRun:
    """``--dry-run`` validates the config and exits without serving."""

    def test_dry_run_does_not_serve(self):
        """``--dry-run`` builds the gateway but never calls ``Gateway.run``."""
        result, gateway_run = _run('--spec', str(SHOP_SPEC), '--name', 'products', '--dry-run')
        assert result.exit_code == 0, result.output
        assert 'Valid' in result.output
        gateway_run.assert_not_called()

    def test_without_dry_run_serves(self):
        """Without ``--dry-run`` the CLI proceeds to ``Gateway.run``."""
        result, gateway_run = _run('--spec', str(SHOP_SPEC), '--name', 'products')
        assert result.exit_code == 0, result.output
        gateway_run.assert_called_once()

    @pytest.mark.parametrize('extra', [('--dry-run',), ()], ids=['dry-run', 'serve'])
    def test_a_config_the_schema_refuses_reads_as_invalid(self, tmp_path: pathlib.Path, extra: tuple[str, ...]):
        """An unknown key fails like any other invalid config, with a message and status 1 rather than a traceback."""
        config = tmp_path / 'config.yml'
        config.write_text(yaml.safe_dump({'servers': [{'name': 'products', 'spec': str(SHOP_SPEC), 'polcy': {}}]}))

        result, gateway_run = _run('--config', str(config), *extra)

        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert '✗ Invalid' in result.output
        assert 'polcy' in result.output
        gateway_run.assert_not_called()

    def test_dry_run_summary_lists_server_details(self):
        """The summary names the server and prints its mount, auth, and exposure."""
        result, _ = _run('--spec', str(SHOP_SPEC), '--name', 'products', '--dry-run')
        assert result.exit_code == 0, result.output
        assert 'products' in result.output
        assert 'mount' in result.output
        assert 'upstream auth' in result.output
        assert 'mcp auth' in result.output
        assert 'exposure' in result.output

    def test_dry_run_names_who_issues_mcp_tokens(self, tmp_path: pathlib.Path):
        """Each side reads as ``mechanism (detail)``, the MCP one as the strategy that issues its tokens reports it."""
        config = tmp_path / 'config.yml'
        auth = {'upstream': SHARED_CREDENTIAL_UPSTREAM, 'mcp': {'clients': {'agent': {'client_secret': 's'}}}}
        server = {'name': 'products', 'spec': str(SHOP_SPEC), **SHARED_CREDENTIAL_SECURITY, 'auth': auth}
        config.write_text(yaml.safe_dump({'servers': [server]}))

        result, _ = _run('--config', str(config), '--dry-run')

        assert result.exit_code == 0, result.output
        assert re.search(r'upstream auth\s+credentials \(upstreamToken\)\n', click.unstyle(result.output))
        assert re.search(r'mcp auth\s+gateway \(client_credentials\)\n', click.unstyle(result.output))

    def test_the_json_states_who_issues_mcp_tokens(self, tmp_path: pathlib.Path):
        """Shaped like ``auth`` in the config, so a check can tell an agent-protected server from an open one."""
        config = tmp_path / 'config.yml'
        servers = [
            {
                'name': 'products',
                'spec': str(SHOP_SPEC),
                **SHARED_CREDENTIAL_SECURITY,
                'auth': {
                    'upstream': SHARED_CREDENTIAL_UPSTREAM,
                    'mcp': {'clients': {'agent': {'client_secret': 's'}}},
                },
            },
            {'name': 'open', 'spec': str(SHOP_SPEC)},
        ]
        config.write_text(yaml.safe_dump({'servers': servers}))

        result, _ = _run('--config', str(config), '--dry-run', '--output', 'json')

        assert result.exit_code == 0, result.output
        protected, open_server = (server['auth'] for server in json.loads(result.stdout)['servers'])
        assert protected['upstream'] == {'type': 'none', 'credentials': 'upstreamToken'}
        assert protected['mcp'] == {
            'issuer': 'http://localhost:8000/products',
            'grant_types': ['client_credentials'],
            'required_scopes': ['api'],
        }
        assert open_server['mcp'] == {'issuer': None, 'grant_types': None, 'required_scopes': None}


class TestSecurityFlags:
    """``--security-scheme`` and ``--security`` declare the security a spec leaves out, as their YAML keys do."""

    def test_a_declared_scheme_serves_a_credential_for_a_spec_without_one(self):
        result, _ = _run(
            '--spec',
            str(SHOP_SPEC),
            '--security-scheme',
            'token={"type": "http", "scheme": "bearer"}',
            '--security',
            'token',
            '--auth-upstream-credential',
            'token=secret',
            '--dry-run',
            '--output',
            'json',
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)['servers'][0]['auth']['upstream']['credentials'] == 'token'

    def test_schemes_needed_together_join_with_a_plus(self):
        key = '{"type": "apiKey", "in": "query", "name": "%s"}'
        result, config = _run_capture_config(
            '--spec',
            str(SHOP_SPEC),
            '--security-scheme',
            f'APIKey={key % "key"}',
            '--security-scheme',
            f'APIToken={key % "token"}',
            '--security',
            'APIKey+APIToken',
            '--security',
            'APIKey',
            '--auth-upstream-credential',
            'APIKey=k',
            '--auth-upstream-credential',
            'APIToken=t',
        )

        assert result.exit_code == 0, result.output
        assert config is not None
        assert config.servers[0].security == [{'APIKey': [], 'APIToken': []}, {'APIKey': []}]

    @pytest.mark.parametrize(
        ('flag', 'value', 'message'),
        [
            ('--security-scheme', 'token', 'NAME=JSON'),
            ('--security-scheme', 'token={"type": ', 'NAME=JSON'),
            ('--security-scheme', 'token=["bearer"]', 'NAME=JSON'),
            ('--security', 'APIKey+', 'SCHEME or SCHEME\\+SCHEME'),
        ],
    )
    def test_a_malformed_value_is_a_usage_error(self, flag, value, message):
        result, _ = _run('--spec', str(SHOP_SPEC), flag, value)

        assert result.exit_code == 2
        assert re.search(message, result.output)


class TestInstructionsLabel:
    """The dry-run table shows enough of a server's instructions to confirm they took effect."""

    def test_unset_instructions_say_so(self):
        assert instructions_label(None) == 'none'

    def test_a_single_short_line_is_shown_whole(self):
        assert instructions_label('Use read_doc first.') == 'Use read_doc first.'

    def test_later_lines_are_marked_as_left_out(self):
        assert instructions_label('Use read_doc first.\nThen search.') == 'Use read_doc first. …'

    def test_a_long_first_line_is_cut(self):
        label = instructions_label('x' * 100)

        assert label == 'x' * 60 + '…'

    def test_the_table_prints_the_label(self):
        """The summary carries the line, so an author sees the setting without asking for JSON."""
        result, _ = _run('--spec', str(SHOP_SPEC), '--name', 'products', '--dry-run')

        assert result.exit_code == 0, result.output
        assert 'instructions' in result.output


class TestPolicySummary:
    """The dry-run names the filter it resolved, so a tool count reads as a decision."""

    def test_an_unfiltered_server_says_so(self):
        """The case worth noticing is the one with nothing narrowing it."""
        assert describe_policy(PolicyConfig()) == 'no filter, every operation exposed'

    def test_each_filter_is_named(self):
        """An operator comparing intent against reality needs the actual values, not a flag."""
        summary = describe_policy(PolicyConfig(annotated_only=True, allow=['safe_*'], deny=['*_admin']))

        assert 'annotated only' in summary
        assert "allow ['safe_*']" in summary
        assert "deny ['*_admin']" in summary


class TestDryRunJsonOutput:
    """``--output json`` exists so a caller does not have to parse a coloured table."""

    def _describe(self, *extra: str) -> dict:
        result, _ = _run('--spec', str(SHOP_SPEC), '--name', 'products', '--dry-run', '--output', 'json', *extra)
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    def test_text_remains_the_default(self):
        """Adding a format must not change what someone running the old command sees."""
        without, _ = _run('--spec', str(SHOP_SPEC), '--name', 'products', '--dry-run')
        explicit, _ = _run('--spec', str(SHOP_SPEC), '--name', 'products', '--dry-run', '--output', 'text')

        assert without.stdout == explicit.stdout
        assert 'Valid' in without.stdout

    def test_the_cli_and_the_library_emit_the_same_document(self):
        """Two implementations would drift, and the point of this feature is that they cannot."""
        gateway = Gateway()
        gateway.add_server(name='products', spec=str(SHOP_SPEC))

        assert self._describe() == gateway.describe()

    def test_stdout_carries_the_document_and_nothing_else(self):
        """The point of the option is `... --output json | jq`, which a stray log line would break."""
        result, _ = _run('--spec', str(SHOP_SPEC), '--name', 'products', '--dry-run', '--output', 'json')

        assert json.loads(result.stdout)
        assert 'Loading server' in result.stderr, 'logging should still happen, just not on stdout'

    def test_every_field_the_table_prints_is_present(self):
        """The two views describe one thing, so neither may carry a fact the other lacks."""
        server = self._describe()['servers'][0]

        assert set(server) >= {'name', 'mount_path', 'base_url', 'auth', 'exposure', 'tools', 'resources'}
        assert server['name'] == 'products'
        assert server['mount_path'] == '/products'
        assert server['auth']['upstream']['type'] == 'none'

    def test_tools_carry_what_a_reviewer_decides_from(self):
        """Name, method and path alone cannot answer whether an operation should be exposed."""
        tool = next(t for t in self._describe()['servers'][0]['tools'] if t['name'] == 'get_product_by_id')

        assert tool['description']
        assert tool['method'] == 'get'
        assert tool['input_schema']['properties']['productId'] == {'type': 'integer'}
        assert tool['input_schema']['required'] == ['productId']

    def test_the_document_carries_no_field_the_caller_could_derive(self):
        """Echoing the config, a prose summary and a count are all things the reader already has.

        They cost bytes, add noise to a diff between two versions of a config,
        and can disagree with the data they were derived from.
        """
        document = self._describe()
        server = document['servers'][0]

        assert 'totals' not in document
        assert 'valid' not in document, 'a field that can never be false says nothing'
        assert 'policy' not in server, 'the caller holds the config already'
        assert 'summary' not in server['auth']
        assert server['auth']['upstream']['type'] == 'none'
        assert server['auth']['upstream'] == {'type': 'none'}, 'a field the server does not have is left out'

    def test_instructions_are_reported_as_the_client_receives_them(self, tmp_path: pathlib.Path):
        """The full text is what the model reads, like a tool description, and an unset value reads null."""
        config = tmp_path / 'config.yml'
        config.write_text(
            yaml.safe_dump(
                {
                    'servers': [
                        {'name': 'products', 'spec': str(SHOP_SPEC), 'instructions': 'Line one.\nLine two.\n'},
                        {'name': 'plain', 'spec': str(SHOP_SPEC)},
                    ]
                }
            )
        )
        result, _ = _run('--config', str(config), '--dry-run', '--output', 'json')
        assert result.exit_code == 0, result.output

        products, plain = json.loads(result.stdout)['servers']
        assert products['instructions'] == 'Line one.\nLine two.'
        assert plain['instructions'] is None

    def test_a_pattern_matching_nothing_is_warned_about(self, tmp_path: pathlib.Path):
        """A typo in `allow` is otherwise invisible, since the result is just a shorter list.

        Warned at load rather than reported in the document, so it reaches everyone who starts the gateway,
        not only the callers who ask for JSON and then read that field.
        """
        config = tmp_path / 'config.yml'
        config.write_text(
            yaml.safe_dump(
                {
                    'servers': [
                        {
                            'name': 'products',
                            'spec': str(SHOP_SPEC),
                            'policy': {'allow': ['get*', 'thisMatchesNothing', 'deleteProduct']},
                        }
                    ]
                }
            )
        )
        result, _ = _run('--config', str(config), '--dry-run', '--output', 'json')
        assert result.exit_code == 0, result.output

        assert 'thisMatchesNothing' in result.stderr
        assert 'matched no operation' in result.stderr
        server = json.loads(result.stdout)['servers'][0]
        assert {tool['name'] for tool in server['tools']} == {'get_product_by_id', 'delete_product'}

    def test_the_reported_flow_is_the_one_that_was_resolved(self, tmp_path: pathlib.Path):
        """A config naming no flow still runs one, picked from the spec.

        Echoing `auth.upstream.flow` reported null while client_credentials was live,
        which describes the config rather than what the server does.
        """
        config = tmp_path / 'config.yml'
        config.write_text(
            yaml.safe_dump(
                {
                    'servers': [
                        {
                            'name': 'secure',
                            'spec': str(CLIENT_CREDENTIALS_SPEC),
                            'auth': {'upstream': {'type': 'oauth2', 'client_id': 'cid', 'client_secret': 'sec'}},
                        }
                    ]
                }
            )
        )
        result, _ = _run('--config', str(config), '--dry-run', '--output', 'json')
        assert result.exit_code == 0, result.output

        auth = json.loads(result.stdout)['servers'][0]['auth']
        assert auth['upstream']['type'] == 'oauth2'
        assert auth['upstream']['flow'] == 'client_credentials'

    def test_no_credential_reaches_the_document(self, tmp_path: pathlib.Path):
        """This output gets piped, pasted into issues and rendered in a browser.

        ``AuthConfig`` also carries the credential values and the upstream client secret,
        so the descriptive fields are an allow list rather than a dump,
        and this test is what holds that line when someone later adds a field.
        """
        config = tmp_path / 'config.yml'
        config.write_text(
            yaml.safe_dump(
                {
                    'servers': [
                        {
                            'name': 'products',
                            'spec': str(SHOP_SPEC),
                            'security_schemes': {
                                'company': {'type': 'apiKey', 'in': 'header', 'name': 'X-Company-Key'}
                            },
                            'security': [{'company': []}],
                            'auth': {'upstream': {'credentials': {'company': {'value': 'SUPER-SECRET-VALUE'}}}},
                        }
                    ]
                }
            )
        )
        result, _ = _run('--config', str(config), '--dry-run', '--output', 'json')
        assert result.exit_code == 0, result.output

        assert 'SUPER-SECRET-VALUE' not in result.stdout
        auth = json.loads(result.stdout)['servers'][0]['auth']
        assert auth == {
            'upstream': {'type': 'none', 'credentials': 'company'},
            'mcp': {'issuer': None, 'grant_types': None, 'required_scopes': None},
        }

    def test_a_spec_without_descriptions_still_describes_every_tool(self):
        """Plenty of internal specs are generated and carry no prose, which must not blank the field."""
        result, _ = _run('--spec', str(UNDOCUMENTED_SPEC), '--name', 'u', '--dry-run', '--output', 'json')
        assert result.exit_code == 0, result.output
        tools = json.loads(result.stdout)['servers'][0]['tools']

        assert tools, 'the fixture should expose at least one tool'
        assert all(tool['description'] for tool in tools)
        assert any(tool['description'].startswith('GET /orders') for tool in tools)


class TestSubcommands:
    """Subcommands an installed package adds through the gateway's entry point group."""

    def test_a_subcommand_runs_without_serving(self):
        """An installed subcommand runs on its own, with neither --spec nor --config, and never starts the gateway."""
        result, gateway_run = _run_with_subcommand('hello')
        assert result.exit_code == 0, result.output
        assert result.output == 'hello\n'
        gateway_run.assert_not_called()

    def test_help_lists_serve_and_the_installed_subcommands(self):
        """``--help`` names the gateway's own serve beside each installed subcommand."""
        result, _ = _run_with_subcommand('--help')
        assert result.exit_code == 0, result.output
        assert 'Commands:\n  hello\n  serve ' in result.output

    def test_serving_needs_the_serve_subcommand(self):
        """Options alone name no subcommand, so the gateway does not serve."""
        result, gateway_run = _run_with_subcommand('--spec', str(SHOP_SPEC))
        assert result.exit_code == 2
        assert "No such option '--spec'" in result.output
        gateway_run.assert_not_called()

    def test_an_unknown_subcommand_is_refused(self):
        """A name no installed package adds is a usage error, not a serve."""
        result, gateway_run = _run_with_subcommand('nope')
        assert result.exit_code == 2
        assert "No such command 'nope'" in result.output
        gateway_run.assert_not_called()


def _run(*args, gateway_run: mock.Mock | None = None):
    """Invoke ``serve`` with ``Gateway.run`` patched out, returning ``(result, mock)``."""
    runner = CliRunner()
    gateway_run = gateway_run or mock.Mock()
    with mock.patch.object(cli.Gateway, 'run', gateway_run):
        result = runner.invoke(cli.main, ['serve', *args], catch_exceptions=False)
    return result, gateway_run


def _run_capture_config(*args) -> tuple[typing.Any, GatewayConfig | None]:
    """Run ``serve`` without starting uvicorn, returning the effective ``GatewayConfig``."""
    captured: dict[str, GatewayConfig] = {}
    real_from_config = cli.Gateway.from_config

    def spy(config: GatewayConfig) -> typing.Any:
        captured['config'] = config
        return real_from_config(config)

    runner = CliRunner()
    with (
        mock.patch.object(cli.Gateway, 'from_config', side_effect=spy),
        mock.patch.object(cli.Gateway, 'run', mock.Mock()),
    ):
        result = runner.invoke(cli.main, ['serve', *args], catch_exceptions=False)
    return result, captured.get('config')


def _run_with_subcommand(*args: str) -> tuple[typing.Any, mock.Mock]:
    """Invoke the gateway's command with one installed subcommand, ``hello``, and ``Gateway.run`` patched out."""

    @click.command()
    def hello() -> None:
        click.echo('hello')

    entry_point = mock.Mock()
    entry_point.name = 'hello'
    entry_point.load.return_value = hello
    gateway_run = mock.Mock()
    with (
        mock.patch.object(cli, 'command_entry_points', return_value=[entry_point]),
        mock.patch.object(cli.Gateway, 'run', gateway_run),
    ):
        result = CliRunner().invoke(cli.main, list(args))
    return result, gateway_run
