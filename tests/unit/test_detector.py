from openapi_mcp_gateway.auth.detector import (
    detect_oauth_flows,
    detect_primary_oauth_flow,
    detect_unsupported_oauth_flows,
)
from openapi_mcp_gateway.openapi import OpenAPISpec


def _spec(security_schemes: dict) -> OpenAPISpec:
    """Build a minimal ``OpenAPISpec`` carrying the given security schemes."""
    return OpenAPISpec(raw={}, security_schemes=security_schemes)


AUTH_CODE_SCHEME = {
    'type': 'oauth2',
    'flows': {
        'authorizationCode': {
            'authorizationUrl': 'https://auth.example.com/authorize',
            'tokenUrl': 'https://auth.example.com/token',
            'scopes': {'read': 'Read', 'write': 'Write'},
        },
    },
}


CLIENT_CREDS_SCHEME = {
    'type': 'oauth2',
    'flows': {
        'clientCredentials': {
            'tokenUrl': 'https://auth.example.com/token',
            'scopes': {'api': 'API access'},
        },
    },
}


class TestDetectOAuthFlows:
    """Enumerate every OAuth2 flow declared under ``securitySchemes``."""

    def test_no_security_schemes(self):
        """A spec without security schemes yields an empty list."""
        assert detect_oauth_flows(_spec({})) == []

    def test_skips_non_oauth_schemes(self):
        """Schemes with a non-oauth2 type (e.g. ``http``) are ignored."""
        spec = _spec({'bearerAuth': {'type': 'http', 'scheme': 'bearer'}})
        assert detect_oauth_flows(spec) == []

    def test_authorization_code_flow(self):
        """An ``authorizationCode`` block is surfaced with its URLs and scopes."""
        flows = detect_oauth_flows(_spec({'oauth2': AUTH_CODE_SCHEME}))
        assert len(flows) == 1
        flow = flows[0]
        assert flow.flow_type == 'authorization_code'
        assert flow.authorization_url == 'https://auth.example.com/authorize'
        assert flow.token_url == 'https://auth.example.com/token'
        assert flow.scopes == {'read': 'Read', 'write': 'Write'}

    def test_client_credentials_flow(self):
        """A ``clientCredentials`` block is surfaced without an authorization URL."""
        flows = detect_oauth_flows(_spec({'oauth2': CLIENT_CREDS_SCHEME}))
        assert len(flows) == 1
        flow = flows[0]
        assert flow.flow_type == 'client_credentials'
        assert flow.authorization_url is None
        assert flow.token_url == 'https://auth.example.com/token'

    def test_both_flows_in_one_scheme(self):
        """Both flows declared under one scheme are returned independently."""
        scheme = {
            'type': 'oauth2',
            'flows': {**AUTH_CODE_SCHEME['flows'], **CLIENT_CREDS_SCHEME['flows']},
        }
        flows = detect_oauth_flows(_spec({'oauth2': scheme}))
        types = sorted(flow.flow_type for flow in flows)
        assert types == ['authorization_code', 'client_credentials']


class TestDetectPrimaryOAuthFlow:
    """Pick a single representative flow, preferring ``authorization_code``."""

    def test_returns_none_without_oauth(self):
        """Specs with no OAuth2 flows resolve to ``None``."""
        assert detect_primary_oauth_flow(_spec({})) is None

    def test_prefers_authorization_code(self):
        """``authorization_code`` wins over ``client_credentials`` even when listed second."""
        scheme = {
            'type': 'oauth2',
            'flows': {**CLIENT_CREDS_SCHEME['flows'], **AUTH_CODE_SCHEME['flows']},
        }
        primary = detect_primary_oauth_flow(_spec({'oauth2': scheme}))
        assert primary is not None
        assert primary.flow_type == 'authorization_code'

    def test_falls_back_to_first_when_no_auth_code(self):
        """Without an ``authorization_code`` flow, the first detected flow is returned."""
        primary = detect_primary_oauth_flow(_spec({'oauth2': CLIENT_CREDS_SCHEME}))
        assert primary is not None
        assert primary.flow_type == 'client_credentials'


class TestDetectUnsupportedOAuthFlows:
    """``detect_unsupported_oauth_flows`` lists OAuth2 flow names the gateway does not implement."""

    def test_empty_when_only_supported_flows(self):
        """Schemes with only authorizationCode/clientCredentials produce no unsupported flows."""
        scheme = {
            'type': 'oauth2',
            'flows': {**AUTH_CODE_SCHEME['flows'], **CLIENT_CREDS_SCHEME['flows']},
        }
        assert detect_unsupported_oauth_flows(_spec({'oauth2': scheme})) == []

    def test_lists_password_and_implicit(self):
        """``password`` and ``implicit`` are surfaced for callers to reject."""
        scheme = {
            'type': 'oauth2',
            'flows': {
                'password': {'tokenUrl': 'https://auth.example.com/token', 'scopes': {}},
                'implicit': {'authorizationUrl': 'https://auth.example.com/authorize', 'scopes': {}},
            },
        }
        unsupported = detect_unsupported_oauth_flows(_spec({'oauth2': scheme}))
        assert sorted(unsupported) == ['implicit', 'password']

    def test_ignores_non_oauth_schemes(self):
        """Non-oauth2 schemes are ignored when looking for unsupported flows."""
        spec = _spec({'apiKey': {'type': 'apiKey', 'in': 'header', 'name': 'X-API-Key'}})
        assert detect_unsupported_oauth_flows(spec) == []
