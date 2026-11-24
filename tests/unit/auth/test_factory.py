import typing
from unittest.mock import patch

import pytest

from openapi_mcp_gateway.auth.assembly import AuthTypeContext, McpSetting, UpstreamSetting
from openapi_mcp_gateway.auth.factory import AUTH_TYPE_HANDLERS, build_auth
from openapi_mcp_gateway.auth.mcp.external_issuer import McpExternalIssuerMetadata
from openapi_mcp_gateway.auth.upstream.resolver import (
    UpstreamNoCredentialAuthResolver,
    UpstreamPassthroughAuthResolver,
    UpstreamStaticAuthResolver,
)
from openapi_mcp_gateway.gateway import Gateway
from openapi_mcp_gateway.openapi import OpenAPISpec
from openapi_mcp_gateway.scopes import McpUnrestrictedScopePolicy, ScopeRegistry
from openapi_mcp_gateway.settings import (
    AuthConfig,
    GatewayConfig,
    McpAuthConfig,
    ServerConfig,
    UpstreamAuthConfig,
)
from openapi_mcp_gateway.stores.memory import MemoryTokenStore
from tests.client_metadata import serving_resolver
from tests.constants import API_URL, GATEWAY_URL, ISSUER, JWKS_URL, TOKEN_URL
from tests.flow import (
    auth_context,
    build_spec,
    server_entry,
    spec_with_authorization_code,
    spec_with_client_credentials,
)


# The upstream of each kind, with what it needs to build, and the spec it builds against.
BEARER = {'type': 'bearer', 'token': 't'}
CLIENT_CREDENTIALS = {'type': 'oauth2', 'flow': 'client_credentials', 'client_id': 'cid', 'client_secret': 'sec'}
AUTHORIZATION_CODE = {'type': 'oauth2', 'flow': 'authorization_code', 'client_id': 'cid', 'client_secret': 'sec'}
TOKEN_EXCHANGE = {
    'type': 'oauth2',
    'flow': 'token_exchange',
    'audience': API_URL,
    'client_id': 'cid',
    'client_secret': 'sec',
}
SPEC_BY_FLOW = {
    'client_credentials': spec_with_client_credentials,
    'authorization_code': spec_with_authorization_code,
}

# What ``token_exchange`` needs on the MCP side to build.
TOKEN_EXCHANGE_MCP = {'issuer': ISSUER, 'required_scopes': ['mcp']}


class TestStaticCredentials:
    """``bearer`` and ``api_key`` differ only in how the credential is framed."""

    def test_bearer_prefixes_the_token(self):
        """A bearer credential travels as ``Authorization: Bearer <token>``."""
        setup = build_auth(_context(AuthConfig(upstream=UpstreamAuthConfig(type='bearer', token='my-token'))))

        assert isinstance(setup.resolver, UpstreamStaticAuthResolver)
        assert setup.resolver._header_name == 'Authorization'
        assert setup.resolver._header_value == 'Bearer my-token'

    def test_api_key_sends_the_token_unprefixed(self):
        """An API key is the whole header value, on a header the API names."""
        setup = build_auth(_context(AuthConfig(upstream=UpstreamAuthConfig(type='api_key', token='key123'))))

        assert isinstance(setup.resolver, UpstreamStaticAuthResolver)
        assert setup.resolver._header_name == 'X-API-Key'
        assert setup.resolver._header_value == 'key123'

    def test_api_key_header_is_configurable(self):
        """``api_key_header`` names the header, since APIs disagree on it."""
        setup = build_auth(
            _context(AuthConfig(upstream=UpstreamAuthConfig(type='api_key', token='key123', api_key_header='X-Custom')))
        )

        assert isinstance(setup.resolver, UpstreamStaticAuthResolver)
        assert setup.resolver._header_name == 'X-Custom'

    def test_env_var_substitution_reaches_the_header(self, monkeypatch):
        """``${VAR}`` resolves on the way through, so config never holds the secret."""
        monkeypatch.setenv('TEST_TOKEN', 'env-token')
        setup = build_auth(_context(AuthConfig(upstream=UpstreamAuthConfig(type='bearer', token='${TEST_TOKEN}'))))

        assert isinstance(setup.resolver, UpstreamStaticAuthResolver)
        assert setup.resolver._header_value == 'Bearer env-token'

    def test_a_declared_type_without_a_token_sends_nothing(self):
        """A half-filled config serves the API unauthenticated rather than failing at startup.

        The upstream rejects the call, which is a clearer signal than a gateway that will not boot.
        """
        setup = build_auth(_context(AuthConfig(upstream=UpstreamAuthConfig(type='bearer'))))

        assert isinstance(setup.resolver, UpstreamNoCredentialAuthResolver)


