import urllib.parse
from unittest.mock import AsyncMock, patch

import pytest

from openapi_mcp_gateway.auth.upstream.authorization_code import UpstreamTokenResponse
from tests.constants import (
    API_URL,
)
from tests.oauth import (
    begin_upstream,
    deliver_callback,
    sign_in,
    upstream_token_response,
)


@pytest.fixture
def audience_provider(make_provider):
    """Provider configured to name the upstream API the token is minted for."""
    return make_provider(audience_params={'audience': API_URL})


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


class TestUpstreamAudienceParams:
    """The audience naming the upstream API rides on every upstream request.

    Without it an authorization server mints a token for its own default audience,
    which an API that merely trusts that issuer will refuse.
    """

    async def test_authorize_url_carries_audience(self, audience_provider, mcp_client_info):
        """The browser redirect names the API, since consent binds the audience."""
        url = await begin_upstream(audience_provider, mcp_client_info)

        assert urllib.parse.urlencode({'audience': API_URL}) in url

    async def test_code_exchange_carries_audience(self, audience_provider, mcp_client_info):
        """The authorization_code grant names the API as well as the authorize request."""
        upstream_url = await begin_upstream(audience_provider, mcp_client_info)

        post_mock = AsyncMock(return_value=upstream_token_response())
        with patch('httpx.AsyncClient.post', post_mock):
            await deliver_callback(audience_provider, upstream_url)

        assert post_mock.await_args is not None
        posted = post_mock.await_args.kwargs['data']
        assert posted['grant_type'] == 'authorization_code'
        assert posted['audience'] == API_URL

    async def test_refresh_carries_audience(self, audience_provider, mcp_client_info, memory_store):
        """A refresh names the API too, so the rotated token stays usable upstream.

        Dropping the audience here would return a token the upstream refuses,
        and that failure would only surface once the first token expired.
        """
        # An upstream token about to expire, so the refresh path actually calls the token endpoint.
        token = await sign_in(audience_provider, mcp_client_info, upstream_expires_in=30)
        assert token.refresh_token is not None
        refresh = await audience_provider.load_refresh_token(mcp_client_info, token.refresh_token)
        assert refresh is not None

        post_mock = AsyncMock(return_value=upstream_token_response('rotated-access-token'))
        with patch('httpx.AsyncClient.post', post_mock):
            await audience_provider.exchange_refresh_token(mcp_client_info, refresh, ['read'])

        assert post_mock.await_args is not None
        posted = post_mock.await_args.kwargs['data']
        assert posted['grant_type'] == 'refresh_token'
        assert posted['audience'] == API_URL

    async def test_unconfigured_provider_sends_no_audience(self, provider, mcp_client_info):
        """An upstream that issues its own tokens sees no audience keys at all."""
        url = await begin_upstream(provider, mcp_client_info)
        assert 'audience=' not in url
        assert 'resource=' not in url

        post_mock = AsyncMock(return_value=upstream_token_response())
        with patch('httpx.AsyncClient.post', post_mock):
            await deliver_callback(provider, url)

        assert post_mock.await_args is not None
        posted = post_mock.await_args.kwargs['data']
        assert 'audience' not in posted
        assert 'resource' not in posted
