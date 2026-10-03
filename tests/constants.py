# The upstream authorization server.
# Its endpoints are built from the issuer, so a rule for how an issuer URL is formed lands in one place.
ISSUER = 'https://auth.example.com'
AUTHORIZE_URL = f'{ISSUER}/authorize'
TOKEN_URL = f'{ISSUER}/token'
JWKS_URL = f'{ISSUER}/jwks'

# The upstream APIs the gateway forwards to.
# ``PETSTORE_URL`` matches the ``servers`` entry of the petstore and client-credentials fixture specs.
API_URL = 'https://api.example.com'
PETSTORE_URL = 'https://petstore.example.com/v1'

# The gateway's own public address, which MCP clients reach and tokens are issued for,
# and the issuer it publishes for a server mounted at ``/petstore``.
GATEWAY_HOST = 'mcp.example.com'
GATEWAY_URL = f'https://{GATEWAY_HOST}'
GATEWAY_ISSUER = f'{GATEWAY_URL}/petstore'

# A web page allowed to call the gateway from a browser.
BROWSER_ORIGIN = 'https://app.example.com'

# An attacker's domain, which a test expects to be refused wherever it appears.
ATTACKER_HOST = 'evil.example.com'
ATTACKER_URL = f'https://{ATTACKER_HOST}'
