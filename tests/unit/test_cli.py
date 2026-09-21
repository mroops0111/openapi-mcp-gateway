import json
import logging
import pathlib
import typing
from unittest import mock

import click
import pytest
import yaml
from click.testing import CliRunner

from openapi_mcp_gateway import Gateway, cli
from openapi_mcp_gateway.gateway import _policy_summary
from openapi_mcp_gateway.settings import GatewayConfig, PolicyConfig


PACKAGE_LOGGER = 'openapi_mcp_gateway'


FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures'
PETSTORE_SPEC = FIXTURES / 'petstore.json'
UNDOCUMENTED_SPEC = FIXTURES / 'undocumented.json'


@pytest.fixture(autouse=True)
def _reset_loggers():
    """Clear handlers and level on package and root loggers between tests."""
    yield
    for name in (PACKAGE_LOGGER, ''):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def _run(*args, gateway_run: mock.Mock | None = None):
    """Invoke ``cli.main`` with ``Gateway.run`` patched out, returning ``(result, mock)``."""
    runner = CliRunner()
    gateway_run = gateway_run or mock.Mock()
    with mock.patch.object(cli.Gateway, 'run', gateway_run):
        result = runner.invoke(cli.main, list(args), catch_exceptions=False)
    return result, gateway_run


def _run_capture_config(*args) -> tuple[typing.Any, GatewayConfig | None]:
    """Run the CLI without starting uvicorn, returning the effective ``GatewayConfig``."""
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
        result = runner.invoke(cli.main, list(args), catch_exceptions=False)
    return result, captured.get('config')


class TestLoggingFlags:
    """CLI behaviour around ``--log-*``, ``-v`` and ``-q`` flags."""

    def test_help_lists_logging_options(self):
        """``--help`` advertises every logging-related flag."""
        runner = CliRunner()
        result = runner.invoke(cli.main, ['--help'])
        assert result.exit_code == 0
        for flag in ('--log-level', '--log-format', '--log-file', '--verbose', '--quiet'):
            assert flag in result.output

    def test_default_level_is_info(self):
        """No flags → root logger ends up at ``INFO``."""
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets')
        assert result.exit_code == 0, result.output
        assert logging.getLogger().level == logging.INFO

    def test_log_level_explicit(self):
        """``--log-level ERROR`` is honoured on the root logger."""
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '--log-level', 'ERROR')
        assert result.exit_code == 0, result.output
        assert logging.getLogger().level == logging.ERROR

    def test_verbose_implies_debug(self):
        """``-v`` is a shortcut for ``--log-level DEBUG``."""
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '-v')
        assert result.exit_code == 0, result.output
        assert logging.getLogger().level == logging.DEBUG

    def test_quiet_implies_warning(self):
        """``-q`` is a shortcut for ``--log-level WARNING``."""
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '-q')
        assert result.exit_code == 0, result.output
        assert logging.getLogger().level == logging.WARNING

    def test_verbose_and_quiet_conflict(self):
        """Combining ``-v`` and ``-q`` is rejected as a usage error."""
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '-v', '-q')
        assert result.exit_code != 0
        assert '--verbose' in result.output and '--quiet' in result.output

    def test_log_file_writes(self, tmp_path: pathlib.Path):
        """``--log-file`` actually writes log records to the given path."""
        log_file = tmp_path / 'gateway.log'
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '-v', '--log-file', str(log_file))
        assert result.exit_code == 0, result.output
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert log_file.exists()
        content = log_file.read_text(encoding='utf-8')
        assert '[INFO]' in content

    def test_invalid_log_level(self):
        """An unknown level value is rejected by Click before the gateway runs."""
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '--log-level', 'LOUD')
        assert result.exit_code != 0
        assert 'LOUD' in result.output


