from mcp.types import CallToolResult, TextContent


def text_of(result: CallToolResult) -> str:
    """The text a tool result carries in its one content block."""
    [content] = result.content
    assert isinstance(content, TextContent)
    return content.text
