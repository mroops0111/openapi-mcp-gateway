import typing

from mcp.server.mcpserver import Context


def stub_context(headers: dict[str, str] | None = None) -> Context:
    """A ``Context`` that reports progress nowhere, whose request carries ``headers``."""
    return typing.cast(Context, _StubContext(headers or {}))


class _StubContext:
    """What a generated tool or resource reads from its MCP context, and nothing more."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.request_context = _StubRequestContext(headers)
        # A first call from a client that declared nothing, so no new upstream sign-in is ever offered.
        self.input_responses = None
        self.protocol_version = None
        self.client_capabilities = None

    async def report_progress(self, *_args: typing.Any, **_kwargs: typing.Any) -> None:
        return None


class _StubRequestContext:
    """The request context of an MCP call, carrying a Starlette-like ``request.headers``."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.request = type('_StubRequest', (), {'headers': headers})()
