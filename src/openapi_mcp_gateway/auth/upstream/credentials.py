import collections.abc
import dataclasses
import typing

from ...openapi import OperationInfo


# Where an OpenAPI ``apiKey`` scheme puts its key, as its ``in`` names it.
CredentialLocation = typing.Literal['header', 'query', 'cookie']

# The header a bearer travels in, and what precedes the token there.
AUTHORIZATION_HEADER = 'Authorization'
BEARER_PREFIX = 'Bearer '

# The scheme types a flow's or a caller's bearer token meets, since each travels as ``Authorization: Bearer``.
_BEARER_TOKEN_SCHEME_TYPES = frozenset({'oauth2', 'openIdConnect'})
_API_KEY_LOCATIONS: frozenset[str] = frozenset(typing.get_args(CredentialLocation))

# How many operations an unmet-security error names before it only counts the rest.
_NAMED_OPERATIONS = 5


@dataclasses.dataclass(frozen=True)
class UpstreamRequestCredentials:
    """The static credentials one upstream request carries, by where each travels.

    The values are secrets, so the ``repr`` leaves them out.
    """

    headers: collections.abc.Mapping[str, str] = dataclasses.field(default_factory=dict, repr=False)
    query: collections.abc.Mapping[str, str] = dataclasses.field(default_factory=dict, repr=False)
    cookies: collections.abc.Mapping[str, str] = dataclasses.field(default_factory=dict, repr=False)

    def merged_cookie_header(self, existing: str | None) -> str | None:
        """The ``Cookie`` header with these cookies in it, replacing any of the same name ``existing`` sends."""
        if not self.cookies:
            return existing
        kept = [
            pair.strip()
            for pair in (existing or '').split(';')
            if pair.strip() and pair.partition('=')[0].strip() not in self.cookies
        ]
        return '; '.join([*kept, *(f'{name}={value}' for name, value in self.cookies.items())])


@dataclasses.dataclass(frozen=True)
class UpstreamCredentialPlacement:
    """Where one security scheme's credential travels, and what precedes the value there."""

    location: CredentialLocation
    name: str
    prefix: str = ''

    @classmethod
    def from_scheme(cls, scheme: collections.abc.Mapping[str, typing.Any]) -> typing.Self | None:
        """The placement ``scheme`` declares, or ``None`` for a scheme a static value cannot serve.

        An ``apiKey`` scheme names its location and key, and an HTTP ``bearer`` one travels in ``Authorization``.
        """
        if scheme.get('type') == 'apiKey' and scheme.get('in') in _API_KEY_LOCATIONS and scheme.get('name'):
            return cls(typing.cast(CredentialLocation, scheme['in']), str(scheme['name']))
        if scheme.get('type') == 'http' and str(scheme.get('scheme', '')).lower() == 'bearer':
            return cls('header', AUTHORIZATION_HEADER, BEARER_PREFIX)
        return None

    def place(self, value: str) -> UpstreamRequestCredentials:
        """The request credentials that carry ``value`` here."""
        placed = {self.name: f'{self.prefix}{value}'}
        if self.location == 'query':
            return UpstreamRequestCredentials(query=placed)
        if self.location == 'cookie':
            return UpstreamRequestCredentials(cookies=placed)
        return UpstreamRequestCredentials(headers=placed)


@dataclasses.dataclass(frozen=True)
class UpstreamCredentialPlan:
    """The static credentials each operation carries, settled once at startup.

    An operation missing from ``operations`` carries none.
    An operation in ``served_by_credentials`` settled on an alternative its static credentials meet alone,
    so its calls carry no token from the flow.
    """

    operations: collections.abc.Mapping[str, UpstreamRequestCredentials] = dataclasses.field(
        default_factory=dict, repr=False
    )
    served_by_credentials: frozenset[str] = frozenset()

    def credentials_for(self, operation_id: str) -> UpstreamRequestCredentials:
        """What a call to ``operation_id`` carries."""
        return self.operations.get(operation_id, NO_REQUEST_CREDENTIALS)

    def carries_flow_credential(self, operation_id: str) -> bool:
        """Whether a call to ``operation_id`` carries the flow's credential beside its static ones."""
        return operation_id not in self.served_by_credentials


# What an operation carries when no static credential serves it.
# It follows the class it names, since Python builds it at import time.
NO_REQUEST_CREDENTIALS = UpstreamRequestCredentials()


def schemes_met_without_credentials(
    security_schemes: collections.abc.Mapping[str, typing.Any],
    carries_bearer: bool,
    forwarded_headers: collections.abc.Collection[str],
) -> frozenset[str]:
    """The schemes a request meets without a static credential, through a flow's token or a forwarded header.

    ``carries_bearer`` says a flow supplies ``Authorization: Bearer``,
    which meets an OAuth, OpenID Connect or HTTP bearer scheme.
    A forwarded ``Authorization`` carries whatever the caller sent, so it meets every HTTP scheme besides those,
    and any other forwarded header meets the ``apiKey`` scheme that names it.
    """
    forwarded = {header.lower() for header in forwarded_headers}
    forwards_authorization = AUTHORIZATION_HEADER.lower() in forwarded
    met: set[str] = set()
    for name, scheme in security_schemes.items():
        if not isinstance(scheme, dict):
            continue
        scheme_type = scheme.get('type')
        placement = UpstreamCredentialPlacement.from_scheme(scheme)
        bearer = scheme_type in _BEARER_TOKEN_SCHEME_TYPES or (
            placement is not None and placement.prefix == BEARER_PREFIX
        )
        if (
            (carries_bearer and bearer)
            or (forwards_authorization and (bearer or scheme_type == 'http'))
            or (placement is not None and placement.location == 'header' and placement.name.lower() in forwarded)
        ):
            met.add(name)
    return frozenset(met)


