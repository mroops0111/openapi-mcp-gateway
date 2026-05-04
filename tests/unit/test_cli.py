import logging
import pathlib
from unittest import mock

import click
import pytest
from click.testing import CliRunner

from openapi_mcp_gateway import cli


PACKAGE_LOGGER = 'openapi_mcp_gateway'


FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures'
PETSTORE_SPEC = FIXTURES / 'petstore.json'


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
            auth_scopes=None,
            auth_authorization_url=None,
            auth_token_url=None,
        )
        assert auth.type == 'none'
        assert auth.token is None
        assert auth.client_id is None

    def test_token_only_infers_bearer(self):
        """A bare ``--auth-token`` is enough to infer ``bearer``."""
        auth = cli._build_auth_config(
            auth_type=None,
            auth_token='my-token',
            auth_client_id=None,
            auth_client_secret=None,
            auth_scopes=None,
            auth_authorization_url=None,
            auth_token_url=None,
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
            auth_scopes=None,
            auth_authorization_url=None,
            auth_token_url=None,
        )
        assert auth.type == 'oauth2'
        assert auth.client_id == 'cid'
        assert auth.client_secret == 'sec'

    def test_explicit_type_wins(self):
        """An explicit ``--auth-type`` takes precedence over inference."""
        auth = cli._build_auth_config(
            auth_type='api_key',
            auth_token='key123',
            auth_client_id=None,
            auth_client_secret=None,
            auth_scopes=None,
            auth_authorization_url=None,
            auth_token_url=None,
        )
        assert auth.type == 'api_key'

    def test_scopes_split_on_commas(self):
        """``--auth-scopes`` is split on commas and each entry stripped."""
        auth = cli._build_auth_config(
            auth_type='oauth2',
            auth_token=None,
            auth_client_id='cid',
            auth_client_secret='sec',
            auth_scopes='read , write,admin',
            auth_authorization_url=None,
            auth_token_url=None,
        )
        assert auth.scopes == ['read', 'write', 'admin']

    def test_ambiguous_flags_raise_usage_error(self):
        """Auth flags with no token, no client_id and no explicit type are unrecoverable."""
        with pytest.raises(click.UsageError):
            cli._build_auth_config(
                auth_type=None,
                auth_token=None,
                auth_client_id=None,
                auth_client_secret=None,
                auth_scopes='read',
                auth_authorization_url=None,
                auth_token_url=None,
            )

    def test_oauth_urls_carried_through(self):
        """Explicit authorization/token URLs are forwarded onto the resulting ``AuthConfig``."""
        auth = cli._build_auth_config(
            auth_type='oauth2',
            auth_token=None,
            auth_client_id='cid',
            auth_client_secret='sec',
            auth_scopes=None,
            auth_authorization_url='https://auth.example.com/authorize',
            auth_token_url='https://auth.example.com/token',
        )
        assert auth.authorization_url == 'https://auth.example.com/authorize'
        assert auth.token_url == 'https://auth.example.com/token'