class TestAuthInference:
    """``_build_auth_config`` derives ``AuthConfig`` from optional auth flags."""

    def test_no_flags_returns_default(self):
        """No auth flags at all yields a default ``AuthConfig`` (type='none')."""
        auth = cli._build_auth_config(
            auth_type=None,
            auth_token=None,
            auth_client_id=None,
            auth_client_secret=None,
            auth_upstream_scopes=None,
            auth_authorization_url=None,
            auth_token_url=None,
            auth_flow=None,
        )
        assert auth.type == 'none'
        assert auth.token is None
        assert auth.upstream.client_id is None

    def test_token_only_infers_bearer(self):
        """A bare ``--auth-token`` is enough to infer ``bearer``."""
        auth = cli._build_auth_config(
            auth_type=None,
            auth_token='my-token',
            auth_client_id=None,
            auth_client_secret=None,
            auth_upstream_scopes=None,
            auth_authorization_url=None,
            auth_token_url=None,
            auth_flow=None,
        )
        assert auth.type == 'bearer'
        assert auth.token == 'my-token'

    def test_client_id_infers_oauth2(self):
        """A bare ``--auth-client-id`` (no type) infers ``oauth2`` even when token is also set."""
        auth = cli._build_auth_config(
            auth_type=None,
            auth_token=None,
            auth_client_id='cid',
            auth_client_secret='sec',
            auth_upstream_scopes=None,
            auth_authorization_url=None,
            auth_token_url=None,
            auth_flow=None,
        )
        assert auth.type == 'oauth2'
        assert auth.upstream.client_id == 'cid'
        assert auth.upstream.client_secret == 'sec'

    def test_explicit_type_wins(self):
        """An explicit ``--auth-type`` takes precedence over inference."""
        auth = cli._build_auth_config(
            auth_type='api_key',
            auth_token='key123',
            auth_client_id=None,
            auth_client_secret=None,
            auth_upstream_scopes=None,
            auth_authorization_url=None,
            auth_token_url=None,
            auth_flow=None,
        )
        assert auth.type == 'api_key'

    def test_scopes_split_on_commas(self):
        """``--auth-scopes`` is split on commas and each entry stripped."""
        auth = cli._build_auth_config(
            auth_type='oauth2',
            auth_token=None,
            auth_client_id='cid',
            auth_client_secret='sec',
            auth_upstream_scopes='read , write,admin',
            auth_authorization_url=None,
            auth_token_url=None,
            auth_flow=None,
        )
        assert auth.upstream.scopes == ['read', 'write', 'admin']

    def test_ambiguous_flags_raise_usage_error(self):
        """Auth flags with no token, no client_id and no explicit type are unrecoverable."""
        with pytest.raises(click.UsageError):
            cli._build_auth_config(
                auth_type=None,
                auth_token=None,
                auth_client_id=None,
                auth_client_secret=None,
                auth_upstream_scopes='read',
                auth_authorization_url=None,
                auth_token_url=None,
                auth_flow=None,
            )

    def test_oauth_urls_carried_through(self):
        """Explicit authorization/token URLs are forwarded onto the resulting ``AuthConfig``."""
        auth = cli._build_auth_config(
            auth_type='oauth2',
            auth_token=None,
            auth_client_id='cid',
            auth_client_secret='sec',
            auth_upstream_scopes=None,
            auth_authorization_url='https://auth.example.com/authorize',
            auth_token_url='https://auth.example.com/token',
            auth_flow=None,
        )
        assert auth.upstream.authorization_url == 'https://auth.example.com/authorize'
        assert auth.upstream.token_url == 'https://auth.example.com/token'


class TestConfigPrecedence:
    """End-to-end: yaml + cli flags compose with non-None-wins precedence."""

    def _yaml_with_port(self, tmp_path: pathlib.Path, **fields) -> pathlib.Path:
        data = {'servers': [{'name': 'pets', 'spec': str(PETSTORE_SPEC)}], **fields}
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

    def test_help_lists_dry_run(self):
        """``--help`` advertises the ``--dry-run`` flag."""
        runner = CliRunner()
        result = runner.invoke(cli.main, ['--help'])
        assert result.exit_code == 0
        assert '--dry-run' in result.output

    def test_dry_run_does_not_serve(self):
        """``--dry-run`` builds the gateway but never calls ``Gateway.run``."""
        result, gateway_run = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '--dry-run')
        assert result.exit_code == 0, result.output
        assert 'Valid' in result.output
        gateway_run.assert_not_called()

    def test_without_dry_run_serves(self):
        """Without ``--dry-run`` the CLI proceeds to ``Gateway.run``."""
        result, gateway_run = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets')
        assert result.exit_code == 0, result.output
        gateway_run.assert_called_once()

    def test_dry_run_summary_lists_server_details(self):
        """The summary names the server and prints its mount, auth, and exposure."""
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '--dry-run')
        assert result.exit_code == 0, result.output
        assert 'pets' in result.output
        assert 'mount' in result.output
        assert 'auth' in result.output
        assert 'exposure' in result.output


class TestPolicySummary:
    """The dry-run names the filter it resolved, so a tool count reads as a decision."""

    def test_an_unfiltered_server_says_so(self):
        """The case worth noticing is the one with nothing narrowing it."""
        assert _policy_summary(PolicyConfig()) == 'no filter, every operation exposed'

    def test_each_filter_is_named(self):
        """An operator comparing intent against reality needs the actual values, not a flag."""
        summary = _policy_summary(PolicyConfig(annotated_only=True, allow=['safe_*'], deny=['*_admin']))

        assert 'annotated only' in summary
        assert "allow ['safe_*']" in summary
        assert "deny ['*_admin']" in summary


