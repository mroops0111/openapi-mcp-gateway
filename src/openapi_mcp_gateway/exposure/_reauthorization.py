import typing

import httpx
from mcp import UrlElicitationRequiredError
from mcp.server.mcpserver import Context
from mcp.types import ElicitRequest, ElicitRequestURLParams, ElicitResult, InputRequiredResult

from ..auth.upstream.reauthorization import UpstreamReauthorizer
from ..auth.upstream.rejection import INVALID_TOKEN, read_upstream_rejection


# The key of the one input request a call asks the client for, the user's new upstream sign-in.
SIGN_IN_INPUT_REQUEST = 'upstream_sign_in'

# How long a retried call waits for the user to finish signing in, in seconds, before it asks again.
SIGN_IN_WAIT_SECONDS = 120.0

# What a call is told about the sign-in it asked for, from the client's answer on the retry.
SignInAnswer = typing.Literal['accept', 'decline', 'cancel']

# The first protocol revision that carries input requests in an ``InputRequiredResult``.
# Revision names are dates, so they compare as strings.
_MULTI_ROUND_TRIP_REVISION = '2026-07-28'


async def send_with_renewal(
    attempt: typing.Callable[[], typing.Awaitable[typing.Any]], reauthorizer: UpstreamReauthorizer | None
) -> typing.Any:
    """Run ``attempt``, and once more if the upstream said the token expired and ``reauthorizer`` renewed it.

    The renewal needs no user, so it runs before any new sign-in is offered.
    Every other upstream error is raised unchanged.
    """
    try:
        return await attempt()
    except httpx.HTTPStatusError as exception:
        if reauthorizer is None:
            raise
        rejection = read_upstream_rejection(exception.response)
        if rejection is None or rejection.error != INVALID_TOKEN or not await reauthorizer.renew():
            raise
    return await attempt()


def read_sign_in_answer(context: Context) -> SignInAnswer | None:
    """How the user answered the sign-in an earlier round of this call asked for, or ``None`` when none did."""
    responses = context.input_responses
    response = responses.get(SIGN_IN_INPUT_REQUEST) if responses else None
    return response.action if isinstance(response, ElicitResult) else None


async def offer_upstream_sign_in(
    context: Context, reauthorizer: UpstreamReauthorizer, response: httpx.Response
) -> InputRequiredResult | None:
    """Ask the client to send the user to sign in to the upstream again, when that cures ``response``.

    A 2026-07-28 client gets an ``InputRequiredResult`` carrying a URL-mode elicitation,
    and retries the call once the user has opened it.
    A 2025-11-25 client gets ``URLElicitationRequiredError`` instead, which is deprecated with that revision,
    and retries by hand, since the gateway sends no ``notifications/elicitation/complete``.
    Returns ``None`` when the client cannot open a URL, the refusal is not one a sign-in cures,
    or no sign-in can run for the call's token, so the caller reports the upstream error as it is.
    """
    if not _accepts_url_elicitation(context):
        return None
    rejection = read_upstream_rejection(response)
    if rejection is None:
        return None
    reauthorization = await reauthorizer.start(rejection)
    if reauthorization is None:
        return None
    params = ElicitRequestURLParams(message=reauthorization.message, url=reauthorization.url)
    if not _speaks_multi_round_trip(context):
        raise UrlElicitationRequiredError(
            [params.model_copy(update={'elicitation_id': reauthorization.reauthorization_id})]
        )
    return InputRequiredResult(input_requests={SIGN_IN_INPUT_REQUEST: ElicitRequest(params=params)})


def _accepts_url_elicitation(context: Context) -> bool:
    capabilities = context.client_capabilities
    return (
        capabilities is not None and capabilities.elicitation is not None and capabilities.elicitation.url is not None
    )


def _speaks_multi_round_trip(context: Context) -> bool:
    version = context.protocol_version
    return version is not None and version >= _MULTI_ROUND_TRIP_REVISION
