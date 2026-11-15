import re
import typing
import urllib.parse

import pydantic

from .dataset import ExpectedCall
from .replay import RecordedRequest


# Methods that only read, so a model may send as many as it likes without failing a task.
READ_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})

PATH_PARAMETER = re.compile(r'\{([^}/]+)\}')


class Grade(pydantic.BaseModel):
    """Whether one run of a task did what the task asked, and what it got wrong when it did not."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    passed: bool
    # Expected calls no request matched, each described in one line.
    missing_calls: list[str]
    # Requests that change something upstream and that no expected call accounts for.
    unexpected_writes: list[str]


def grade(expected_calls: typing.Sequence[ExpectedCall], requests: typing.Sequence[RecordedRequest]) -> Grade:
    """Grade the requests one run sent upstream against what its task expected.

    Each expected call needs a request of its own that matches it, in any order.
    A run passes when every expected call is matched and every request that writes is one of them,
    so reading more than needed costs nothing, while changing more than asked fails the task.
    """
    unused = list(requests)
    missing_calls = []
    for expected in expected_calls:
        match = next((request for request in unused if matches(expected, request)), None)
        if match is None:
            missing_calls.append(_describe(expected))
        else:
            unused.remove(match)
    unexpected_writes = [
        f'{request.method} {request.path} on {request.server}'
        for request in unused
        if request.method not in READ_METHODS
    ]
    return Grade(
        passed=not missing_calls and not unexpected_writes,
        missing_calls=missing_calls,
        unexpected_writes=unexpected_writes,
    )


def matches(expected: ExpectedCall, request: RecordedRequest) -> bool:
    """Whether ``request`` is the call ``expected`` describes, carrying every value it names."""
    if (expected.server, expected.method) != (request.server, request.method):
        return False
    path_parameters = _path_parameters(expected.path, request.path)
    if path_parameters is None:
        return False
    if any(path_parameters.get(name) != str(value) for name, value in expected.path_parameters.items()):
        return False
    if any(str(value) not in request.query.get(name, []) for name, value in expected.query.items()):
        return False
    return _contains(request.body, expected.body)


def _path_parameters(template: str, path: str) -> dict[str, str] | None:
    """The values ``path`` gives ``template``'s placeholders, or None when it does not fit the template."""
    pattern = (
        '^'
        + ''.join(
            f'(?P<{_group_name(part)}>[^/]+)' if index % 2 else re.escape(part)
            for index, part in enumerate(PATH_PARAMETER.split(template))
        )
        + '/?$'
    )
    found = re.match(pattern, path)
    if found is None:
        return None
    names = PATH_PARAMETER.findall(template)
    return {name: urllib.parse.unquote(found.group(_group_name(name))) for name in names}


def _group_name(placeholder: str) -> str:
    return 'parameter_' + re.sub(r'\W', '_', placeholder)


def _contains(actual: typing.Any, expected: typing.Any) -> bool:
    """Whether ``actual`` holds everything ``expected`` names, comparing scalars as text.

    A form-encoded body arrives as lists of strings, so a scalar is also found among a list's items.
    """
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return not expected
        return all(name in actual and _contains(actual[name], value) for name, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and all(any(_contains(item, wanted) for item in actual) for wanted in expected)
    if isinstance(actual, list):
        return any(_contains(item, expected) for item in actual)
    return _text(actual) == _text(expected)


def _text(value: typing.Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _describe(expected: ExpectedCall) -> str:
    details = {**expected.path_parameters, **expected.query, **expected.body}
    suffix = f' with {details}' if details else ''
    return f'{expected.method} {expected.path} on {expected.server}{suffix}'
