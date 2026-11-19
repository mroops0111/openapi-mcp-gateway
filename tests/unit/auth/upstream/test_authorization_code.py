import pytest

from openapi_mcp_gateway.auth.upstream.authorization_code import UpstreamTokenResponse


class TestGrantedScopes:
    """The upstream token response's ``scope`` is read as RFC 6749 states it."""

    @pytest.mark.parametrize(
        ('scope', 'granted'),
        [
            pytest.param('read:pets write:pets', ['read:pets', 'write:pets'], id='space-delimited'),
            pytest.param(None, ['previous'], id='omitted means unchanged'),
            pytest.param('', [], id='empty grants nothing'),
            pytest.param('read:user,repo', ['read:user,repo'], id='a comma belongs to the scope'),
        ],
    )
    def test_the_granted_scope_is_parsed_per_rfc_6749(self, scope, granted):
        response = UpstreamTokenResponse(access_token='token', refresh_token=None, expires_in=3600, scope=scope)
        assert response.resolve_granted_scopes(['previous']) == granted
