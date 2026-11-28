import typing
from unittest.mock import AsyncMock, MagicMock

from mcp.server.mcpserver import Context

from openapi_mcp_gateway.exposure import CallProgress


async def test_progress_only_grows_and_ends_at_its_total():
    """The MCP spec requires each progress value to exceed the last."""
    context = MagicMock(report_progress=AsyncMock())
    progress = CallProgress(typing.cast(Context, context))

    await progress.report('Waiting')
    await progress.report('Sending')
    await progress.finish('Done')

    sent = [call.args[:2] for call in context.report_progress.await_args_list]
    assert sent == [(1, None), (2, None), (3, 3)]
