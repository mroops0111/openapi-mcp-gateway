import re
import typing
import urllib.parse

import pydantic

from .dataset import AnyOf, ExpectedCall
from .replay import RecordedRequest


# Methods that only read, so a request by one is never an extra write.
READ_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})

PATH_PARAMETER = re.compile(r'\{([^}/]+)\}')

# Methods that change one existing record, so a later one to the same path replaces what an earlier one set.
UPDATE_METHODS = frozenset({'PUT', 'PATCH'})

# What a body holds at a path it does not have, apart from a null it does hold.
_ABSENT = object()


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
    Every way of pairing calls with requests is tried, so a request that fits two calls never blocks a valid pairing.
    An update counts only while no later update of the same record changes the values it was matched on,
    since the upstream ends up holding the later value.
    A run passes when every expected call is matched and no request is one the task forbids.
    A write no expected call accounts for is reported as an extra write without failing the run,
    since a harmless one, such as setting a field after creating a record, says little about the task.
    ``read_only_operations`` names, by server, the operations whose method writes but which only read.
    """
    steps = [step.alternatives for step in expected_calls]
    assigned = _assign(steps, requests, frozenset())
    if assigned is None:
        # No pairing covers every step, so the steps that no request can cover even alone are the ones missing,
        # or, when each can be covered alone, every step, since they cannot all be covered at once.
        alone = [step for step in steps if _assign([step], requests, frozenset()) is None]
        missing_calls = [_describe_step(step) for step in alone or steps]
        assigned = []
    else:
        missing_calls = []
    unused = [request for index, request in enumerate(requests) if index not in assigned]
    forbidden = [request.describe() for request in requests if any(matches(call, request) for call in forbidden_calls)]
    extra_writes = [
        request.describe()
        for request in unused
        if not reads(request.method, request.path, read_only_operations.get(request.server, ()))
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


def _assign(
    steps: typing.Sequence[typing.Sequence[typing.Sequence[ExpectedCall]]],
    requests: typing.Sequence[RecordedRequest],
    used: frozenset[int],
) -> list[int] | None:
    """The indexes of distinct requests that do every step, each by one of its ways, or None when none do."""
    if not steps:
        return []
    for alternative in steps[0]:
        for found in _pairings(alternative, requests, used):
            rest = _assign(steps[1:], requests, used | set(found))
            if rest is not None:
                return found + rest
    return None


def _pairings(
    calls: typing.Sequence[ExpectedCall], requests: typing.Sequence[RecordedRequest], used: frozenset[int]
) -> typing.Iterator[list[int]]:
    """Each way to give every one of ``calls`` a distinct unused request that it matches and no later one undoes."""
    if not calls:
        yield []
        return
    for index, request in enumerate(requests):
        if index in used or not matches(calls[0], request) or _undone(calls[0], index, requests):
            continue
        for rest in _pairings(calls[1:], requests, used | {index}):
            yield [index, *rest]


def _undone(expected: ExpectedCall, index: int, requests: typing.Sequence[RecordedRequest]) -> bool:
    """Whether a later update of the same record set a single value ``expected`` names to another.

    A list may be appended to, as Notion appends blocks, and a POST may create another record, so neither undoes.
    """
    matched = requests[index]
    if matched.method not in UPDATE_METHODS:
        return False
    for later in requests[index + 1 :]:
        if (later.server, later.method, later.path) != (matched.server, matched.method, matched.path):
            continue
        for path, value in _leaves(expected.body):
            if isinstance(value, list):
                continue
            found = _at(later.body, path)
            if found is not _ABSENT and not _contains(found, value):
                return True
    return False


def _leaves(value: typing.Any, path: tuple[str, ...] = ()) -> typing.Iterator[tuple[tuple[str, ...], typing.Any]]:
    """Each value a body names below its objects, with the keys leading to it."""
    if isinstance(value, dict) and value:
        for name, item in value.items():
            yield from _leaves(item, (*path, name))
    else:
        yield path, value


def _at(body: typing.Any, path: tuple[str, ...]) -> typing.Any:
    for name in path:
        if not isinstance(body, dict) or name not in body:
            return _ABSENT
        body = body[name]
    return body


def reads(method: str, path: str, read_only_operations: typing.Sequence[str]) -> bool:
    """Whether a request by ``method`` to ``path`` only reads, by its method or as one of ``read_only_operations``.

    ``path`` may be concrete or a template, and each of ``read_only_operations`` is a method and a path template.
    """
    if method in READ_METHODS:
        return True
    for operation in read_only_operations:
        listed_method, template = operation.split(' ', 1)
        if listed_method == method and _path_parameters(template, path) is not None:
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


def _describe_step(alternatives: typing.Sequence[typing.Sequence[ExpectedCall]]) -> str:
    return ' or '.join(_describe_all(alternative) for alternative in alternatives)


def _describe_all(calls: typing.Sequence[ExpectedCall]) -> str:
    return ' and '.join(_describe(call) for call in calls)


def _describe(expected: ExpectedCall) -> str:
    details = {**expected.path_parameters, **expected.query, **expected.body}
    suffix = f' with {details}' if details else ''
    return f'{expected.method} {expected.path} on {expected.server}{suffix}'
