import pytest

from openapi_mcp_gateway.auth.upstream.credentials import (
    UpstreamCredentialPlacement,
    UpstreamRequestCredentials,
    plan_upstream_credentials,
    schemes_met_without_credentials,
)
from openapi_mcp_gateway.openapi import OperationInfo


# The schemes a Trello-like API declares, two query keys needed together, beside a header key and an OAuth scheme.
SCHEMES = {
    'APIKey': {'type': 'apiKey', 'in': 'query', 'name': 'key'},
    'APIToken': {'type': 'apiKey', 'in': 'query', 'name': 'token'},
    'header_key': {'type': 'apiKey', 'in': 'header', 'name': 'X-API-Key'},
    'session': {'type': 'apiKey', 'in': 'cookie', 'name': 'sid'},
    'bearerAuth': {'type': 'http', 'scheme': 'bearer'},
    'basic': {'type': 'http', 'scheme': 'basic'},
    'oauth': {'type': 'oauth2', 'flows': {}},
}


class TestPlacement:
    """Where a scheme's credential travels comes from the scheme, never from the config."""

    @pytest.mark.parametrize(
        ('scheme', 'placed'),
        [
            ('APIKey', UpstreamRequestCredentials(query={'key': 'v'})),
            ('header_key', UpstreamRequestCredentials(headers={'X-API-Key': 'v'})),
            ('session', UpstreamRequestCredentials(cookies={'sid': 'v'})),
            ('bearerAuth', UpstreamRequestCredentials(headers={'Authorization': 'Bearer v'})),
        ],
    )
    def test_each_scheme_places_its_value_where_it_declares(self, scheme, placed):
        placement = UpstreamCredentialPlacement.from_scheme(SCHEMES[scheme])
        assert placement is not None
        assert placement.place('v') == placed

    @pytest.mark.parametrize('scheme', ['basic', 'oauth'])
    def test_a_scheme_no_static_value_serves_has_no_placement(self, scheme):
        assert UpstreamCredentialPlacement.from_scheme(SCHEMES[scheme]) is None


class TestCookieHeader:
    def test_the_credential_joins_the_cookies_already_sent(self):
        credentials = UpstreamRequestCredentials(cookies={'sid': 'secret'})
        assert credentials.merged_cookie_header('theme=dark; sid=stale') == 'theme=dark; sid=secret'

    def test_no_cookie_credential_leaves_the_header_as_it_was(self):
        assert UpstreamRequestCredentials().merged_cookie_header('theme=dark') == 'theme=dark'


