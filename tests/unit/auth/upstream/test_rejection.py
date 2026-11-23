import httpx
import pytest

from openapi_mcp_gateway.auth.upstream.rejection import UpstreamRejection, read_upstream_rejection


class TestReadUpstreamRejection:
    """Only a standard RFC 6750 Bearer challenge says a new upstream sign-in can cure a refusal."""

    @pytest.mark.parametrize(
        ('status', 'challenge', 'expected'),
        [
            # Google, Spotify and Box answer an expired or revoked token like this.
            (
                401,
                'Bearer realm="https://accounts.google.com/", error="invalid_token"',
                UpstreamRejection('invalid_token'),
            ),
            (
                403,
                'Bearer error="insufficient_scope", scope="repo read:org", error_description="Needs \\"repo\\""',
                UpstreamRejection('insufficient_scope', ('repo', 'read:org')),
            ),
            (401, 'bearer error=invalid_token', UpstreamRejection('invalid_token')),
            (
                403,
                'Basic realm="api", Bearer error="insufficient_scope", scope=write',
                UpstreamRejection('insufficient_scope', ('write',)),
            ),
            # HTTP counts a tab as whitespace too.
            (
                403,
                'Bearer\terror="insufficient_scope",\tscope="write"',
                UpstreamRejection('insufficient_scope', ('write',)),
            ),
        ],
    )
    def test_a_curable_challenge_is_read(self, status, challenge, expected):
        assert read_upstream_rejection(_response(status, challenge)) == expected

    @pytest.mark.parametrize(
        ('status', 'challenge'),
        [
            # Dropbox and Microsoft Graph name no error.
            (401, 'Bearer realm="Dropbox-API"'),
            (401, 'Bearer realm="", authorization_uri="https://login.microsoftonline.com/common/oauth2/authorize"'),
            # The error code arrives with a status RFC 6750 does not pair it with.
            (403, 'Bearer error="invalid_token"'),
            (401, 'Bearer error="insufficient_scope"'),
            (400, 'Bearer error="invalid_request"'),
            (401, 'Basic realm="api", error="invalid_token"'),
            (401, 'Bearer realm="api", Basic error="invalid_token"'),
            (401, None),
        ],
    )
    def test_any_other_refusal_is_not(self, status, challenge):
        assert read_upstream_rejection(_response(status, challenge)) is None

    def test_a_later_header_is_read_when_the_first_is_not_bearer(self):
        response = httpx.Response(
            401,
            headers=[('WWW-Authenticate', 'Basic realm="api"'), ('WWW-Authenticate', 'Bearer error="invalid_token"')],
        )

        assert read_upstream_rejection(response) == UpstreamRejection('invalid_token')


def _response(status: int, challenge: str | None) -> httpx.Response:
    return httpx.Response(status, headers={'WWW-Authenticate': challenge} if challenge is not None else {})