class TestOtherTypes:
    """The remaining types need no credential of the gateway's own."""

    def test_none_sends_nothing(self):
        """A public API needs no header."""
        setup = build_auth(_context(AuthConfig(upstream=UpstreamAuthConfig(type='none'))))

        assert isinstance(setup.resolver, UpstreamNoCredentialAuthResolver)

    def test_a_token_alongside_none_is_refused(self):
        """Previously the token was quietly ignored, which read as auth that was never applied."""
        with pytest.raises(ValueError, match='no effect'):
            build_auth(_context(AuthConfig(upstream=UpstreamAuthConfig(type='none', token='ignored'))))


class TestDispatch:
    """The registry is the extension point, so adding a type never edits a branch."""

    def test_every_declared_type_has_a_handler(self):
        """``auth.upstream.type`` and the registry must not drift apart.

        A value the config accepts but the registry does not know would fail only at startup,
        for the one deployment that happened to use it.
        """
        declared = set(typing.get_args(UpstreamAuthConfig.model_fields['type'].annotation))

        assert declared == set(AUTH_TYPE_HANDLERS)

    def test_an_unknown_type_names_what_is_supported(self):
        """The error lists the alternatives rather than only rejecting the input."""
        context = _context(AuthConfig())
        context.entry.auth.upstream.type = 'invented'  # type: ignore[assignment]

        with pytest.raises(ValueError, match='unsupported auth type'):
            build_auth(context)


class TestPassthroughAuthType:
    """``passthrough`` is an auth type, since the gateway runs no OAuth exchange in that mode."""

    def test_resolves_without_consulting_a_flow(self):
        """It never reaches the flow factory, so no spec or flow value is involved."""
        gateway = Gateway.from_config(GatewayConfig())
        entry = server_entry(AuthConfig(upstream=UpstreamAuthConfig(type='passthrough')))

        setup = gateway._resolve_auth(entry, build_spec(), ScopeRegistry(McpUnrestrictedScopePolicy()))

        assert isinstance(setup.resolver, UpstreamPassthroughAuthResolver)
        assert setup.provider is None
        assert setup.verifier is None
        assert setup.settings is None


class TestUpstreamPrincipal:
    """Each strategy declares whose identity the upstream sees, which selects who issues the MCP tokens."""

    @pytest.mark.parametrize(
        ('auth', 'principal'),
        [
            ({'upstream': {'type': 'none'}}, 'gateway'),
            ({'upstream': BEARER}, 'gateway'),
            ({'upstream': {'type': 'api_key', 'token': 'k'}}, 'gateway'),
            ({'upstream': CLIENT_CREDENTIALS}, 'gateway'),
            ({'upstream': {**CLIENT_CREDENTIALS, 'flow': None}}, 'gateway'),
            ({'upstream': AUTHORIZATION_CODE}, 'user'),
            ({'upstream': TOKEN_EXCHANGE, 'mcp': TOKEN_EXCHANGE_MCP}, 'user'),
            ({'upstream': {'type': 'passthrough'}}, 'caller'),
        ],
    )
    def test_each_upstream_names_its_principal(self, auth, principal):
        """An unset flow takes the principal of the flow the spec resolves it to."""
        context = _build_context(auth)
        handler = AUTH_TYPE_HANDLERS[context.entry.auth.upstream.type]()
        assert handler.select(context).upstream_principal == principal


