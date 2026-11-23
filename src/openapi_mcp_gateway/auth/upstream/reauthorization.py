import abc
import dataclasses

from .rejection import UpstreamRejection


@dataclasses.dataclass(frozen=True)
class UpstreamReauthorization:
    """A new upstream sign-in waiting for the user, started for the token of the call being handled.

    ``url`` is the gateway page that starts it, which the user opens in the browser they signed in with.
    ``message`` tells the user why, as the client shows it before opening ``url``.
    ``reauthorization_id`` names it in ``url``.
    """

    reauthorization_id: str
    url: str
    message: str


class UpstreamReauthorizer(abc.ABC):
    """Cures an upstream refusal of the caller's own upstream token, for a flow that signs each user in upstream.

    Only a flow where the gateway holds a per-user upstream token can renew it or send the user back to sign in,
    so the capability sits apart from ``UpstreamAuthResolver``.
    Every method acts on the token of the call being handled.
    """

    @abc.abstractmethod
    async def renew(self) -> bool:
        """Refresh the upstream token behind the call, without the user, and say whether a new one is in place."""

    @abc.abstractmethod
    async def start(self, rejection: UpstreamRejection) -> UpstreamReauthorization | None:
        """Start a new upstream sign-in that cures ``rejection``, or return ``None`` when none can.

        A sign-in cannot cure a refusal for scopes beyond what the server may ask the upstream for,
        nor run for a token whose sign-in no browser is bound to.
        """

    @abc.abstractmethod
    async def wait_for_completion(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for the sign-in started for the call's token, and say whether it finished."""


class UpstreamNoReauthorizer(UpstreamReauthorizer):
    """For a flow with no per-user upstream token, which leaves every upstream refusal as it is."""

    async def renew(self) -> bool:
        return False

    async def start(self, rejection: UpstreamRejection) -> UpstreamReauthorization | None:
        return None

    async def wait_for_completion(self, timeout: float) -> bool:
        return False
