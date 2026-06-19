import datetime
import json
import logging
import os
import sys


LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
FORMATS = ('text', 'json')
TEXT_FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
THIRD_PARTY_PREFIXES = ('openapi_mcp_gateway', 'uvicorn', 'mcp')

LEVEL_COLORS = {
    'DEBUG': '\033[36m',
    'INFO': '\033[32m',
    'WARNING': '\033[33m',
    'ERROR': '\033[31m',
    'CRITICAL': '\033[1;31m',
}
LOGGER_COLOR = '\033[34m'
RESET = '\033[0m'


def iso_time(record: logging.LogRecord) -> str:
    """Format ``record.created`` as ISO-8601 with millisecond precision."""
    return datetime.datetime.fromtimestamp(record.created).isoformat(timespec='milliseconds')


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line (time, level, logger, message)."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                'time': iso_time(record),
                'level': record.levelname,
                'logger': record.name,
                'message': record.getMessage(),
            },
            ensure_ascii=False,
        )


class TextFormatter(logging.Formatter):
    """Human-readable log lines, with optional ANSI colour on level and logger names."""

    default_time_format = '%Y-%m-%dT%H:%M:%S'
    default_msec_format = '%s.%03d'

    def __init__(self, use_color: bool = False) -> None:
        super().__init__(TEXT_FORMAT)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        if not self.use_color:
            return super().format(record)
        color = LEVEL_COLORS.get(record.levelname, '')
        original_level = record.levelname
        original_name = record.name
        record.levelname = f'{color}{original_level}{RESET}'
        record.name = f'{LOGGER_COLOR}{original_name}{RESET}'
        try:
            return super().format(record)
        finally:
            record.levelname = original_level
            record.name = original_name


def stderr_supports_color() -> bool:
    """Return False when ``NO_COLOR`` is set or stderr is not a TTY."""
    if os.environ.get('NO_COLOR'):
        return False
    return sys.stderr.isatty()


def setup(level: str = 'INFO', format: str = 'text', file: str | None = None) -> None:
    """Attach JSON or text handlers to the root logger for gateway processes.

    Clears existing root handlers on each call.
    Third-party loggers under ``uvicorn`` / ``mcp`` are stripped of duplicate handlers,
    so records flow through this configuration. stderr uses color only when it is a TTY and ``NO_COLOR`` is unset.
    """
    is_json = format == 'json'

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(JsonFormatter() if is_json else TextFormatter(use_color=stderr_supports_color()))
    handlers: list[logging.Handler] = [stderr_handler]

    if file:
        file_handler = logging.FileHandler(file, encoding='utf-8')
        file_handler.setFormatter(JsonFormatter() if is_json else TextFormatter())
        handlers.append(file_handler)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    for h in handlers:
        root.addHandler(h)

    # Strip third-party loggers' own handlers (uvicorn's column-aligned format,
    # mcp's rich format) so they propagate up to our root.
    for name in list(logging.Logger.manager.loggerDict):
        if name.startswith(THIRD_PARTY_PREFIXES):
            lib = logging.getLogger(name)
            lib.handlers.clear()
            lib.propagate = True
            lib.setLevel(logging.NOTSET)
