import logging

import pytest
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import InvalidRedirectUriError, InvalidScopeError, OAuthClientInformationFull
from pydantic import AnyUrl

from openapi_mcp_gateway.auth.mcp.client import (
    McpClientMetadataDirectory,
    McpDynamicClientRegistry,
    McpOtherClients,
    McpPreRegisteredClient,
    McpPreRegisteredClientDirectory,
)
from openapi_mcp_gateway.auth.mcp.client_metadata import ClientMetadataDocument, McpClientMetadataResolver
from tests.client_metadata import CLIENT_METADATA_HOST, CLIENT_METADATA_URL, client_metadata_document, serving_resolver
from tests.constants import BROWSER_ORIGIN, GATEWAY_ISSUER


# The callback a web client registers, matched exactly.
WEB_CALLBACK = f'{BROWSER_ORIGIN}/oauth/callback'

# A loopback callback registered without a port, as a client that picks its port at run time registers it.
LOOPBACK_CALLBACK = 'http://localhost/callback'

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

    @pytest.mark.parametrize('redirect_uri', [LOOPBACK_CALLBACK, 'http://localhost:54321/callback'])
    def test_a_registered_loopback_redirect_matches_on_any_port(self, redirect_uri):
        """OAuth 2.1 §8.4.2 requires it, since a CLI client picks its port at run time."""
        client = _pre_registered(redirect_uris=[LOOPBACK_CALLBACK])
        assert str(client.validate_redirect_uri(AnyUrl(redirect_uri))) == redirect_uri

    @pytest.mark.parametrize(
        'redirect_uri',
        [
            'http://localhost:54321/other',
            'http://localhost:54321/callback?next=evil',
            'http://127.0.0.1:54321/callback',
            'https://localhost:54321/callback',
        ],
    )
    def test_a_registered_loopback_redirect_still_matches_all_but_the_port(self, redirect_uri):
        client = _pre_registered(redirect_uris=[LOOPBACK_CALLBACK])
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl(redirect_uri))

    def test_a_sign_in_naming_no_redirect_uses_the_only_registered_one(self):
        client = _pre_registered(redirect_uris=[LOOPBACK_CALLBACK])
        assert str(client.validate_redirect_uri(None)) == LOOPBACK_CALLBACK

    def test_a_metadata_client_refusal_points_at_its_document(self, caplog):
        """Its redirects come from its document, so naming a config field would send the operator the wrong way."""
        client = McpPreRegisteredClient(client_id=CLIENT_METADATA_URL, redirect_uris=[AnyUrl(WEB_CALLBACK)])
        with caplog.at_level(logging.WARNING), pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl('http://localhost:3000/callback'))
        assert 'the redirect_uris of its metadata document' in caplog.text

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


