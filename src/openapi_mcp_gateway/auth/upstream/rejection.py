import dataclasses
import re
import typing

import httpx


# The RFC 6750 §3.1 error codes that a new upstream sign-in can cure.
INVALID_TOKEN = 'invalid_token'  # noqa: S105
INSUFFICIENT_SCOPE = 'insufficient_scope'

# The status each curable error code arrives with, per RFC 6750 §3.1.
_CURABLE_STATUS = {INVALID_TOKEN: 401, INSUFFICIENT_SCOPE: 403}

# One ``name=value`` parameter of a challenge, the value quoted or a bare token, per RFC 9110 §11.2.
_CHALLENGE_PARAMETER = re.compile(r'([!#$%&\'*+\-.^_`|~0-9A-Za-z]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')
_QUOTED_ESCAPE = re.compile(r'\\(.)')


@dataclasses.dataclass(frozen=True)
class UpstreamRejection:
    """An upstream refusal of the user's token that a new upstream sign-in can cure, read from its Bearer challenge.

    ``error`` is ``invalid_token`` for an expired or revoked token, and ``insufficient_scope`` for a narrow one.
    ``scopes`` are what the challenge says the request needs, empty when it names none.
    """

    error: typing.Literal['invalid_token', 'insufficient_scope']
    scopes: tuple[str, ...] = ()


def read_upstream_rejection(response: httpx.Response) -> UpstreamRejection | None:
    """The curable refusal ``response`` carries, or ``None`` for any other response.

    Only a standard RFC 6750 challenge counts,
    a ``401`` with ``error="invalid_token"`` or a ``403`` with ``error="insufficient_scope"``.
    A refusal without one, or with another error, says nothing a new sign-in is known to cure.
    """
    for challenge in response.headers.get_list('www-authenticate'):
        parameters = _bearer_parameters(challenge)
        if parameters is None:
            continue
        error = parameters.get('error')
        if error not in _CURABLE_STATUS or _CURABLE_STATUS[error] != response.status_code:
            continue
        scopes = tuple(parameters.get('scope', '').split())
        return UpstreamRejection(typing.cast(typing.Literal['invalid_token', 'insufficient_scope'], error), scopes)
    return None


def _bearer_parameters(challenge: str) -> dict[str, str] | None:
    """The parameters of ``challenge`` when it is a Bearer challenge, or ``None`` for another scheme.

    A header holding several challenges is read from the Bearer one to the next scheme.
    """
    match = re.search(r'(?:^|,)\s*Bearer(?=\s|$|,)', challenge, re.IGNORECASE)
    if match is None:
        return None
    parameters: dict[str, str] = {}
    rest = challenge[match.end() :]
    position = 0
    while (parameter := _CHALLENGE_PARAMETER.search(rest, position)) is not None:
        between = rest[position : parameter.start()]
        # A bare word between parameters starts the next challenge's scheme.
        if between.strip(' ,'):
            break
        name, quoted, bare = parameter.groups()
        parameters.setdefault(name.lower(), _QUOTED_ESCAPE.sub(r'\1', quoted) if quoted is not None else bare)
        position = parameter.end()
    return parameters
