import pathlib
import re
import typing
import urllib.parse

import pydantic
import yaml

from .env import resolve_env_var
from .openapi import McpIntegration


# Every model below sets extra='forbid'.
# An unknown key is a mistake rather than a preference, and silently dropping one is worse than refusing it.
# policy.annotated_only decides which operations a caller may invoke,
# so a typo there once widened a curated surface to the whole spec with no signal at all.


# One RFC 6749 §3.3 scope-token, which excludes spaces, double quotes and backslashes.
SCOPE_TOKEN = re.compile(r'[\x21\x23-\x5B\x5D-\x7E]+')

# A host name as ``trusted_hosts`` lists it, lowercase ASCII labels with no scheme, port, wildcard or trailing dot.
_HOST_NAME = re.compile(r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*')

# Keys that left the top of ``auth``, and where each lives now.
# A config written for the old layout is refused with the new place named, rather than with a bare unknown key.
_MOVED_AUTH_KEYS = {
    'type': 'auth.upstream.type',
    'token': 'auth.upstream.token',
    'api_key_header': 'auth.upstream.api_key_header',
    'flow': 'auth.upstream.flow',
    'issuer': 'auth.mcp.issuer',
    'required_scopes': 'auth.mcp.required_scopes',
    'operation_scopes': 'auth.mcp.operation_scopes',
    'clients': 'auth.mcp.clients',
    'mcp_access_token_ttl': 'auth.mcp.access_token_ttl',
    'mcp_refresh_token_ttl': 'auth.mcp.refresh_token_ttl',
}


# The resolver moved to the leaf ``env`` module so exposure/ can reuse it without importing settings.
# Kept as a private alias here so AuthConfig's existing call sites need no change.
_resolve_env_var = resolve_env_var


class UpstreamAuthConfig(pydantic.BaseModel):
    """How the gateway authenticates to the upstream API, the outbound half of ``auth``.

    ``type`` says where the credential comes from.
    ``token`` and ``api_key_header`` serve the static types, and the rest serve ``type: oauth2``.
    """

    model_config = pydantic.ConfigDict(coerce_numbers_to_str=True, extra='forbid')

    type: typing.Literal['bearer', 'api_key', 'oauth2', 'passthrough', 'none'] = 'none'
    token: str | None = None
    api_key_header: str = 'X-API-Key'

    flow: typing.Literal['authorization_code', 'client_credentials', 'token_exchange'] | None = None

    client_id: str | None = None
    client_secret: str | None = None
    authorization_url: str | None = None
    token_url: str | None = None
    scopes: list[str] = pydantic.Field(default_factory=list)

    # The upstream scopes every sign-in or token exchange asks for, whatever the operations it opens need,
    # such as ``offline_access`` for a refresh token or ``openid`` for the user's identity.
    # ``scopes`` is the most a sign-in or exchange asks for beyond them.
    # Only ``authorization_code`` and ``token_exchange`` obtain a token per user, so no other flow reads them.
    required_scopes: list[str] = pydantic.Field(default_factory=list)

    # The upstream authorization server's issuer identifier, exactly as its metadata publishes it.
    # It is what an RFC 9207 ``iss`` on the authorization response must equal.
    # Unset, a present ``iss`` cannot be checked, so the mix-up defence is off.
    issuer: str | None = None

    # Names the API the token is for, when that API and its authorization server are different parties.
    # Without it the authorization server mints for its own default audience, which the API refuses.
    # Servers disagree on the spelling, so both are offered and only what is set is sent.
    # ``resource`` is the RFC 8707 parameter, ``audience`` is the spelling Auth0 uses.
    # A server that does not recognise one ignores it silently rather than refusing,
    # so setting both is the portable choice.
    resource: str | None = None
    audience: str | None = None

    # Whether an operation also needs the upstream scopes its OpenAPI ``security`` declares,
    # checked against the scopes the upstream actually granted the user.
    # ``scope_implies`` maps an upstream scope to the narrower ones it covers, such as GitHub's ``repo``.
    enforce_scopes: bool = False
    scope_implies: dict[str, list[str]] = pydantic.Field(default_factory=dict)

    @pydantic.field_validator('scope_implies')
    @classmethod
    def _validate_implied_scope_names(cls, scope_implies: dict[str, list[str]]) -> dict[str, list[str]]:
        implied = [scope for scopes in scope_implies.values() for scope in scopes]
        _refuse_invalid_scope_tokens([*scope_implies, *implied], 'auth.upstream.scope_implies')
        return scope_implies

    def resolve_token(self) -> str | None:
        """Static credential after env-var substitution.

        How it is framed as a header belongs to the auth type handler rather than here,
        since ``Bearer <token>`` is protocol knowledge rather than configuration.
        """
        return _resolve_env_var(self.token)

    def resolve_client_id(self) -> str | None:
        """OAuth client id after env-var substitution."""
        return _resolve_env_var(self.client_id)

    def resolve_client_secret(self) -> str | None:
        """OAuth client secret after env-var substitution."""
        return _resolve_env_var(self.client_secret)

    def resolve_issuer(self) -> str | None:
        """Upstream authorization server issuer after env-var substitution."""
        return _resolve_env_var(self.issuer)

    def resolve_audience_params(self) -> dict[str, str]:
        """Return the audience-naming parameters for authorize and token requests.

        Empty when neither is configured, which is the right shape for an upstream that issues its own tokens.
        """
        params: dict[str, str] = {}
        resource = _resolve_env_var(self.resource)
        audience = _resolve_env_var(self.audience)
        if resource:
            params['resource'] = resource
        if audience:
            params['audience'] = audience
        return params


class ManualScopeConfig(pydantic.BaseModel):
    """One hand-written MCP scope and the operations it opens.

    ``operations`` takes the same patterns as ``policy.allow``.
    ``description`` is what a person approving the scope is shown.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    description: str
    operations: list[str] = pydantic.Field(min_length=1)


class OperationScopesConfig(pydantic.BaseModel):
    """The MCP scopes of one server, and which operations each opens.

    An operation opens to a token carrying any scope that covers it.
    ``auto`` derives scopes from the spec, ``manual`` declares them by hand, and both may cover one operation.
    ``unscoped`` decides an operation no scope covers.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    auto: typing.Literal['none', 'tags'] = 'none'
    unscoped: typing.Literal['deny', 'allow'] = 'deny'
    manual: dict[str, ManualScopeConfig] = pydantic.Field(default_factory=dict)

    @pydantic.field_validator('manual')
    @classmethod
    def _validate_scope_names(cls, manual: dict[str, ManualScopeConfig]) -> dict[str, ManualScopeConfig]:
        _refuse_invalid_scope_tokens(manual, 'auth.mcp.operation_scopes.manual')
        return manual


class ClientScopesConfig(pydantic.BaseModel):
    """The scopes a client may hold, shared by ``auth.mcp.clients`` and ``auth.mcp.other_clients``.

    ``scopes`` are every scope the client may be granted, and each must be one ``auth.mcp.operation_scopes`` defines.
    ``default_scopes`` are what a client asking for no scope receives, a subset of ``scopes``.
    Left out, they are all of ``scopes``, and otherwise the client steps up for the rest.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    # Where these settings sit in the config, which each error names.
    location: typing.ClassVar[str]

    scopes: list[str] = pydantic.Field(default_factory=list)
    default_scopes: list[str] | None = None

    @pydantic.field_validator('scopes')
    @classmethod
    def _validate_scope_names(cls, scopes: list[str]) -> list[str]:
        _refuse_invalid_scope_tokens(scopes, f'{cls.location}.scopes')
        return scopes

    @pydantic.model_validator(mode='after')
    def _refuse_defaults_beyond_scopes(self) -> typing.Self:
        """Refuse a default the client could never be granted, since ``scopes`` is its maximum."""
        beyond = sorted(set(self.default_scopes or ()) - set(self.scopes))
        if beyond:
            raise ValueError(
                f'{self.location}.default_scopes names {beyond!r}, which are not in its scopes. '
                'Add them to scopes, or remove them from default_scopes.'
            )
        return self

    @property
    def granted_by_default(self) -> list[str]:
        """What the client receives when it asks for no scope."""
        return self.scopes if self.default_scopes is None else self.default_scopes


class ClientConfig(ClientScopesConfig):
    """One pre-registered MCP client, keyed by its ``client_id`` under ``auth.mcp.clients``.

    Without ``client_secret`` it is a public client, held to PKCE alone.
    Without ``redirect_uris`` any loopback redirect is accepted, on any port and path, as OAuth 2.1 requires,
    which is what a CLI client listening on a local port needs.
    ``consent`` shows the gateway's consent page before the upstream sign-in.
    Turning it off trusts the client on the operator's word.
    The user is then never asked which client they are signing in to.

    A key that is a Client ID Metadata Document's URL names a client that publishes its own metadata there.
    It takes neither ``client_secret``, which the CIMD draft forbids, nor ``redirect_uris``, which its document lists.
    """

    location: typing.ClassVar[str] = 'auth.mcp.clients.<client>'

    client_secret: str | None = None
    redirect_uris: list[pydantic.AnyUrl] | None = pydantic.Field(default=None, min_length=1)
    consent: bool = True

    def find_settings_without_sign_in(self) -> list[tuple[str, str]]:
        """The fields this client sets, or lacks, that only make sense when a user signs in, each with why."""
        problems = []
        if not self.client_secret:
            problems.append(('client_secret', 'is required, since it is the only proof of who the agent is'))
        for field in ('redirect_uris', 'default_scopes', 'consent'):
            if field in self.model_fields_set:
                problems.append((field, 'has no effect, since no user signs in'))
        return problems

    def resolve_client_secret(self) -> str | None:
        """Client secret after env-var substitution."""
        return _resolve_env_var(self.client_secret)


class OtherClientsConfig(ClientScopesConfig):
    """The Client ID Metadata Document clients ``auth.mcp.clients`` does not list, under ``auth.mcp.other_clients``.

    ``trusted_hosts`` admits only the clients whose documents sit on one of these hosts, matched exactly.
    Left out, it admits any.
    Each is granted scopes as a listed client is, and always passes the consent page.
    """

    location: typing.ClassVar[str] = 'auth.mcp.other_clients'

    trusted_hosts: list[str] | None = pydantic.Field(default=None, min_length=1)

    @pydantic.field_validator('trusted_hosts')
    @classmethod
    def _validate_hosts(cls, hosts: list[str] | None) -> list[str] | None:
        """Lowercase each host, as URL hosts compare without case, and refuse one no URL host could equal."""
        if hosts is None:
            return None
        hosts = [host.lower() for host in hosts]
        invalid = [host for host in hosts if not _HOST_NAME.fullmatch(host)]
        if invalid:
            raise ValueError(
                f'{cls.location}.trusted_hosts names {invalid!r}, which are not host names. '
                'List each as a bare host such as claude.ai, without a scheme, port, wildcard or trailing dot, '
                'and an internationalised one in its xn-- form.'
            )
        return hosts


class McpAuthConfig(pydantic.BaseModel):
    """Who may call this MCP endpoint, the inbound half of ``auth``.

    ``issuer`` serves ``token_exchange``, where another authorization server mints the tokens.
    ``required_scopes`` is what every token must carry.
    It is checked on that server's tokens under ``token_exchange``, and granted on every token the gateway mints.
    The rest serve the gateway's own tokens, which users sign in for under ``authorization_code``,
    and which agents obtain with their own credentials in front of one shared upstream credential.
    """

    model_config = pydantic.ConfigDict(coerce_numbers_to_str=True, extra='forbid')

    # The external authorization server that mints tokens for this MCP endpoint, and what every token must carry.
    issuer: str | None = None
    required_scopes: list[str] = pydantic.Field(default_factory=list)

    # Which operations each scope opens, and the clients the gateway issues tokens to, with the scopes each may hold.
    # ``other_clients`` admits the Client ID Metadata Document clients ``clients`` does not list, and ``false`` none.
    # Left out, it admits any when ``clients`` lists none, and none otherwise, so a listed set stays closed.
    operation_scopes: OperationScopesConfig | None = None
    clients: dict[str, ClientConfig] = pydantic.Field(default_factory=dict)
    other_clients: OtherClientsConfig | typing.Literal[False] | None = None

    # Lifetimes of the MCP tokens the gateway mints, in seconds.
    # The refresh TTL is the practical idle window, since each refresh issues a fresh refresh token,
    # so a client refreshing within it slides forward and never re-authorizes.
    access_token_ttl: int = pydantic.Field(default=3600, gt=0)
    refresh_token_ttl: int = pydantic.Field(default=86400, gt=0)

    @pydantic.model_validator(mode='after')
    def _reject_scope_settings_that_cannot_take_effect(self) -> typing.Self:
        """Refuse client scopes no operation scope could use, and a client list that admits no one."""
        if self.other_clients is False and not self.clients:
            raise ValueError(
                'auth.mcp.other_clients is false and auth.mcp.clients lists no client, so no client could sign in.'
            )
        scoped_clients = sorted(client_id for client_id, client in self.clients.items() if client.scopes)
        if self.operation_scopes is None and scoped_clients:
            raise ValueError(
                f'auth.mcp.clients ({", ".join(scoped_clients)}) grant scopes but auth.mcp.operation_scopes defines none, '
                'so they would have no effect here.'
            )
        if self.operation_scopes is None and self.other_clients and self.other_clients.scopes:
            raise ValueError(
                'auth.mcp.other_clients grants scopes but auth.mcp.operation_scopes defines none, '
                'so they would have no effect here.'
            )
        return self

    def refuse_scope_settings_for_gateway_tokens(self, server_name: str) -> None:
        """Refuse operation scopes no client could receive, and endpoint scopes that would stop no one.

        It holds where the gateway mints the tokens, from the scopes a configured client is granted.
        An endpoint-wide ``required_scopes`` would then check only what the gateway itself just granted.
        A token another issuer mints carries whatever that issuer grants, so neither rule applies to it.
        """
        if self.operation_scopes is not None and not self.clients and not self.other_clients:
            raise ValueError(
                f'Server "{server_name}": auth.mcp.operation_scopes needs auth.mcp.clients or auth.mcp.other_clients, '
                'since only a client configured there is granted scopes, so it would have no effect here.'
            )
        if self.operation_scopes is not None and 'required_scopes' in self.model_fields_set:
            raise ValueError(
                f'Server "{server_name}": auth.mcp.required_scopes cannot be combined with auth.mcp.operation_scopes. '
                'Every token is minted from a client in auth.mcp.clients or auth.mcp.other_clients, '
                'so an endpoint-wide scope would stop no one.'
            )

    def refuse_scope_settings_for_issuer_tokens(self, server_name: str) -> None:
        """Refuse an external issuer whose clients would be told no scope to ask for first.

        It holds where another issuer mints the tokens, whose clients read ``scopes_supported``
        from the protected resource metadata, and ``required_scopes`` is what that lists.
        With none listed, the MCP SDK falls back to every scope the issuer advertises,
        which the issuer refuses for any client it restricts.
        """
        if self.resolve_issuer() and not self.required_scopes:
            raise ValueError(
                f'Server "{server_name}": auth.mcp.issuer needs auth.mcp.required_scopes, '
                'the scopes every client asks the issuer for first. '
                'Without them the protected resource metadata lists no scopes_supported, '
                'and the MCP SDK then asks for every scope the issuer advertises, '
                'which the issuer refuses for any client it restricts.'
            )

    def resolve_other_clients(self) -> OtherClientsConfig | None:
        """The rule for unlisted Client ID Metadata Document clients, or ``None`` when none may sign in."""
        if self.other_clients is None:
            return None if self.clients else OtherClientsConfig()
        return self.other_clients or None

    @property
    def accepts_dynamic_registration(self) -> bool:
        """Whether clients may register themselves, which only a config naming neither kind of client allows.

        The 2026-07-28 spec deprecates Dynamic Client Registration.
        It cannot be held to ``other_clients.trusted_hosts``, since nothing a client registers is verified,
        so setting ``other_clients`` at all turns it off.
        """
        return not self.clients and self.other_clients is None

    def resolve_issuer(self) -> str | None:
        """External authorization server issuer after env-var substitution."""
        return _resolve_env_var(self.issuer)

    def refuse_client_settings_without_sign_in(self, server_name: str) -> None:
        """Refuse what only a user signing in reads, for a server where agents obtain tokens as themselves.

        Each client needs a secret, since it is its only proof of identity.
        A redirect, default scopes and the consent page all belong to a sign-in that never happens.
        A request with no scope is granted all of ``scopes``, so ``default_scopes`` would have nothing to narrow.
        """
        problems = [
            f'auth.mcp.clients.{client_id}.{field} {reason}'
            for client_id, client in self.clients.items()
            for field, reason in client.find_settings_without_sign_in()
        ]
        problems += [
            f'auth.mcp.clients.{client_id} names a Client ID Metadata Document, whose client signs a user in'
            for client_id in self.clients
            if is_url_client_id(client_id)
        ]
        if problems:
            raise ValueError(
                f'Server "{server_name}": agents here obtain tokens with their own credentials, and no user signs in. '
                + '. '.join(problems)
                + '.'
            )


class AuthConfig(pydantic.BaseModel):
    """Authentication for one server, split by direction.

    ``upstream`` is how the gateway reaches the API, and ``mcp`` is who may call this MCP endpoint.
    Each field is in exactly one of the two, so a name such as ``issuer`` means one thing in each.

    ``token``, the issuers and the OAuth client fields accept substitution at resolve time,
    in both the ``${ENV_VAR}`` and ``${ENV_VAR:-default}`` forms.
    Numeric OAuth credentials are coerced from int to str,
    so unquoted YAML values still parse on providers that use numeric ``client_id``.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    upstream: UpstreamAuthConfig = UpstreamAuthConfig()
    mcp: McpAuthConfig = McpAuthConfig()

    @pydantic.model_validator(mode='before')
    @classmethod
    def _refuse_moved_keys(cls, data: typing.Any) -> typing.Any:
        """Name the new place of every key a config still writes at the top of ``auth``."""
        if not isinstance(data, dict):
            return data
        moved = [f'auth.{key} is now {_MOVED_AUTH_KEYS[key]}' for key in data if key in _MOVED_AUTH_KEYS]
        if moved:
            raise ValueError('. '.join(moved) + '.')
        return data


class CORSConfig(pydantic.BaseModel):
    """Starlette CORS middleware settings for the gateway HTTP app."""

    model_config = pydantic.ConfigDict(extra='forbid')

    allow_origins: list[str] = ['*']
    allow_methods: list[str] = ['*']
    allow_headers: list[str] = ['*']
    expose_headers: list[str] = ['*']


class DNSRebindingProtectionConfig(pydantic.BaseModel):
    """``Host`` and ``Origin`` allowlists for the MCP endpoints, which is how a DNS rebinding attack is refused.

    Left empty, a loopback bind accepts only the loopback names, and any other bind accepts any ``Host``.
    Listing hosts turns the check on for every bind, accepting only what is listed.
    Entries match exactly, except that a trailing ``:*`` accepts any port.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    allowed_hosts: list[str] = []
    allowed_origins: list[str] = []

    @pydantic.model_validator(mode='after')
    def _require_hosts_with_origins(self) -> typing.Self:
        """Refuse origins without hosts, since the check they turn on would then refuse every ``Host``."""
        if self.allowed_origins and not self.allowed_hosts:
            raise ValueError(
                'dns_rebinding_protection.allowed_origins needs dns_rebinding_protection.allowed_hosts, '
                'since the check it turns on would otherwise refuse every Host.'
            )
        return self


class StoreConfig(pydantic.BaseModel):
    """Selects the ``TokenStore`` backend (in-process memory or Redis)."""

    model_config = pydantic.ConfigDict(extra='forbid')

    type: typing.Literal['memory', 'redis'] = 'memory'
    redis_url: str = 'redis://localhost:6379'
    key_prefix: str = 'mcp_gw'


class LoggingConfig(pydantic.BaseModel):
    """Logging configuration.

    CLI flags (``--log-level``, ``--log-format``, ``--log-file``, ``-v`` / ``-q``) override these.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    level: typing.Literal['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] = 'INFO'
    format: typing.Literal['text', 'json'] = 'text'
    file: str | None = None


class PolicyConfig(pydantic.BaseModel):
    """Glob filters that decide which operations become MCP tools."""

    model_config = pydantic.ConfigDict(extra='forbid')

    allow: list[str] | None = None
    deny: list[str] | None = None
    # Named for the spec-side annotation it filters on, ``x-mcp-integration``.
    annotated_only: bool = False


class SearchFieldWeights(pydantic.BaseModel):
    """How much a matching term counts in each field ``search_operations`` ranks by.

    A field left unset keeps the gateway's default weight, and 0 leaves the field out of search.
    ``parameters``, each argument's name and description, defaults to 0.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    name: float | None = pydantic.Field(default=None, ge=0)
    summary: float | None = pydantic.Field(default=None, ge=0)
    description: float | None = pydantic.Field(default=None, ge=0)
    path: float | None = pydantic.Field(default=None, ge=0)
    tags: float | None = pydantic.Field(default=None, ge=0)
    parameters: float | None = pydantic.Field(default=None, ge=0)

    @pydantic.model_validator(mode='after')
    def _refuse_all_zero(self) -> typing.Self:
        """Refuse setting every field to 0, since search would then match nothing."""
        weights = self.model_dump()
        # Parameters are searched only when given a weight, so leaving them unset counts as 0.
        weights['parameters'] = weights['parameters'] or 0
        if all(weight == 0 for weight in weights.values()):
            raise ValueError('exposure.search.field_weights sets every field to 0, so search would match nothing.')
        return self


class SemanticSearchConfig(pydantic.BaseModel):
    """Search by meaning as well as by words, through an embedding model the ``embeddings`` extra reaches.

    ``model`` names it as Pydantic AI does, such as ``openai:text-embedding-3-small`` or ``ollama:bge-m3``,
    and the provider reads its key and address from its usual environment variables.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    model: str = pydantic.Field(min_length=1)
    # Shorter vectors, for a model that can return them, such as OpenAI's text-embedding-3 models.
    dimensions: int | None = pydantic.Field(default=None, gt=0)
    # Where the operations' vectors are kept between restarts, so only a changed operation is embedded again.
    cache_directory: pathlib.Path | None = None
    # semantic ranks by the embedding alone, and hybrid merges that ranking with the word ranking.
    # semantic is the default, since on the search benchmark's dev split it ranked better than hybrid.
    retrieval: typing.Literal['semantic', 'hybrid'] = 'semantic'


class SearchConfig(pydantic.BaseModel):
    """How ``search_operations`` ranks operations under dynamic exposure."""

    model_config = pydantic.ConfigDict(extra='forbid')

    field_weights: SearchFieldWeights = SearchFieldWeights()
    semantic: SemanticSearchConfig | None = None


class ExposureConfig(pydantic.BaseModel):
    """How a server's operations surface as MCP primitives.

    The two settings interact, so they live together rather than as sibling keys.
    ``style: dynamic`` fronts the whole spec with meta-tools, which surface every operation uniformly,
    so it refuses ``promote_resources``.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    style: typing.Literal['static', 'dynamic'] = 'static'
    # Whether a parameterless GET may become an MCP resource instead of a tool.
    # Named for what it does.
    # The operations it promotes are the ones a client reads rather than calls,
    # and an operation can still opt in per-operation regardless.
    promote_resources: bool = False
    # Ranking for the search_operations meta-tool, so it only takes effect with ``style: dynamic``.
    search: SearchConfig | None = None

    @pydantic.model_validator(mode='after')
    def _refuse_settings_of_the_other_style(self) -> typing.Self:
        """Refuse a setting the chosen ``style`` ignores, since it would have no effect.

        Only the dynamic meta-tools search, and only static exposure promotes resources.
        """
        if self.search is not None and self.style != 'dynamic':
            raise ValueError('exposure.search needs exposure.style dynamic, so it would have no effect here.')
        if self.promote_resources and self.style == 'dynamic':
            raise ValueError('exposure.promote_resources needs exposure.style static, so it would have no effect here.')
        return self


class OperationOverride(McpIntegration):
    """What ``operations.<operationId>`` overrides of one operation.

    ``tool`` and ``resource`` replace the spec's ``x-mcp-integration``.
    ``security`` replaces the spec's own ``security``, in the same OpenAPI form,
    for a spec that declares an operation's upstream scopes wrongly.
    """

    model_config = pydantic.ConfigDict(extra='forbid')

    security: list[dict[str, list[str]]] | None = None

    @property
    def overrides_integration(self) -> bool:
        """Whether this replaces ``x-mcp-integration``, which an override of ``security`` alone leaves as it is."""
        return self.model_fields_set != {'security'}

    @property
    def integration(self) -> McpIntegration:
        """The ``x-mcp-integration`` this override puts in place of the spec's."""
        return McpIntegration.model_validate(self.model_dump(exclude={'security'}, exclude_unset=True))


class ServerConfig(pydantic.BaseModel):
    """One upstream API, with its OpenAPI location, auth, policy, and HTTP timeout."""

    model_config = pydantic.ConfigDict(extra='forbid')

    name: str
    spec: str
    base_url: str | None = None
    path_prefix: str | None = None
    auth: AuthConfig = AuthConfig()
    policy: PolicyConfig = PolicyConfig()
    timeout: float = 90
    exposure: ExposureConfig = ExposureConfig()
    operations: dict[str, OperationOverride] = pydantic.Field(default_factory=dict)
    # Static headers sent on every upstream call, such as an API version the whole API requires.
    headers: dict[str, str] = pydantic.Field(default_factory=dict)
    # Guidance about the server as a whole, such as when to use it rather than another,
    # sent in the MCP ``initialize`` result, which clients place in the model's context.
    instructions: str | None = None

    @pydantic.field_validator('name')
    @classmethod
    def _validate_name(cls, name: str) -> str:
        if not name.replace('-', '').replace('_', '').isalnum():
            raise ValueError(f'Server name must be alphanumeric (with - or _): {name}')
        return name

    @pydantic.field_validator('instructions')
    @classmethod
    def _validate_instructions(cls, instructions: str | None) -> str | None:
        """Trim the newline a YAML block scalar leaves, and treat blank text as unset."""
        if instructions is None:
            return None
        return instructions.strip() or None

    @pydantic.field_validator('headers')
    @classmethod
    def _validate_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        if any(name.lower() == 'authorization' for name in headers):
            raise ValueError('headers cannot set Authorization. The upstream credential belongs to auth.')
        return headers

    @pydantic.model_validator(mode='after')
    def _refuse_resources_that_cannot_take_effect(self) -> typing.Self:
        """Refuse an ``operations.<operationId>.resource`` that no resource would come of.

        Only static exposure with ``exposure.promote_resources`` registers resources.
        The dynamic meta-tools surface every operation uniformly, and without promotion every operation is a tool.
        A spec's own ``x-mcp-integration.resource`` is only warned about, since the operator did not write it.
        """
        if self.exposure.promote_resources:
            return self
        named = [operation_id for operation_id, override in self.operations.items() if override.resource is not None]
        if named:
            fields = ', '.join(f'operations.{operation_id}.resource' for operation_id in named)
            needed = 'exposure.style static' if self.exposure.style == 'dynamic' else 'exposure.promote_resources true'
            raise ValueError(f'{fields} needs {needed}, so it would have no effect here.')
        return self

    def resolve_headers(self) -> dict[str, str]:
        """Static upstream headers after env-var substitution."""
        return {
            name: resolved for name, value in self.headers.items() if (resolved := _resolve_env_var(value)) is not None
        }

    @pydantic.computed_field
    @property
    def mount_path(self) -> str:
        """HTTP path prefix for this server, derived from ``path_prefix`` or ``name``."""
        if self.path_prefix is not None:
            return f'/{self.path_prefix.strip("/")}'
        return f'/{self.name}'


class GatewayConfig(pydantic.BaseModel):
    """Process-wide settings for the listen address, transports, store, logging, and registered servers."""

    model_config = pydantic.ConfigDict(extra='forbid')

    host: str = '0.0.0.0'
    port: int = 8000
    url: str = ''
    transport: typing.Literal['sse', 'streamable-http', 'stdio'] = 'streamable-http'
    debug: bool = False
    enable_docs: bool = False
    cors: CORSConfig = CORSConfig()
    dns_rebinding_protection: DNSRebindingProtectionConfig = DNSRebindingProtectionConfig()
    store: StoreConfig = StoreConfig()
    logging: LoggingConfig = LoggingConfig()
    servers: list[ServerConfig] = pydantic.Field(default_factory=list)

    @pydantic.model_validator(mode='after')
    def _set_default_url(self) -> typing.Self:
        """Default ``url`` to ``http://{host}:{port}`` when unset, mapping ``0.0.0.0`` to ``localhost``."""
        if not self.url:
            public_host = 'localhost' if self.host == '0.0.0.0' else self.host
            self.url = f'http://{public_host}:{self.port}'
        return self

    @classmethod
    def from_yaml(cls, path: str | pathlib.Path) -> typing.Self:
        """Load gateway config from a YAML file (thin wrapper over ``build_gateway_config``)."""
        return typing.cast(typing.Self, build_gateway_config(yaml_layer(path)))

    @classmethod
    def from_single_spec(
        cls,
        spec: str,
        name: str = 'api',
        base_url: str | None = None,
        auth: AuthConfig | None = None,
        transport: typing.Literal['sse', 'streamable-http', 'stdio'] | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> typing.Self:
        """Build a single-server config, where ``host`` / ``port`` / ``transport`` only override when set."""
        layers: list[dict[str, typing.Any]] = [
            single_spec_layer(spec=spec, name=name, base_url=base_url, auth=auth or AuthConfig()),
        ]
        runtime: dict[str, typing.Any] = {}
        if host is not None:
            runtime['host'] = host
        if port is not None:
            runtime['port'] = port
        if transport is not None:
            runtime['transport'] = transport
        if runtime:
            layers.append(runtime)
        return typing.cast(typing.Self, build_gateway_config(*layers))


def yaml_layer(path: str | pathlib.Path) -> dict[str, typing.Any]:
    """Read a YAML config file into a partial-settings dict.

    Empty files yield an empty layer (no contributions).
    Missing files raise ``FileNotFoundError`` to match historical behaviour.
    """
    resolved = pathlib.Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f'Config file not found: {resolved}')
    raw = yaml.safe_load(resolved.read_text(encoding='utf-8'))
    return raw or {}


def single_spec_layer(
    *,
    spec: str,
    name: str = 'api',
    base_url: str | None = None,
    auth: AuthConfig | None = None,
) -> dict[str, typing.Any]:
    """Build a partial-settings dict for the single-spec CLI / API entry path.

    The returned dict mirrors the ``servers:`` shape that ``GatewayConfig`` expects,
    so it composes via ``build_gateway_config`` like any other layer.
    """
    server: dict[str, typing.Any] = {'name': name, 'spec': spec}
    if base_url is not None:
        server['base_url'] = base_url
    # Only what the caller actually set, so a default never arrives looking like a choice.
    # Dumping everything would also make every field count as explicitly set on the way back in,
    # which is what the applicability check reads to tell a decision from a default.
    server['auth'] = (auth or AuthConfig()).model_dump(exclude_unset=True)
    return {'servers': [server]}


def build_gateway_config(*layers: dict[str, typing.Any]) -> GatewayConfig:
    """Compose layered partial-settings dicts (later wins) into a validated ``GatewayConfig``.

    A *layer* is a dict containing only the fields that source actually supplies.
    Pydantic defaults form the implicit floor, so callers never need to repeat them.
    Order encodes precedence.
    Typical CLI use is::

        build_gateway_config(yaml_layer, cli_layer)

    Sub-trees (``logging``, per-server ``policy`` / ``auth``) merge recursively,
    so a CLI flag that only sets ``logging.level`` does not erase ``logging.format`` from YAML.
    Lists (``servers``) are replaced wholesale, since merging by index is rarely what you want.
    """
    merged: dict[str, typing.Any] = {}
    for layer in layers:
        merged = _deep_merge(merged, layer)
    return GatewayConfig.model_validate(merged)


def is_url_client_id(client_id: str) -> bool:
    """Whether an ``auth.mcp.clients`` key is a URL, meant as a Client ID Metadata Document's."""
    return urllib.parse.urlsplit(client_id).scheme in ('http', 'https')


def _deep_merge(base: dict[str, typing.Any], override: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Recursively merge ``override`` into ``base``, with ``override`` winning for non-dict values.

    Keys present only in ``base`` are preserved.
    Keys whose value on both sides is a dict are merged recursively,
    so a partial ``{'logging': {'level': 'DEBUG'}}`` does not blow away the rest of ``logging``.
    """
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _refuse_invalid_scope_tokens(scopes: typing.Iterable[str], location: str) -> None:
    """Refuse a scope name a token could not carry, since one with a space would split into two scopes."""
    invalid = sorted(scope for scope in scopes if not SCOPE_TOKEN.fullmatch(scope))
    if invalid:
        raise ValueError(f'{location} names invalid scopes {invalid!r}. A scope cannot contain spaces, " or \\.')
