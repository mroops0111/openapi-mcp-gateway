import os
import re


def resolve_env_var(value: str | None) -> str | None:
    """Substitute ``${VAR}`` or ``${VAR:-default}`` when ``value`` is a lone reference.

    Any other value passes through unchanged.
    Returns ``None`` if the variable is unset and no default is given.
    """
    if value is None:
        return None
    match = re.fullmatch(r'\$\{(\w+)(?::-(.*))?\}', value)
    if not match:
        return value
    env_value = os.environ.get(match.group(1))
    if env_value is not None:
        return env_value
    if match.group(2) is not None:
        return match.group(2)
    return None
