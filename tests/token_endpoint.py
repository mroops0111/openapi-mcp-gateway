import typing
import urllib.parse

import httpx


class TokenEndpoint(httpx.MockTransport):
    """An OAuth token endpoint answering in turn from ``answers``, each a response or an async function of the request.

    The last answer repeats once the others are spent.
    It records each request's form, and counts how often a client using it was closed.
    """

    def __init__(self, *answers: httpx.Response | typing.Callable[[httpx.Request], typing.Awaitable[httpx.Response]]):
        self.forms: list[dict[str, str]] = []
        self.closed = 0
        remaining = list(answers)

        async def answer(request: httpx.Request) -> httpx.Response:
            self.forms.append(dict(urllib.parse.parse_qsl(request.content.decode())))
            reply = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            if isinstance(reply, httpx.Response):
                return reply
            return await reply(request)

        super().__init__(answer)

    async def aclose(self) -> None:
        self.closed += 1
