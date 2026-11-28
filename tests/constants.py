# The upstream authorization server.
# Its endpoints are built from the issuer, so a rule for how an issuer URL is formed lands in one place.
ISSUER = 'https://auth.example.com'
AUTHORIZE_URL = f'{ISSUER}/authorize'
TOKEN_URL = f'{ISSUER}/token'
JWKS_URL = f'{ISSUER}/jwks'

# The upstream APIs the gateway forwards to.
# ``SHOP_URL`` matches the ``servers`` entry of the shop and client-credentials fixture specs.
API_URL = 'https://api.example.com'
SHOP_URL = 'https://shop.example.com/v1'

# The gateway's own public address, which MCP clients reach and tokens are issued for,
# and the issuer it publishes for a server mounted at ``/shop``, with that server's MCP endpoint.
GATEWAY_HOST = 'mcp.example.com'
GATEWAY_URL = f'https://{GATEWAY_HOST}'
GATEWAY_ISSUER = f'{GATEWAY_URL}/shop'
GATEWAY_RESOURCE = f'{GATEWAY_ISSUER}/mcp'

# The one credential a shared-credential server sends upstream on every call.
# The shop fixture declares no scheme it fits, so the server declares a bearer one as the root security.
UPSTREAM_TOKEN = 'upstream-pat'
SHARED_CREDENTIAL_SCHEME = 'upstreamToken'
SHARED_CREDENTIAL_SECURITY = {
    'security_schemes': {SHARED_CREDENTIAL_SCHEME: {'type': 'http', 'scheme': 'bearer'}},
    'security': [{SHARED_CREDENTIAL_SCHEME: []}],
}
SHARED_CREDENTIAL_UPSTREAM = {'credentials': {SHARED_CREDENTIAL_SCHEME: {'value': UPSTREAM_TOKEN}}}

# A web page allowed to call the gateway from a browser.
BROWSER_ORIGIN = 'https://app.example.com'

# An attacker's domain, which a test expects to be refused wherever it appears.
ATTACKER_HOST = 'evil.example.com'
ATTACKER_URL = f'https://{ATTACKER_HOST}'
