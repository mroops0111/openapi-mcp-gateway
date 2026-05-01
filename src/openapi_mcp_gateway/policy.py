import fnmatch

from .openapi import OperationInfo


def matches_pattern(operation: OperationInfo, pattern: str) -> bool:
    """Return True if ``operation`` matches ``pattern``.

    ``pattern`` may glob-match ``operation_id``, or use ``METHOD /path`` form
    where both method and path support shell-style wildcards.
    """
    # Try as "METHOD path" pattern
    if ' ' in pattern:
        method_pattern, path_pattern = pattern.split(' ', 1)
        return fnmatch.fnmatch(operation.method.upper(), method_pattern.upper()) and fnmatch.fnmatch(
            operation.path, path_pattern
        )
    # Match against operation ID
    return fnmatch.fnmatch(operation.operation_id, pattern)


def filter_operations(
    operations: list[OperationInfo],
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    marked_only: bool = False,
) -> list[OperationInfo]:
    """Apply ``allow``, ``deny``, and ``marked_only`` rules to ``operations``.

    Args:
        operations: Candidate operations (typically from ``parse_spec``).
        allow: If set, keep only operations matching at least one pattern.
        deny: Exclude operations matching any of these patterns.
        marked_only: If True, keep only operations exposed via
            ``x-mcp-integration.expose.tool``.

    Returns:
        The filtered sequence (possibly empty).
    """
    result = operations

    if marked_only:
        result = [op for op in result if op.tool_exposed]

    if allow:
        result = [op for op in result if any(matches_pattern(op, p) for p in allow)]

    if deny:
        result = [op for op in result if not any(matches_pattern(op, p) for p in deny)]

    return result
