import json
import logging
import pathlib
import re

import pytest

from openapi_mcp_gateway.logger import JsonFormatter, TextFormatter, setup


ISO_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}$')


PACKAGE_LOGGER = 'openapi_mcp_gateway'


@pytest.fixture(autouse=True)
def _reset_loggers():
    """Clear handlers and level on package and root loggers between tests."""
    yield
    for name in (PACKAGE_LOGGER, ''):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


class TestSetup:
    """Behaviour of ``logger.setup`` (level, idempotency, file output, third-party fixup)."""

    def test_default(self):
        """``setup()`` configures root at INFO with a single stream handler."""
        setup()
        root = logging.getLogger()
        assert root.level == logging.INFO
        assert any(isinstance(handler, logging.StreamHandler) for handler in root.handlers)

    def test_idempotent(self):
        """Calling ``setup`` twice replaces handlers instead of stacking them."""
        setup(level='DEBUG')
        setup(level='WARNING')
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert root.level == logging.WARNING

    def test_log_file(self, tmp_path: pathlib.Path):
        """``file=`` argument makes log records land on disk in addition to stderr."""
        log_file = tmp_path / 'gateway.log'
        setup(level='DEBUG', file=str(log_file))
        logging.getLogger(PACKAGE_LOGGER).info('hello world')
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert 'hello world' in log_file.read_text(encoding='utf-8')

    def test_third_party_propagates(self):
        """Pre-existing third-party logger handlers are stripped so output flows through root."""
        uvicorn_logger = logging.getLogger('uvicorn.access')
        uvicorn_logger.addHandler(logging.NullHandler())
        uvicorn_logger.propagate = False

        setup()

        assert uvicorn_logger.handlers == []
        assert uvicorn_logger.propagate is True


class TestTextFormatter:
    """Human-readable formatter: timestamp, level/logger colouring, mutation safety."""

    def _record(self) -> logging.LogRecord:
        """Build a minimal ``WARNING`` record for formatter tests."""
        return logging.LogRecord(
            name='openapi_mcp_gateway.test',
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg='hello',
            args=(),
            exc_info=None,
        )

    def test_plain(self):
        """``use_color=False`` emits no ANSI escape codes but keeps ``[LEVEL]`` markers."""
        out = TextFormatter(use_color=False).format(self._record())
        assert '\033[' not in out
        assert '[WARNING]' in out

    def test_colored_wraps_levelname_and_logger(self):
        """Colouring wraps levelname/logger name and reverts the record afterwards."""
        record = self._record()
        out = TextFormatter(use_color=True).format(record)
        assert '\033[33m' in out  # yellow level
        assert '\033[34m' in out  # blue logger name
        assert '\033[0m' in out
        assert record.levelname == 'WARNING'
        assert record.name == 'openapi_mcp_gateway.test'

    def test_iso_time_format(self):
        """Leading timestamp matches ``YYYY-MM-DDTHH:MM:SS.mmm``."""
        out = TextFormatter(use_color=False).format(self._record())
        timestamp = out.split(' [')[0]
        assert ISO_PATTERN.match(timestamp), timestamp


class TestJsonFormatter:
    """Structured JSON formatter payload shape."""

    def test_payload(self):
        """Formatter emits ``{time, level, logger, message}`` with rendered args."""
        record = logging.LogRecord(
            name='openapi_mcp_gateway.test',
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='hello %s',
            args=('world',),
            exc_info=None,
        )
        payload = json.loads(JsonFormatter().format(record))
        assert ISO_PATTERN.match(payload['time']), payload['time']
        assert payload == {
            'time': payload['time'],
            'level': 'INFO',
            'logger': 'openapi_mcp_gateway.test',
            'message': 'hello world',
        }