class TestDryRunJsonOutput:
    """``--output json`` exists so a caller does not have to parse a coloured table."""

    def _describe(self, *extra: str) -> dict:
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '--dry-run', '--output', 'json', *extra)
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    def test_help_lists_the_option(self):
        runner = CliRunner()
        result = runner.invoke(cli.main, ['--help'])
        assert result.exit_code == 0
        assert '--output' in result.output

    def test_text_remains_the_default(self):
        """Adding a format must not change what someone running the old command sees."""
        without, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '--dry-run')
        explicit, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '--dry-run', '--output', 'text')

        assert without.stdout == explicit.stdout
        assert 'Valid' in without.stdout

    def test_the_cli_and_the_library_emit_the_same_document(self):
        """Two implementations would drift, and the point of this feature is that they cannot."""
        gateway = Gateway()
        gateway.add_server(name='pets', spec=str(PETSTORE_SPEC))

        assert self._describe() == gateway.describe()

    def test_stdout_carries_the_document_and_nothing_else(self):
        """The point of the option is `... --output json | jq`, which a stray log line would break."""
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '--dry-run', '--output', 'json')

        assert json.loads(result.stdout)
        assert 'Loading server' in result.stderr, 'logging should still happen, just not on stdout'

    def test_the_output_is_plain_json(self):
        """No custom encoder, because a caller in another language has none of our types."""
        document = self._describe()

        assert json.dumps(document)
        assert document['valid'] is True
        assert document['totals']['servers'] == 1
        assert document['totals']['tools'] == len(document['servers'][0]['tools'])

    def test_every_field_the_table_prints_is_present(self):
        """The two views describe one thing, so neither may carry a fact the other lacks."""
        server = self._describe()['servers'][0]

        assert set(server) >= {'name', 'mount_path', 'base_url', 'auth', 'policy', 'exposure', 'tools', 'resources'}
        assert server['name'] == 'pets'
        assert server['mount_path'] == '/pets'
        assert server['auth']['type'] == 'none'

    def test_tools_carry_what_a_reviewer_decides_from(self):
        """Name, method and path alone cannot answer whether an operation should be exposed."""
        tool = next(t for t in self._describe()['servers'][0]['tools'] if t['name'] == 'get_pet_by_id')

        assert tool['description']
        assert tool['method'] == 'get'
        assert tool['input_schema']['properties']['petId'] == {'type': 'integer'}
        assert tool['input_schema']['required'] == ['petId']

    def test_auth_and_policy_are_data_rather_than_prose(self):
        """A caller should not have to pull a Python list repr out of an English sentence."""
        server = self._describe()['servers'][0]

        assert server['policy']['allow'] == []
        assert server['policy']['annotated_only'] is False
        assert server['auth']['type'] == 'none'
        assert server['auth']['flow'] is None, 'absent, not empty'
        assert server['policy']['summary'], 'the readable form is kept alongside, not replaced'

    def test_a_pattern_matching_nothing_is_named(self, tmp_path: pathlib.Path):
        """A typo in `allow` is otherwise invisible, since the result is just a shorter list.

        `policy.allow` echoes the request, so on its own it cannot say whether an entry did any
        work. Reporting what matched nothing is what turns the echo into information.
        """
        config = tmp_path / 'config.yml'
        config.write_text(
            yaml.safe_dump(
                {
                    'servers': [
                        {
                            'name': 'pets',
                            'spec': str(PETSTORE_SPEC),
                            'policy': {'allow': ['get*', 'thisMatchesNothing', 'deletePet']},
                        }
                    ]
                }
            )
        )
        result, _ = _run('--config', str(config), '--dry-run', '--output', 'json')
        assert result.exit_code == 0, result.output
        server = json.loads(result.stdout)['servers'][0]

        assert server['policy']['unmatched'] == ['thisMatchesNothing']
        assert {tool['name'] for tool in server['tools']} == {'get_pet_by_id', 'delete_pet'}

    def test_no_credential_reaches_the_document(self, tmp_path: pathlib.Path):
        """This output gets piped, pasted into issues and rendered in a browser.

        ``AuthConfig`` also carries the bearer token and the upstream client secret, so the
        descriptive fields are an allow list rather than a dump, and this test is what holds
        that line when someone later adds a field.
        """
        config = tmp_path / 'config.yml'
        config.write_text(
            yaml.safe_dump(
                {
                    'servers': [
                        {
                            'name': 'pets',
                            'spec': str(PETSTORE_SPEC),
                            'auth': {
                                'type': 'api_key',
                                'token': 'SUPER-SECRET-VALUE',
                                'api_key_header': 'X-Company-Key',
                            },
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
            'type': 'api_key',
            'flow': None,
            'api_key_header': 'X-Company-Key',
            'summary': 'api_key (header X-Company-Key)',
        }

    def test_a_spec_without_descriptions_still_describes_every_tool(self):
        """Plenty of internal specs are generated and carry no prose, which must not blank the field."""
        result, _ = _run('--spec', str(UNDOCUMENTED_SPEC), '--name', 'u', '--dry-run', '--output', 'json')
        assert result.exit_code == 0, result.output
        tools = json.loads(result.stdout)['servers'][0]['tools']

        assert tools, 'the fixture should expose at least one tool'
        assert all(tool['description'] for tool in tools)
        assert any(tool['description'].startswith('GET /orders') for tool in tools)