class TestUpstreamSettingsWithoutEffect:
    """An ``auth.upstream`` field the selected strategy never reads is refused, naming what would read it.

    A known key in the wrong place is the same mistake as an unknown one.
    Both leave an operator believing they configured something.
    """

    def test_every_upstream_setting_is_an_upstream_field(self):
        """``UpstreamSetting`` and ``auth.upstream`` must not drift apart, or a strategy could name a missing field."""
        assert set(typing.get_args(UpstreamSetting)) == set(UpstreamAuthConfig.model_fields) - {'type'}

    @pytest.mark.parametrize(
        ('upstream', 'refusal'),
        [
            ({**BEARER, 'api_key_header': 'X-Custom'}, r'api_key_header needs auth\.upstream\.type api_key,'),
            (
                {**CLIENT_CREDENTIALS, 'token': 'ignored'},
                r'token needs auth\.upstream\.type bearer, or auth\.upstream\.type api_key,',
            ),
            ({**BEARER, 'audience': 'https://api'}, r'audience needs auth\.upstream\.type oauth2,'),
            ({**BEARER, 'flow': 'client_credentials'}, r'flow needs auth\.upstream\.type oauth2,'),
            ({**CLIENT_CREDENTIALS, 'issuer': ISSUER}, r'issuer needs auth\.upstream\.flow authorization_code,'),
            (
                {**CLIENT_CREDENTIALS, 'enforce_scopes': True},
                r'enforce_scopes needs auth\.upstream\.flow authorization_code or token_exchange,',
            ),
            (
                {**CLIENT_CREDENTIALS, 'authorization_url': 'https://idp/authorize'},
                r'authorization_url needs auth\.upstream\.flow authorization_code,',
            ),
            (
                {**CLIENT_CREDENTIALS, 'flow': None, 'required_scopes': ['openid']},
                r'required_scopes needs auth\.upstream\.flow authorization_code or token_exchange,',
            ),
        ],
    )
    def test_a_setting_nothing_reads_is_refused(self, upstream, refusal):
        """An unset flow is checked against the flow the spec resolves it to."""
        with pytest.raises(ValueError, match=rf'Server "srv": auth\.upstream\.{refusal} so it would have no effect'):
            _build({'upstream': upstream})

    @pytest.mark.parametrize(
        'auth',
        [
            {'upstream': BEARER},
            {'upstream': {'type': 'api_key', 'token': 'k', 'api_key_header': 'X-Custom'}},
            {'upstream': {**AUTHORIZATION_CODE, 'issuer': ISSUER, 'required_scopes': ['openid']}},
            {'upstream': {**CLIENT_CREDENTIALS, 'scopes': ['api'], 'audience': API_URL}},
            {'upstream': {'type': 'none'}},
        ],
    )
    def test_a_setting_the_strategy_reads_is_accepted(self, auth):
        """Refusing the inapplicable must not refuse the ordinary."""
        assert _build(auth)


class TestMcpSettingsWithoutEffect:
    """An ``auth.mcp`` field the assembled MCP half never reads is refused, naming what would read it."""

    def test_every_mcp_setting_is_an_mcp_field(self):
        """``McpSetting`` and ``auth.mcp`` must not drift apart, or a strategy could name a missing field."""
        assert set(typing.get_args(McpSetting)) == set(McpAuthConfig.model_fields)

    @pytest.mark.parametrize(
        ('auth', 'refusal'),
        [
            (
                {'upstream': BEARER, 'mcp': {'required_scopes': ['x']}},
                r'required_scopes needs auth\.mcp\.issuer on .*, or auth\.mcp\.clients on',
            ),
            ({'upstream': BEARER, 'mcp': {'access_token_ttl': 60}}, r'access_token_ttl needs auth\.mcp\.clients on'),
            (
                {'upstream': CLIENT_CREDENTIALS, 'mcp': {'access_token_ttl': 60}},
                r'access_token_ttl needs auth\.mcp\.clients on',
            ),
            (
                {'upstream': AUTHORIZATION_CODE, 'mcp': {'issuer': ISSUER}},
                r'issuer needs auth\.upstream\.flow token_exchange',
            ),
            (
                {'upstream': {'type': 'passthrough'}, 'mcp': {'issuer': ISSUER}},
                r'issuer needs auth\.upstream\.flow token_exchange',
            ),
            (
                {'upstream': TOKEN_EXCHANGE, 'mcp': {**TOKEN_EXCHANGE_MCP, 'clients': {'agent': {}}}},
                r'clients needs auth\.upstream\.flow authorization_code, where',
            ),
            (
                {'upstream': BEARER, 'mcp': {'clients': {'agent': {'client_secret': 's'}}, 'other_clients': {}}},
                r'other_clients needs auth\.upstream\.flow authorization_code',
            ),
            (
                {'upstream': {'type': 'passthrough'}, 'mcp': {'clients': {'agent': {'client_secret': 's'}}}},
                r'clients needs auth\.upstream\.flow authorization_code.*, or auth\.mcp\.clients on an upstream that sees only',
            ),
        ],
    )
    def test_a_setting_nothing_reads_is_refused(self, auth, refusal):
        """The suggestion keeps to the same upstream identity where a strategy there reads the field."""
        with pytest.raises(ValueError, match=rf'Server "srv": auth\.mcp\.{refusal}'):
            _build(auth)

    @pytest.mark.parametrize(
        'auth',
        [
            {'upstream': AUTHORIZATION_CODE, 'mcp': {'access_token_ttl': 60, 'refresh_token_ttl': 600}},
            {'upstream': TOKEN_EXCHANGE, 'mcp': {**TOKEN_EXCHANGE_MCP, 'required_scopes': ['x']}},
            {'upstream': {**CLIENT_CREDENTIALS, 'flow': None}, 'mcp': {'issuer': ISSUER, 'required_scopes': ['x']}},
            {
                'upstream': BEARER,
                'mcp': {'clients': {'agent': {'client_secret': 's'}}, 'access_token_ttl': 600},
            },
            {
                'upstream': {**CLIENT_CREDENTIALS, 'flow': None},
                'mcp': {
                    'operation_scopes': {'auto': 'tags'},
                    'clients': {'agent': {'client_secret': 's', 'scopes': ['api']}},
                },
            },
        ],
    )
    def test_a_setting_the_mcp_half_reads_is_accepted(self, auth):
        assert _build(auth)

    @pytest.mark.parametrize(
        ('auth', 'refusal'),
        [
            (
                {'upstream': AUTHORIZATION_CODE, 'mcp': {'operation_scopes': {'auto': 'tags'}}},
                r'auth\.mcp\.operation_scopes needs auth\.mcp\.clients',
            ),
            (
                {
                    'upstream': AUTHORIZATION_CODE,
                    'mcp': {'operation_scopes': {'auto': 'tags'}, 'clients': {'agent': {}}, 'required_scopes': ['api']},
                },
                r'auth\.mcp\.required_scopes cannot be combined with auth\.mcp\.operation_scopes',
            ),
            (
                {'upstream': TOKEN_EXCHANGE, 'mcp': {'issuer': ISSUER, 'operation_scopes': {'auto': 'tags'}}},
                r'auth\.mcp\.issuer needs auth\.mcp\.required_scopes, the scopes every client asks the issuer for first',
            ),
            (
                {'upstream': BEARER, 'mcp': {'issuer': ISSUER}},
                r'auth\.mcp\.issuer needs auth\.mcp\.required_scopes',
            ),
            (
                {'upstream': BEARER, 'mcp': {'issuer': ISSUER, 'clients': {'agent': {'client_secret': 's'}}}},
                r"auth\.mcp\.issuer on .* and auth\.mcp\.clients on .* each issue this endpoint's tokens",
            ),
        ],
    )
    def test_scope_settings_the_token_issuer_cannot_honour_are_refused(self, auth, refusal):
        """Where the gateway mints the tokens, a client must be granted the scopes, and only one party mints them.

        Under an external issuer, a client must be told which scopes to ask for first.
        """
        with pytest.raises(ValueError, match=rf'Server "srv": {refusal}'):
            _build(auth)

    def test_the_refusal_comes_before_anything_is_built(self):
        """``token_exchange`` fetches its issuer's metadata when built, so a misplaced setting must stop it first."""
        context = _build_context({'upstream': TOKEN_EXCHANGE, 'mcp': {**TOKEN_EXCHANGE_MCP, 'access_token_ttl': 60}})
        with (
            patch('openapi_mcp_gateway.auth.authorities.fetch_mcp_external_issuer_metadata') as fetch,
            pytest.raises(ValueError, match=r'access_token_ttl needs'),
        ):
            build_auth(context)
        fetch.assert_not_called()


