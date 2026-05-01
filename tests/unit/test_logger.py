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
    yield
    for name in (PACKAGE_LOGGER, ''):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.setLevel(logging.NOTSET)
        lg.propagate = True


class TestSetup:
    def test_default(self):
        setup()
        root = logging.getLogger()
        assert root.level == logging.INFO
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)

    def test_idempotent(self):
        setup(level='DEBUG')
        setup(level='WARNING')
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert root.level == logging.WARNING

    def test_log_file(self, tmp_path: pathlib.Path):
        log_file = tmp_path / 'gw.log'
        setup(level='DEBUG', file=str(log_file))
        logging.getLogger(PACKAGE_LOGGER).info('hello world')
        for h in logging.getLogger().handlers:
            h.flush()
        assert 'hello world' in log_file.read_text(encoding='utf-8')

    def test_third_party_propagates(self):
        # Pretend uvicorn already attached its own handler before setup().
        uv = logging.getLogger('uvicorn.access')
        uv.addHandler(logging.NullHandler())
        uv.propagate = False

        setup()

        assert uv.handlers == []
        assert uv.propagate is True


class TestTextFormatter:
    def _record(self) -> logging.LogRecord:
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
        out = TextFormatter(use_color=False).format(self._record())
        assert '\033[' not in out
        assert '[WARNING]' in out

    def test_colored_wraps_levelname_and_logger(self):
        record = self._record()
        out = TextFormatter(use_color=True).format(record)
        assert '\033[33m' in out  # yellow level
        assert '\033[34m' in out  # blue logger name
        assert '\033[0m' in out
        # Mutation must be reverted so re-formatting stays clean.
        assert record.levelname == 'WARNING'
        assert record.name == 'openapi_mcp_gateway.test'

    def test_iso_time_format(self):
        out = TextFormatter(use_color=False).format(self._record())
        timestamp = out.split(' [')[0]
        assert ISO_PATTERN.match(timestamp), timestamp


class TestJsonFormatter:
    def test_payload(self):
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
