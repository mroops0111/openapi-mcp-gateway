"""Tests for the CLI."""

import logging
import pathlib
from unittest import mock

import pytest
from click.testing import CliRunner

from openapi_mcp_gateway import cli


PACKAGE_LOGGER = 'openapi_mcp_gateway'


FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures'
PETSTORE_SPEC = FIXTURES / 'petstore.json'


@pytest.fixture(autouse=True)
def _reset_loggers():
    yield
    for name in (PACKAGE_LOGGER, ''):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.setLevel(logging.NOTSET)
        lg.propagate = True


def _run(*args, gateway_run: mock.Mock | None = None):
    runner = CliRunner()
    gateway_run = gateway_run or mock.Mock()
    with mock.patch.object(cli.Gateway, 'run', gateway_run):
        result = runner.invoke(cli.main, list(args), catch_exceptions=False)
    return result, gateway_run


class TestLoggingFlags:
    def test_help_lists_logging_options(self):
        runner = CliRunner()
        result = runner.invoke(cli.main, ['--help'])
        assert result.exit_code == 0
        for flag in ('--log-level', '--log-format', '--log-file', '--verbose', '--quiet'):
            assert flag in result.output

    def test_default_level_is_info(self):
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets')
        assert result.exit_code == 0, result.output
        assert logging.getLogger().level == logging.INFO

    def test_log_level_explicit(self):
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '--log-level', 'ERROR')
        assert result.exit_code == 0, result.output
        assert logging.getLogger().level == logging.ERROR

    def test_verbose_implies_debug(self):
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '-v')
        assert result.exit_code == 0, result.output
        assert logging.getLogger().level == logging.DEBUG

    def test_quiet_implies_warning(self):
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '-q')
        assert result.exit_code == 0, result.output
        assert logging.getLogger().level == logging.WARNING

    def test_verbose_and_quiet_conflict(self):
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '-v', '-q')
        assert result.exit_code != 0
        assert 'mutually exclusive' in result.output

    def test_log_file_writes(self, tmp_path: pathlib.Path):
        log_file = tmp_path / 'gw.log'
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '-v', '--log-file', str(log_file))
        assert result.exit_code == 0, result.output
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert log_file.exists()
        content = log_file.read_text(encoding='utf-8')
        # An INFO-level "Registered server" entry should land in the file.
        assert 'Registered server' in content

    def test_invalid_log_level(self):
        result, _ = _run('--spec', str(PETSTORE_SPEC), '--name', 'pets', '--log-level', 'LOUD')
        assert result.exit_code != 0
        assert 'Invalid value' in result.output or 'invalid choice' in result.output.lower()
