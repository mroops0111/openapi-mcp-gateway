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
        result = [op for op in result if op.tool_exposed]

    if allow:
        result = [op for op in result if any(matches_pattern(op, p) for p in allow)]

    if deny:
        result = [op for op in result if not any(matches_pattern(op, p) for p in deny)]

    return result