def _context(auth: AuthConfig) -> AuthTypeContext:
    """Build the one shape every handler receives, for a server carrying ``auth``."""
    return AuthTypeContext(
        entry=ServerConfig(name='srv', spec='dummy.json', base_url=API_URL, auth=auth),
        spec=OpenAPISpec(title='test', version='1.0.0', operations=[], security_schemes={}),
        store=MemoryTokenStore(),
        gateway_url=GATEWAY_URL,
        scope_registry=ScopeRegistry(McpUnrestrictedScopePolicy()),
        client_metadata_resolver=serving_resolver({}),
    )


def _build(auth: dict):
    """Build the auth of a server carrying ``auth``, against a spec declaring its flow.

    Issuer discovery is stubbed, so ``token_exchange`` builds offline.
    """
    metadata = McpExternalIssuerMetadata(issuer=ISSUER, jwks_uri=JWKS_URL, token_endpoint=TOKEN_URL)
    with (
        patch('openapi_mcp_gateway.auth.authorities.fetch_mcp_external_issuer_metadata', return_value=metadata),
        patch('openapi_mcp_gateway.auth.mcp.external_issuer._build_jwk_client'),
    ):
        return build_auth(_build_context(auth))


def _build_context(auth: dict) -> AuthTypeContext:
    """What ``build_auth`` receives for a server carrying ``auth``, against a spec declaring its flow."""
    upstream = auth['upstream']
    flow = upstream.get('flow') or ('client_credentials' if upstream['type'] == 'oauth2' else None)
    spec = SPEC_BY_FLOW[flow]() if flow in SPEC_BY_FLOW else build_spec()
    return auth_context(server_entry(AuthConfig.model_validate(auth)), spec)