class TestClientMetadataDirectory:
    """A metadata document URL signs in when listed or admitted as another client, with its document's redirects."""

    async def test_a_listed_document_client_keeps_its_listing_and_takes_its_name_and_redirects(self):
        listed = _pre_registered(scopes=('pets:write',), consent_required=False, client_id=CLIENT_METADATA_URL)
        directory = _directory([listed])
        client = await directory.get(CLIENT_METADATA_URL)
        assert client is not None
        assert (client.client_name, [str(uri) for uri in client.redirect_uris or []]) == (
            'Example Agent',
            ['http://127.0.0.1/callback'],
        )
        assert client.validate_scope(None) == ['pets:write']
        assert directory.requires_consent(CLIENT_METADATA_URL) is False

    async def test_another_client_on_a_trusted_host_is_granted_the_other_clients_scopes(self):
        directory = _directory(other_clients=_other_clients(trusted_hosts=frozenset({CLIENT_METADATA_HOST})))
        client = await directory.get(CLIENT_METADATA_URL)
        assert client is not None
        assert client.validate_scope(None) == ['pets:read']
        assert client.token_endpoint_auth_method == 'none'
        assert directory.requires_consent(CLIENT_METADATA_URL) is True

    @pytest.mark.parametrize(
        'other_clients', [None, McpOtherClients('', [], trusted_hosts=frozenset({'other.example'}))]
    )
    async def test_an_unlisted_document_client_without_a_rule_admitting_it_is_unknown(self, other_clients):
        """Nothing is fetched for it, so an unlisted URL cannot make the gateway reach out."""
        directory = _directory(other_clients=other_clients, resolver=_FailingResolver())
        assert await directory.get(CLIENT_METADATA_URL) is None
        assert await directory.admits(CLIENT_METADATA_URL) is False

    async def test_a_document_that_cannot_be_used_leaves_the_client_unknown(self, caplog):
        """The SDK then refuses the sign-in, and the log says why, which its own error does not."""
        directory = _directory(other_clients=_other_clients(), resolver=serving_resolver({}))
        with caplog.at_level(logging.WARNING):
            assert await directory.get(CLIENT_METADATA_URL) is None
        assert 'its metadata document answered HTTP 404' in caplog.text

    async def test_a_document_client_keeps_its_tokens_while_its_server_is_down(self):
        """Each MCP request reads the config alone, so it never waits on, or fails with, the client's server."""
        directory = _directory(other_clients=_other_clients(), resolver=_FailingResolver())
        assert await directory.admits(CLIENT_METADATA_URL) is True

    async def test_a_client_named_by_a_plain_id_is_found_where_it_is_listed_or_registered(self, memory_store):
        registry = McpDynamicClientRegistry(memory_store, GATEWAY_ISSUER)
        await registry.register(OAuthClientInformationFull(client_id='registered'))
        directory = _directory([_pre_registered()], registry=registry, resolver=_FailingResolver())
        assert [await directory.admits(client_id) for client_id in ('agent', 'registered', 'stranger')] == [
            True,
            True,
            False,
        ]
        registered = await directory.get('registered')
        assert registered is not None
        assert registered.client_id == 'registered'

    async def test_registration_is_open_only_with_a_registry(self, memory_store):
        closed = _directory()
        assert closed.build_registration_options(['api']).enabled is False
        with pytest.raises(RegistrationError):
            await closed.register(OAuthClientInformationFull(client_id='newcomer'))
        open_ = _directory(registry=McpDynamicClientRegistry(memory_store, GATEWAY_ISSUER))
        assert open_.build_registration_options(['api']).enabled is True
        await open_.register(OAuthClientInformationFull(client_id='newcomer'))
        assert await open_.admits('newcomer') is True

    @pytest.mark.parametrize(
        ('listed_id', 'other_clients', 'accepts'),
        [('agent', None, False), (CLIENT_METADATA_URL, None, True), ('agent', McpOtherClients('', []), True)],
    )
    def test_metadata_documents_are_announced_when_any_client_may_use_one(self, listed_id, other_clients, accepts):
        directory = _directory([_pre_registered(client_id=listed_id)], other_clients=other_clients)
        assert directory.accepts_client_metadata_documents is accepts


class _FailingResolver(McpClientMetadataResolver):
    """A resolver that fails the test if anything asks it to fetch a document."""

    async def resolve(self, client_id: str) -> ClientMetadataDocument:
        raise AssertionError(f'{client_id} was fetched')


def _directory(
    listed: list[McpPreRegisteredClient] | None = None,
    other_clients: McpOtherClients | None = None,
    resolver: McpClientMetadataResolver | None = None,
    registry: McpDynamicClientRegistry | None = None,
) -> McpClientMetadataDirectory:
    """A directory whose network serves the document of ``CLIENT_METADATA_URL`` unless ``resolver`` replaces it."""
    return McpClientMetadataDirectory(
        McpPreRegisteredClientDirectory(listed or []),
        other_clients,
        resolver or serving_resolver({CLIENT_METADATA_URL: client_metadata_document()}),
        registry,
    )


def _other_clients(trusted_hosts: frozenset[str] | None = None) -> McpOtherClients:
    return McpOtherClients(scope='pets:read', default_scopes=['pets:read'], trusted_hosts=trusted_hosts)


def _pre_registered(
    scopes: tuple[str, ...] = (),
    default_scopes: list[str] | None = None,
    redirect_uris: list[str] | None = None,
    consent_required: bool = True,
    client_id: str = 'agent',
) -> McpPreRegisteredClient:
    """A pre-registered client ``client_id`` holding ``scopes``, granted ``default_scopes`` or else all of them."""
    return McpPreRegisteredClient(
        client_id=client_id,
        redirect_uris=[AnyUrl(uri) for uri in redirect_uris] if redirect_uris else None,
        scope=' '.join(scopes),
        default_scopes=list(scopes if default_scopes is None else default_scopes),
        consent_required=consent_required,
    )
