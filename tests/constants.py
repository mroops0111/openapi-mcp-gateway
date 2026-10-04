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

# The gateway's own public URL, which MCP clients reach and tokens are issued for.
GATEWAY_URL = 'https://mcp.example.com'
