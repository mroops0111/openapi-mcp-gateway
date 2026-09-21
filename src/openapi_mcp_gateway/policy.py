import fnmatch

from .openapi import OperationInfo


def matches_pattern(operation: OperationInfo, pattern: str) -> bool:
    """Match ``operation`` against ``pattern``.

    ``METHOD /path`` form is used when ``pattern`` contains a space,
    otherwise the pattern globs against ``operation_id``.
    """
    if ' ' in pattern:
        method_pattern, path_pattern = pattern.split(' ', 1)
        return fnmatch.fnmatch(operation.method.upper(), method_pattern.upper()) and fnmatch.fnmatch(
            operation.path, path_pattern
        )
    return fnmatch.fnmatch(operation.operation_id, pattern)


def unmatched_patterns(operations: list[OperationInfo], *pattern_lists: list[str] | None) -> tuple[str, ...]:
    """Return the patterns that matched no operation at all, in the order they were written.

    A pattern matching nothing is almost always a typo, and it is invisible in the result:
    the filtered list simply lacks an operation nobody notices is missing.
    """
    unmatched: list[str] = []
    for patterns in pattern_lists:
        for pattern in patterns or ():
            if not any(matches_pattern(operation, pattern) for operation in operations):
                unmatched.append(pattern)
    return tuple(unmatched)


def filter_operations(
    operations: list[OperationInfo],
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    annotated_only: bool = False,
) -> list[OperationInfo]:
    """Apply ``annotated_only``, ``allow``, and ``deny`` rules in that order.

    ``annotated_only`` keeps only operations the spec annotates with ``x-mcp-integration.tool``.
    """
    result = operations

    if annotated_only:
        result = [operation for operation in result if operation.tool_exposed]

    if allow:
        result = [operation for operation in result if any(matches_pattern(operation, pattern) for pattern in allow)]

    if deny:
        result = [operation for operation in result if not any(matches_pattern(operation, pattern) for pattern in deny)]

    return result
