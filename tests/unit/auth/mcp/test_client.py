import logging

import pytest
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import InvalidRedirectUriError, InvalidScopeError, OAuthClientInformationFull
from pydantic import AnyUrl

from openapi_mcp_gateway.auth.mcp.client import (
    McpDynamicClientRegistry,
    McpPreRegisteredClient,
    McpPreRegisteredClientDirectory,
)
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
            (None, ['issues:read']),
            ('issues:write', ['issues:write']),
            ('issues:write issues:delete', ['issues:write']),
        ],
    )
    def test_requested_scopes_are_narrowed_to_the_client_set(self, requested, granted):
        """Nothing asked grants the defaults, and a request is narrowed to the set, as RFC 6749 §3.3 allows."""
        client = _pre_registered(scopes=('issues:read', 'issues:write'), default_scopes=['issues:read'])
        assert client.validate_scope(requested) == granted

    def test_without_default_scopes_the_whole_set_is_the_default(self):
        client = _pre_registered(scopes=('issues:read', 'issues:write'))
        assert client.validate_scope(None) == ['issues:read', 'issues:write']

    def test_a_request_with_nothing_in_reach_is_refused(self, caplog):
        """Granting something other than what was asked would be silent, so ``invalid_scope`` says so instead."""
        client = _pre_registered(scopes=('issues:read',))
        with caplog.at_level(logging.WARNING), pytest.raises(InvalidScopeError):
            client.validate_scope('issues:delete')
        assert "asked for scopes ['issues:delete']" in caplog.text


class TestRedirectUris:
    """Loopback redirects match any port and path, and registered redirects match exactly."""

    @pytest.mark.parametrize(
        'redirect_uri',
        ['http://localhost:51234/callback', 'http://127.0.0.1:8080/', 'http://[::1]:9000/oauth/cb'],
    )
    def test_any_loopback_redirect_is_accepted_without_registered_uris(self, redirect_uri):
        """A CLI client picks its port at run time, so its user never has to know the callback."""
        client = _pre_registered()
        assert str(client.validate_redirect_uri(AnyUrl(redirect_uri))) == redirect_uri

    @pytest.mark.parametrize('redirect_uri', ['https://localhost/callback', 'http://evil.example.com/callback', None])
    def test_anything_but_a_loopback_redirect_is_refused_without_registered_uris(self, redirect_uri, caplog):
        """The refusal logs what the client sent, so its operator can copy it into ``redirect_uris``."""
        client = _pre_registered()
        with caplog.at_level(logging.WARNING), pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl(redirect_uri) if redirect_uri else None)
        assert f'used redirect_uri {redirect_uri}' in caplog.text

    def test_registered_uris_match_exactly(self):
        """Registering a callback turns off the loopback allowance, loopback addresses included."""
        client = _pre_registered(redirect_uris=[WEB_CALLBACK])
        assert str(client.validate_redirect_uri(AnyUrl(WEB_CALLBACK))) == WEB_CALLBACK
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl('http://localhost:3000/callback'))

    @pytest.mark.parametrize('redirect_uri', NEAR_MISSES)
    @pytest.mark.parametrize('registered', ['pre-registered', 'dynamic'])
    def test_a_near_miss_of_a_registered_uri_is_refused(self, registered, redirect_uri):
        """No prefix, suffix, or added query matches, as the MCP Security Best Practices require.

        The SDK does the matching, so this pins its behaviour for both kinds of client.
        """
        client = (
            _pre_registered(redirect_uris=[WEB_CALLBACK])
            if registered == 'pre-registered'
            else OAuthClientInformationFull(client_id='dynamic', redirect_uris=[AnyUrl(WEB_CALLBACK)])
        )
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl(redirect_uri))


class TestConsentRequirement:
    """Every client passes the consent page unless the operator exempts a pre-registered one."""

    @pytest.mark.parametrize('required', [True, False])
    def test_a_pre_registered_client_follows_its_setting(self, required):
        directory = McpPreRegisteredClientDirectory([_pre_registered(consent_required=required)])
        assert directory.requires_consent('agent') is required

    def test_a_dynamically_registered_client_always_needs_consent(self, memory_store):
        assert McpDynamicClientRegistry(memory_store, GATEWAY_ISSUER).requires_consent('anyone') is True


class TestClientLookup:
    """A pre-registered directory knows only the clients it was given."""

    async def test_a_given_client_is_found(self):
        client = _pre_registered()
        assert await McpPreRegisteredClientDirectory([client]).get('agent') is client

    async def test_an_unknown_client_is_absent(self):
        assert await McpPreRegisteredClientDirectory([]).get('stranger') is None

    def test_a_client_registered_twice_is_refused(self):
        """Keeping either record would decide silently which secret and consent setting apply."""
        with pytest.raises(ValueError, match='registered twice'):
            McpPreRegisteredClientDirectory([_pre_registered(), _pre_registered(consent_required=False)])


class TestRegistration:
    """Dynamic Client Registration stays only where no client is pre-registered."""

    async def test_pre_registered_directories_refuse_registration(self):
        directory = McpPreRegisteredClientDirectory([])
        assert directory.build_registration_options(['api']).enabled is False
        with pytest.raises(RegistrationError):
            await directory.register(OAuthClientInformationFull(client_id='newcomer'))

    @pytest.mark.parametrize(('mcp_endpoint_scopes', 'advertised'), [(['api'], ['api']), ([], None)])
    def test_pre_registered_directories_still_advertise_the_endpoint_scopes(self, mcp_endpoint_scopes, advertised):
        """``scopes_supported`` is read from the registration options, so it would vanish with registration."""
        options = McpPreRegisteredClientDirectory([]).build_registration_options(mcp_endpoint_scopes)
        assert options.valid_scopes == advertised

    def test_dynamic_registries_grant_the_endpoint_scopes(self, memory_store):
        options = McpDynamicClientRegistry(memory_store, GATEWAY_ISSUER).build_registration_options(['api'])
        assert options.enabled is True
        assert options.valid_scopes == options.default_scopes == ['api']


def _pre_registered(
    scopes: tuple[str, ...] = (),
    default_scopes: list[str] | None = None,
    redirect_uris: list[str] | None = None,
    consent_required: bool = True,
) -> McpPreRegisteredClient:
    """A pre-registered client ``agent`` holding ``scopes``, granted ``default_scopes`` or else all of them."""
    return McpPreRegisteredClient(
        client_id='agent',
        redirect_uris=[AnyUrl(uri) for uri in redirect_uris] if redirect_uris else None,
        scope=' '.join(scopes),
        default_scopes=list(scopes if default_scopes is None else default_scopes),
        consent_required=consent_required,
    )
