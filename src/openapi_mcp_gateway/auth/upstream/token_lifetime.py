import math
import typing


# How long before a token expires its source treats it as stale, and the lifetime assumed when a response states none.
DEFAULT_REFRESH_SKEW_SECONDS = 30.0
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600.0


def parse_expires_in(value: typing.Any) -> int | None:
    """``expires_in`` from a token response as whole seconds, or ``None`` when it states no usable lifetime.

    RFC 6749 §5.1 makes it an optional positive number.
    Some servers send it as a string, and anything missing, null, non-positive or non-finite reads as unstated.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return int(seconds) if math.isfinite(seconds) and seconds > 0 else None