class TestPlan:
    """Each operation settles at startup on the first alternative its configured credentials meet."""

    def test_two_keys_needed_together_both_ride(self):
        operation = _operation('getBoard', [{'APIKey': [], 'APIToken': []}])
        plan = _plan([operation], {'APIKey': 'k', 'APIToken': 't'})

        assert plan.credentials_for('getBoard') == UpstreamRequestCredentials(query={'key': 'k', 'token': 't'})

    def test_the_first_met_alternative_in_spec_order_is_used(self):
        """``getProductById`` takes an API key or an OAuth token, and the key comes first."""
        operation = _operation('getProductById', [{'header_key': []}, {'oauth': ['read']}])
        plan = _plan([operation], {'header_key': 'k'}, met_without_credentials=frozenset({'oauth'}))

        assert plan.credentials_for('getProductById') == UpstreamRequestCredentials(headers={'X-API-Key': 'k'})

    def test_an_alternative_a_flow_meets_carries_no_static_credential(self):
        operation = _operation('createProduct', [{'oauth': ['write']}])
        other = _operation('getInventory', [{'header_key': []}])
        plan = _plan([operation, other], {'header_key': 'k'}, met_without_credentials=frozenset({'oauth'}))

        assert plan.credentials_for('createProduct') == UpstreamRequestCredentials()

    def test_an_empty_alternative_yields_to_a_configured_credential(self):
        """``[{}, {key}]`` makes the key optional, so a configured key is sent."""
        operation = _operation('getProduct', [{}, {'header_key': []}])
        plan = _plan([operation], {'header_key': 'k'})

        assert plan.credentials_for('getProduct') == UpstreamRequestCredentials(headers={'X-API-Key': 'k'})

    def test_only_an_operation_its_credentials_serve_alone_leaves_the_flow_out(self):
        operations = [
            _operation('getInventory', [{'header_key': []}]),
            _operation('createProduct', [{'oauth': ['write']}]),
            _operation('getBoard', [{'header_key': [], 'oauth': []}]),
            _operation('listPublic', []),
        ]
        plan = _plan(operations, {'header_key': 'k'}, met_without_credentials=frozenset({'oauth'}))

        assert not plan.carries_flow_credential('getInventory')
        assert plan.carries_flow_credential('createProduct')
        assert plan.carries_flow_credential('getBoard')
        assert plan.carries_flow_credential('listPublic')

    def test_the_repr_leaves_the_values_out(self):
        plan = _plan([_operation('getInventory', [{'header_key': []}])], {'header_key': 'secret'})

        assert 'secret' not in repr(plan)
        assert 'secret' not in repr(plan.credentials_for('getInventory'))

    def test_an_operation_without_security_carries_nothing(self):
        plan = _plan([_operation('listPublic', [])], {})
        assert plan.credentials_for('listPublic') == UpstreamRequestCredentials()

    def test_an_operation_no_alternative_is_met_for_is_refused(self):
        operation = _operation('getBoard', [{'APIKey': [], 'APIToken': []}, {'oauth': []}])

        with pytest.raises(ValueError, match=r'meets the security of getBoard \(needs APIKey \+ APIToken or oauth\)'):
            _plan([operation], {'APIKey': 'k'})

    def test_a_credential_no_operation_uses_is_refused(self):
        operation = _operation('getBoard', [{'APIKey': []}])

        with pytest.raises(ValueError, match=r'credentials header_key serve no exposed operation'):
            _plan([operation], {'APIKey': 'k', 'header_key': 'h'})

    def test_a_credential_for_an_undeclared_scheme_is_refused(self):
        with pytest.raises(ValueError, match=r"names schemes the spec does not declare \['missing'\]"):
            _plan([], {'missing': 'k'})

    @pytest.mark.parametrize('scheme', ['basic', 'oauth'])
    def test_a_credential_for_a_scheme_no_static_value_serves_is_refused(self, scheme):
        with pytest.raises(ValueError, match=rf'credentials\.{scheme} serves a scheme no static value can'):
            _plan([], {scheme: 'k'})

    def test_a_credential_resolving_to_nothing_is_refused(self):
        with pytest.raises(ValueError, match=r'credentials APIKey resolve to nothing'):
            _plan([_operation('getBoard', [{'APIKey': []}])], {'APIKey': None})


class TestMetWithoutCredentials:
    """A flow's token or a forwarded header meets some schemes with no static value configured."""

    def test_a_flow_meets_the_bearer_schemes(self):
        assert schemes_met_without_credentials(SCHEMES, carries_bearer=True, forwarded_headers=()) == {
            'oauth',
            'bearerAuth',
        }

    def test_a_forwarded_authorization_meets_every_http_scheme(self):
        met = schemes_met_without_credentials(SCHEMES, carries_bearer=False, forwarded_headers=('authorization',))
        assert met == {'oauth', 'bearerAuth', 'basic'}

    def test_a_forwarded_header_meets_the_api_key_it_names(self):
        met = schemes_met_without_credentials(SCHEMES, carries_bearer=False, forwarded_headers=('x-api-key',))
        assert met == {'header_key'}


def _operation(operation_id: str, security: list[dict[str, list[str]]]) -> OperationInfo:
    return OperationInfo(operation_id=operation_id, method='get', path=f'/{operation_id}', security=security)


def _plan(operations, credentials, met_without_credentials=frozenset()):
    return plan_upstream_credentials('srv', operations, SCHEMES, credentials, met_without_credentials)
