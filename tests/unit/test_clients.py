import logging

import pytest
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull
from pydantic import AnyUrl

from openapi_mcp_gateway.auth.flows import DynamicClientRegistry, PreRegisteredClientDirectory
from openapi_mcp_gateway.settings import ClientConfig
from tests.constants import BROWSER_ORIGIN, GATEWAY_ISSUER


# The callback a web client registers, matched exactly.
WEB_CALLBACK = f'{BROWSER_ORIGIN}/oauth/callback'

# Addresses that resemble ``WEB_CALLBACK`` closely enough to pass a loose match.
NEAR_MISSES = [
    f'{WEB_CALLBACK}/extra',
    f'{WEB_CALLBACK}?next=evil',
    f'{BROWSER_ORIGIN}/oauth',
    f'{BROWSER_ORIGIN}.evil.example.com/oauth/callback',
    WEB_CALLBACK.replace('https://', 'http://'),
]


class TestScopeGrant:
    """A pre-registered client is granted scopes within its set rather than refused for asking outside it."""

    @pytest.mark.parametrize(
        ('requested', 'granted'),
        [
            (None, ['issues:read', 'issues:write']),
            ('issues:write', ['issues:write']),
            ('issues:write issues:delete', ['issues:write']),
            ('issues:delete', ['issues:read', 'issues:write']),
        ],
    )
    async def test_requested_scopes_are_narrowed_to_the_client_set(self, requested, granted):
        """Nothing asked, or nothing within reach, grants the whole set, as RFC 6749 §3.3 allows."""
        client = await _pre_registered(scopes=['issues:read', 'issues:write'])
        assert client.validate_scope(requested) == granted

    async def test_endpoint_scopes_are_granted_beside_the_client_set(self):
        """Without ``auth.operation_scopes`` every token carries the endpoint scopes, as a registered client's would."""
        client = await _pre_registered(mcp_endpoint_scopes=('api',))
        assert client.validate_scope(None) == ['api']


class TestRedirectUris:
    """Loopback redirects match any port and path, and registered redirects match exactly."""

    @pytest.mark.parametrize(
        'redirect_uri',
        ['http://localhost:51234/callback', 'http://127.0.0.1:8080/', 'http://[::1]:9000/oauth/cb'],
    )
    async def test_any_loopback_redirect_is_accepted_without_registered_uris(self, redirect_uri):
        """A CLI client picks its port at run time, so its user never has to know the callback."""
        client = await _pre_registered()
        assert str(client.validate_redirect_uri(AnyUrl(redirect_uri))) == redirect_uri

    @pytest.mark.parametrize('redirect_uri', ['https://localhost/callback', 'http://evil.example.com/callback', None])
    async def test_anything_but_a_loopback_redirect_is_refused_without_registered_uris(self, redirect_uri, caplog):
        """The refusal logs what the client sent, so its operator can copy it into ``redirect_uris``."""
        client = await _pre_registered()
        with caplog.at_level(logging.WARNING), pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl(redirect_uri) if redirect_uri else None)
        assert f'used redirect_uri {redirect_uri}' in caplog.text

    async def test_registered_uris_match_exactly(self):
        """Registering a callback turns off the loopback allowance, loopback addresses included."""
        client = await _pre_registered(redirect_uris=[WEB_CALLBACK])
        assert str(client.validate_redirect_uri(AnyUrl(WEB_CALLBACK))) == WEB_CALLBACK
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl('http://localhost:3000/callback'))

    @pytest.mark.parametrize('redirect_uri', NEAR_MISSES)
    @pytest.mark.parametrize('registered', ['pre-registered', 'dynamic'])
    async def test_a_near_miss_of_a_registered_uri_is_refused(self, registered, redirect_uri):
        """No prefix, suffix, or added query matches, as the MCP Security Best Practices require.

        The SDK does the matching, so this pins its behaviour for both kinds of client.
        """
        client = (
            await _pre_registered(redirect_uris=[WEB_CALLBACK])
            if registered == 'pre-registered'
            else OAuthClientInformationFull(client_id='dynamic', redirect_uris=[AnyUrl(WEB_CALLBACK)])
        )
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl(redirect_uri))


class TestConsentRequirement:
    """Every client passes the consent page unless the operator exempts a pre-registered one."""

    @pytest.mark.parametrize(
        ('client', 'required'), [({}, True), ({'consent': True}, True), ({'consent': False}, False)]
    )
    def test_a_pre_registered_client_follows_its_setting(self, client, required):
        directory = PreRegisteredClientDirectory({'agent': ClientConfig.model_validate(client)}, ())
        assert directory.requires_consent('agent') is required

    def test_a_dynamically_registered_client_always_needs_consent(self, memory_store):
        assert DynamicClientRegistry(memory_store, GATEWAY_ISSUER).requires_consent('anyone') is True


class TestClientAuthentication:
    """How a pre-registered client proves itself at the token endpoint."""

    @pytest.mark.parametrize(
        ('client', 'method'),
        [({'client_secret': 'shh'}, 'client_secret_basic'), ({}, 'none')],
    )
    async def test_a_secret_selects_basic_authentication(self, client, method):
        """Without a secret the client is public and held to PKCE alone."""
        record = await _pre_registered(**client)
        assert record.token_endpoint_auth_method == method

    def test_a_secret_that_resolves_to_nothing_is_refused(self, monkeypatch):
        """An unset variable would otherwise turn a confidential client public, open to anyone knowing its id."""
        monkeypatch.delenv('AGENT_SECRET', raising=False)
        client = ClientConfig(client_secret='${AGENT_SECRET}')
        with pytest.raises(ValueError, match=r'auth\.clients\.agent\.client_secret'):
            PreRegisteredClientDirectory({'agent': client}, ())

    async def test_an_unknown_client_is_absent(self):
        directory = PreRegisteredClientDirectory({}, ())
        assert await directory.get('stranger') is None


class TestRegistration:
    """Dynamic Client Registration stays only where no client is pre-registered."""

    async def test_pre_registered_directories_refuse_registration(self):
        directory = PreRegisteredClientDirectory({}, ())
        assert directory.registration_options(['api']).enabled is False
        with pytest.raises(RegistrationError):
            await directory.register(OAuthClientInformationFull(client_id='newcomer'))

    @pytest.mark.parametrize(('mcp_endpoint_scopes', 'advertised'), [(['api'], ['api']), ([], None)])
    def test_pre_registered_directories_still_advertise_the_endpoint_scopes(self, mcp_endpoint_scopes, advertised):
        """``scopes_supported`` is read from the registration options, so it would vanish with registration."""
        options = PreRegisteredClientDirectory({}, ()).registration_options(mcp_endpoint_scopes)
        assert options.valid_scopes == advertised

    def test_dynamic_registries_grant_the_endpoint_scopes(self, memory_store):
        options = DynamicClientRegistry(memory_store, GATEWAY_ISSUER).registration_options(['api'])
        assert options.enabled is True
        assert options.valid_scopes == options.default_scopes == ['api']


async def _pre_registered(mcp_endpoint_scopes: tuple[str, ...] = (), **client: object) -> OAuthClientInformationFull:
    """The record a directory holding one ``auth.clients`` entry, ``agent``, gives the SDK."""
    directory = PreRegisteredClientDirectory({'agent': ClientConfig.model_validate(client)}, mcp_endpoint_scopes)
    record = await directory.get('agent')
    assert record is not None
    return record
