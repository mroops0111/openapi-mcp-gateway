import re
import typing
import urllib.parse

import pydantic

from .dataset import AnyOf, ExpectedCall
from .replay import RecordedRequest


# Methods that only read, so a request by one is never an extra write.
READ_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})

PATH_PARAMETER = re.compile(r'\{([^}/]+)\}')


class Grade(pydantic.BaseModel):
    """Whether one run of a task did what the task asked, and what it got wrong when it did not."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    passed: bool
    # Expected calls no request matched, each described in one line.
    missing_calls: list[str]
    # Requests the task forbids that the run made, any one of which fails it.
    forbidden_calls: list[str]
    # Requests that change something upstream and that no expected call accounts for, a side effect the report counts.
    extra_writes: list[str]


def grade(
    expected_calls: typing.Sequence[ExpectedCall | AnyOf],
    forbidden_calls: typing.Sequence[ExpectedCall],
    requests: typing.Sequence[RecordedRequest],
    read_only_operations: typing.Mapping[str, typing.Sequence[str]],
) -> Grade:
    """Grade the requests one run sent upstream against what its task expected and forbade.

    Each expected call needs a request of its own that matches it, in any order.
    An ``any_of`` step needs one of its ways done, a call or an ``all_of`` group with a request for each of its calls.
    A run passes when every expected call is matched and no request is one the task forbids.
    A write no expected call accounts for is reported as an extra write without failing the run,
    since a harmless one, such as setting a field after creating a record, says little about the task.
    ``read_only_operations`` names, by server, the operations whose method writes but which only read.
    """
    unused = list(requests)
    missing_calls = []
    for step in expected_calls:
        found = next(
            (found for alternative in step.alternatives if (found := _match_all(alternative, unused)) is not None), None
        )
        if found is None:
            missing_calls.append(' or '.join(_describe_all(alternative) for alternative in step.alternatives))
        else:
            for request in found:
                unused.remove(request)
    forbidden = [request.describe() for request in requests if any(matches(call, request) for call in forbidden_calls)]
    extra_writes = [
        request.describe()
        for request in unused
        if request.method not in READ_METHODS and not _reads(request, read_only_operations.get(request.server, ()))
    ]
    return Grade(
        passed=not missing_calls and not forbidden,
        missing_calls=missing_calls,
        forbidden_calls=forbidden,
        extra_writes=extra_writes,
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


def _match_all(
    calls: typing.Sequence[ExpectedCall], requests: typing.Sequence[RecordedRequest]
) -> list[RecordedRequest] | None:
    """A distinct request among ``requests`` for each of ``calls``, or None when one has none."""
    available = list(requests)
    found = []
    for call in calls:
        request = next((request for request in available if matches(call, request)), None)
        if request is None:
            return None
        available.remove(request)
        found.append(request)
    return found


def _reads(request: RecordedRequest, read_only_operations: typing.Sequence[str]) -> bool:
    """Whether ``request`` is one of ``read_only_operations``, each a method and a path template."""
    for operation in read_only_operations:
        method, template = operation.split(' ', 1)
        if method == request.method and _path_parameters(template, request.path) is not None:
            return True
    return False


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


def _describe_all(calls: typing.Sequence[ExpectedCall]) -> str:
    return ' and '.join(_describe(call) for call in calls)


def _describe(expected: ExpectedCall) -> str:
    details = {**expected.path_parameters, **expected.query, **expected.body}
    suffix = f' with {details}' if details else ''
    return f'{expected.method} {expected.path} on {expected.server}{suffix}'
