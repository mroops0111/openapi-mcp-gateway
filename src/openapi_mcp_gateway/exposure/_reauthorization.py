import abc
import typing

import anyio
import httpx
from mcp.server.mcpserver import Context
from mcp.types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestURLParams,
    ElicitResult,
    InputRequiredResult,
    TextContent,
)
from mcp_types.version import is_version_at_least

from ..auth.upstream.reauthorization import UpstreamReauthorizer
from ..auth.upstream.rejection import INVALID_TOKEN, read_upstream_rejection
from ._progress import CallProgress


# The key of the one input request a call asks the client for, the user's new upstream sign-in.
SIGN_IN_INPUT_REQUEST = 'upstream_sign_in'

# How long a retried call waits for the user to finish signing in, in seconds.
# It stays under the 60 seconds after which common clients give up on a request.
SIGN_IN_WAIT_SECONDS = 50.0

# The first protocol revision that carries input requests in an ``InputRequiredResult``.
_MULTI_ROUND_TRIP_REVISION = '2026-07-28'

# How often a waiting call tells the client it is still waiting, in seconds.
_SIGN_IN_PROGRESS_INTERVAL = 5.0


async def send_with_renewal(
    attempt: typing.Callable[[], typing.Awaitable[typing.Any]], reauthorizer: UpstreamReauthorizer
) -> typing.Any:
    """Run ``attempt``, and once more if the upstream said the token expired and ``reauthorizer`` renewed it.

    The renewal needs no user, so it runs before any new sign-in is offered.
    Every other upstream error is raised unchanged.
    """
    try:
        return await attempt()
    except httpx.HTTPStatusError as exception:
        rejection = read_upstream_rejection(exception.response)
        if rejection is None or rejection.error != INVALID_TOKEN or not await reauthorizer.renew():
            raise
    return await attempt()


class UpstreamSignInOffer(abc.ABC):
    """Whether a call may ask the client to send the user to sign in to the upstream again, and how.

    A call that can ask spans two rounds.
    The first ends in the offer, and the client's retry carries the user's answer to it.
    """

    @abc.abstractmethod
    async def await_answer(
        self, context: Context, reauthorizer: UpstreamReauthorizer, progress: CallProgress
    ) -> CallToolResult | None:
        """Settle the user's answer to the sign-in an earlier round offered, before the request is sent.

        Returns the result that ends the call when the request is not worth sending, or ``None`` to send it.
        """

    @abc.abstractmethod
    async def offer(
        self, context: Context, reauthorizer: UpstreamReauthorizer, response: httpx.Response
    ) -> InputRequiredResult | None:
        """Ask for a new sign-in that cures ``response``, or return ``None`` to report the upstream error as it is."""


class UpstreamNoSignInOffer(UpstreamSignInOffer):
    """For a request that finishes in one round, such as a resource read, which has no way to ask."""

    async def await_answer(
        self, context: Context, reauthorizer: UpstreamReauthorizer, progress: CallProgress
    ) -> CallToolResult | None:
        return None

    async def offer(
        self, context: Context, reauthorizer: UpstreamReauthorizer, response: httpx.Response
    ) -> InputRequiredResult | None:
        return None


# The offer of a call that never asks, the default for any request but a tool call.
NO_SIGN_IN_OFFER = UpstreamNoSignInOffer()


class UpstreamUrlElicitationSignInOffer(UpstreamSignInOffer):
    """For a tool call, which asks through a URL-mode elicitation in an ``InputRequiredResult``.

    Only a 2026-07-28 client that declares URL elicitation is asked, and only once per call.
    A retry the upstream refuses again, whatever the user answered, hears the upstream's error,
    so a sign-in refused at the upstream or one granting too little never loops.
    """

    async def await_answer(
        self, context: Context, reauthorizer: UpstreamReauthorizer, progress: CallProgress
    ) -> CallToolResult | None:
        """Wait for a sign-in the user took up, and end the call unsent when the user turned it down.

        A sign-in still unfinished after ``SIGN_IN_WAIT_SECONDS`` ends the call too.
        Its page stays open, so once the user finishes it a new call goes through.
        """
        answer = _read_sign_in_answer(context)
        if answer is None:
            return None
        if answer != 'accept':
            return _unsent('The user turned down signing in to the upstream again, so the request was not sent.')
        async with anyio.create_task_group() as group:
            group.start_soon(_report_waiting, progress)
            finished = await reauthorizer.wait_for_completion(SIGN_IN_WAIT_SECONDS)
            group.cancel_scope.cancel()
        if finished:
            return None
        return _unsent(
            'The new upstream sign-in was not finished, so the request was not sent. '
            'Call this tool again once the user has signed in.'
        )

    async def offer(
        self, context: Context, reauthorizer: UpstreamReauthorizer, response: httpx.Response
    ) -> InputRequiredResult | None:
        rejection = read_upstream_rejection(response)
        if rejection is None or _read_sign_in_answer(context) is not None:
            return None
        if not _speaks_multi_round_trip(context) or not _accepts_url_elicitation(context):
            return None
        reauthorization = await reauthorizer.start(rejection)
        if reauthorization is None:
            return None
        params = ElicitRequestURLParams(message=reauthorization.message, url=reauthorization.url)
        return InputRequiredResult(input_requests={SIGN_IN_INPUT_REQUEST: ElicitRequest(params=params)})


# The offer of a tool call, which holds no state, so every tool shares it.
URL_ELICITATION_SIGN_IN_OFFER = UpstreamUrlElicitationSignInOffer()


async def _report_waiting(progress: CallProgress) -> None:
    while True:
        await progress.report('Waiting for the new upstream sign-in ...')
        await anyio.sleep(_SIGN_IN_PROGRESS_INTERVAL)


def _unsent(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type='text', text=message)], is_error=True)


def _read_sign_in_answer(context: Context) -> str | None:
    """How the user answered the sign-in an earlier round of this call offered, or ``None`` when none did."""
    responses = context.input_responses
    response = responses.get(SIGN_IN_INPUT_REQUEST) if responses else None
    return response.action if isinstance(response, ElicitResult) else None


def _accepts_url_elicitation(context: Context) -> bool:
    capabilities = context.client_capabilities
    return (
        capabilities is not None and capabilities.elicitation is not None and capabilities.elicitation.url is not None
    )


def _speaks_multi_round_trip(context: Context) -> bool:
    version = context.protocol_version
    return version is not None and is_version_at_least(version, _MULTI_ROUND_TRIP_REVISION)
