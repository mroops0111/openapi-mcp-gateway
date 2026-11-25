import abc


class UpstreamTokenSource(abc.ABC):
    """Where the upstream bearer for the call being handled comes from.

    ``get_token`` runs on every tool call, so an implementation that fetches over the network caches what it gets,
    refreshes it before expiry, and de-dups concurrent refreshes.
    """

    @abc.abstractmethod
    async def get_token(self) -> str | None:
        """Return a currently-valid bearer token, or ``None`` when this call has none."""

    async def aclose(self) -> None:
        """Release resources held by the token source. Default is a no-op."""
        return None
