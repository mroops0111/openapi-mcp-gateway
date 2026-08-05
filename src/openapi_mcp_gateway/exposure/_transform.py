import logging
import typing

from jsonata import Jsonata
from jsonata.jexception import JException


logger = logging.getLogger(__name__)


class TransformError(ValueError):
    """A JSONata request or response expression that could not be compiled or evaluated."""


def compile_expression(expression: str, *, label: str) -> Jsonata:
    """Compile a JSONata expression once at registration, raising ``TransformError`` on a syntax error.

    ``label`` names the offending expression (for example ``"discover_movies request"``) in the message,
    so a broken override fails fast at startup rather than on the first call.
    """
    try:
        return Jsonata(expression)
    except JException as error:
        raise TransformError(f'Invalid JSONata in {label}: {error}') from error


def apply_transform(compiled: Jsonata, data: typing.Any) -> typing.Any:
    """Evaluate a compiled JSONata expression against ``data``.

    An evaluation error is wrapped as ``TransformError`` so the caller can return it to the client.
    """
    try:
        return compiled.evaluate(data)
    except JException as error:
        raise TransformError(f'JSONata evaluation failed: {error}') from error