def met_alternatives(
    security: collections.abc.Sequence[collections.abc.Mapping[str, typing.Any]],
    available: collections.abc.Set[str],
) -> list[collections.abc.Mapping[str, typing.Any]]:
    """The alternatives of ``security`` whose schemes are all ``available``, in spec order with an empty one last.

    An empty alternative makes the credential optional, so one another alternative names is sent when configured.
    """
    met = [alternative for alternative in security if alternative.keys() <= available]
    return [
        *(alternative for alternative in met if alternative),
        *(alternative for alternative in met if not alternative),
    ]


def plan_upstream_credentials(
    server_name: str,
    operations: collections.abc.Sequence[OperationInfo],
    security_schemes: collections.abc.Mapping[str, typing.Any],
    credentials: collections.abc.Mapping[str, str | None],
    met_without_credentials: frozenset[str],
) -> UpstreamCredentialPlan:
    """Settle which static credentials each of ``operations`` carries, refusing a config that cannot serve them.

    An operation's ``security`` lists alternatives, and each names the schemes it needs together.
    The first alternative in spec order whose schemes are all met is the one used, the empty one only when no other is,
    and its configured schemes are the credentials the operation carries.
    Startup is refused for an operation no alternative is met for, and for a credential no operation uses.
    """
    placements = _resolve_placements(server_name, security_schemes, credentials)
    values = _resolve_values(server_name, credentials)
    available = frozenset(credentials) | met_without_credentials
    planned: dict[str, UpstreamRequestCredentials] = {}
    unmet: list[OperationInfo] = []
    served_by_credentials: set[str] = set()
    used: set[str] = set()
    for operation in operations:
        if not operation.security:
            continue
        met = met_alternatives(operation.security, available)
        if not met:
            unmet.append(operation)
            continue
        requirement = met[0]
        carried = [scheme for scheme in requirement if scheme in credentials]
        used.update(carried)
        if carried:
            planned[operation.operation_id] = _combine(placements[scheme].place(values[scheme]) for scheme in carried)
        if requirement and len(carried) == len(requirement):
            served_by_credentials.add(operation.operation_id)
    if unmet:
        raise ValueError(_describe_unmet(server_name, unmet))
    unused = sorted(set(credentials) - used)
    if unused:
        raise ValueError(
            f'Server "{server_name}": auth.upstream.credentials {", ".join(unused)} serve no exposed operation, '
            'so they would have no effect here. Remove them, or check the operations security names them.'
        )
    return UpstreamCredentialPlan(planned, frozenset(served_by_credentials))


def _resolve_placements(
    server_name: str,
    security_schemes: collections.abc.Mapping[str, typing.Any],
    credentials: collections.abc.Mapping[str, str | None],
) -> dict[str, UpstreamCredentialPlacement]:
    """Where each configured credential travels, refusing a scheme that is undeclared or no static value serves."""
    undeclared = sorted(set(credentials) - set(security_schemes))
    if undeclared:
        raise ValueError(
            f'Server "{server_name}": auth.upstream.credentials names schemes the spec does not declare '
            f'{undeclared!r}. Use the names under components.securitySchemes, or declare them in security_schemes.'
        )
    placements: dict[str, UpstreamCredentialPlacement] = {}
    for scheme_name in credentials:
        scheme = security_schemes[scheme_name]
        placement = UpstreamCredentialPlacement.from_scheme(scheme if isinstance(scheme, dict) else {})
        if placement is None:
            raise ValueError(
                f'Server "{server_name}": auth.upstream.credentials.{scheme_name} serves a scheme no static value can, '
                'since only an apiKey scheme in a header, the query or a cookie, or an HTTP bearer scheme, takes one. '
                'An OAuth scheme takes a token from auth.upstream.type oauth2.'
            )
        placements[scheme_name] = placement
    return placements


def _resolve_values(server_name: str, credentials: collections.abc.Mapping[str, str | None]) -> dict[str, str]:
    """Each credential's value, refusing one that resolves to nothing, since it would be sent empty."""
    empty = sorted(name for name, value in credentials.items() if not value)
    if empty:
        raise ValueError(
            f'Server "{server_name}": auth.upstream.credentials {", ".join(empty)} resolve to nothing. '
            'Set the environment variables they reference.'
        )
    return {name: value for name, value in credentials.items() if value}


def _combine(placed: collections.abc.Iterable[UpstreamRequestCredentials]) -> UpstreamRequestCredentials:
    headers: dict[str, str] = {}
    query: dict[str, str] = {}
    cookies: dict[str, str] = {}
    for credentials in placed:
        headers.update(credentials.headers)
        query.update(credentials.query)
        cookies.update(credentials.cookies)
    return UpstreamRequestCredentials(headers, query, cookies)


def _describe_unmet(server_name: str, unmet: collections.abc.Sequence[OperationInfo]) -> str:
    """Name the operations no credential serves, with the alternatives each would accept."""
    described = [
        f'{operation.operation_id} (needs {" or ".join(" + ".join(alternative) for alternative in operation.security)})'
        for operation in unmet[:_NAMED_OPERATIONS]
    ]
    rest = len(unmet) - len(described)
    if rest:
        described.append(f'and {rest} more')
    return (
        f'Server "{server_name}": no configured credential meets the security of {", ".join(described)}. '
        'Add the credentials to auth.upstream.credentials, '
        'pick another alternative under operations.<id>.security, or leave the operations out with policy.deny.'
    )
