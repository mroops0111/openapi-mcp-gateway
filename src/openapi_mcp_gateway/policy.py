"""Policy controls for filtering which OpenAPI operations are exposed as MCP tools."""

import fnmatch

from .openapi import OperationInfo


def matches_pattern(operation: OperationInfo, pattern: str) -> bool:
    """Check if an operation matches a filter pattern.

    Supported patterns:
        - Operation ID: "getUsers", "create*"
        - METHOD path: "GET /users/*", "POST /api/*"
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
    """Filter operations based on policy rules.

    Args:
        operations: All parsed operations.
        allow: If set, only operations matching at least one pattern are included.
        deny: Operations matching any pattern are excluded.
        marked_only: If True, only include operations with x-mcp-integration.expose.tool.

    Returns:
        Filtered list of operations.
    """
    result = operations

    if marked_only:
        result = [op for op in result if op.tool_exposed]

    if allow:
        result = [op for op in result if any(matches_pattern(op, p) for p in allow)]

    if deny:
        result = [op for op in result if not any(matches_pattern(op, p) for p in deny)]

    return result
