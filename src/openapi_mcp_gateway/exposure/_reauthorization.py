import abc
import typing

import httpx
from mcp.server.mcpserver import Context
from mcp.types import ElicitRequest, ElicitRequestURLParams, ElicitResult, InputRequiredResult

from ..auth.upstream.reauthorization import UpstreamReauthorizer
from ..auth.upstream.rejection import INVALID_TOKEN, read_upstream_rejection


# The key of the one input request a call asks the client for, the user's new upstream sign-in.
SIGN_IN_INPUT_REQUEST = 'upstream_sign_in'

# How long a retried call waits for the user to finish signing in, in seconds, before it asks again.
SIGN_IN_WAIT_SECONDS = 120.0

# The first protocol revision that carries input requests in an ``InputRequiredResult``.
# Revision names are dates, so they compare as strings.
_MULTI_ROUND_TRIP_REVISION = '2026-07-28'


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
    async def await_answer(self, context: Context, reauthorizer: UpstreamReauthorizer) -> None:
        """Before the request is sent, wait for the sign-in an earlier round offered, if the user took it up."""

    @abc.abstractmethod
    async def offer(
        self, context: Context, reauthorizer: UpstreamReauthorizer, response: httpx.Response
    ) -> InputRequiredResult | None:
        """Ask for a new sign-in that cures ``response``, or return ``None`` to report the upstream error as it is."""


class UpstreamNoSignInOffer(UpstreamSignInOffer):
    """For a request that finishes in one round, such as a resource read, which has no way to ask."""

    async def await_answer(self, context: Context, reauthorizer: UpstreamReauthorizer) -> None:
        return None

    async def offer(
        self, context: Context, reauthorizer: UpstreamReauthorizer, response: httpx.Response
    ) -> InputRequiredResult | None:
        return None


# The offer of a call that never asks, the default for any request but a tool call.
NO_SIGN_IN_OFFER = UpstreamNoSignInOffer()


class UpstreamUrlElicitationSignInOffer(UpstreamSignInOffer):
    """For a tool call, which asks through a URL-mode elicitation in an ``InputRequiredResult``.

    Only a 2026-07-28 client that declares URL elicitation is asked.
    A user who turned the sign-in down hears the upstream's answer rather than being asked again.
    """

    async def await_answer(self, context: Context, reauthorizer: UpstreamReauthorizer) -> None:
        if _read_sign_in_answer(context) != 'accept':
            return
        await context.report_progress(0, 1, 'Waiting for the new upstream sign-in ...')
        await reauthorizer.wait_for_completion(SIGN_IN_WAIT_SECONDS)

    async def offer(
        self, context: Context, reauthorizer: UpstreamReauthorizer, response: httpx.Response
    ) -> InputRequiredResult | None:
        rejection = read_upstream_rejection(response)
        if rejection is None or _read_sign_in_answer(context) in ('decline', 'cancel'):
            return None
        if not _speaks_multi_round_trip(context) or not _accepts_url_elicitation(context):
            return None
        reauthorization = await reauthorizer.start(rejection)
        if reauthorization is None:
            return None
        params = ElicitRequestURLParams(message=reauthorization.message, url=reauthorization.url)
        return InputRequiredResult(input_requests={SIGN_IN_INPUT_REQUEST: ElicitRequest(params=params)})


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
    return version is not None and version >= _MULTI_ROUND_TRIP_REVISION
