from mcp.server.mcpserver import Context


class CallProgress:
    """The progress notifications of one call, whose value only ever grows, as the MCP spec requires.

    Each report moves one step on, and ``finish`` states the last step as the total.
    """

    def __init__(self, context: Context) -> None:
        self._context = context
        self._step = 0

    async def report(self, message: str) -> None:
        self._step += 1
        await self._context.report_progress(self._step, None, message)

    async def finish(self, message: str) -> None:
        self._step += 1
        await self._context.report_progress(self._step, self._step, message)
