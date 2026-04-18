# OpenAPI MCP Gateway

Turn any OpenAPI specification into a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server with a single command. Supports multiple APIs simultaneously.

## Features

- **One-line startup** - Point to an OpenAPI spec and get a running MCP server
- **Multi-server** - Serve multiple APIs from a single gateway via config file
- **OAuth2 support** - Full authorization code flow with automatic upstream token management
- **Policy controls** - Allow/deny patterns to filter which endpoints are exposed
- **Auth support** - Bearer token, API key, or OAuth2 with environment variable resolution
- **`x-mcp-integration` markers** - Fine-grained control over which operations become tools
- **OAuth discovery** - `.well-known` endpoints (RFC 8414 / RFC 9728) for each server
- **Flexible transport** - SSE, Streamable HTTP, or stdio (for Claude Desktop / IDE)
- **Python API** - Use as a library in your own FastAPI application

## Installation

```bash
pip install openapi-mcp-gateway
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add openapi-mcp-gateway
```

## Quick Start

### CLI - Single Spec

```bash
# From a URL
openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json

# From a local file
openapi-mcp-gateway --spec ./my-api.json

# With base URL override
openapi-mcp-gateway --spec ./my-api.json --base-url https://api.example.com
```

### CLI - Multiple Servers

Create a `servers.yml`:

```yaml
host: "0.0.0.0"
port: 8000
url: https://mcp.example.com    # public-facing URL for OAuth discovery

servers:
  - name: petstore
    spec: https://petstore3.swagger.io/api/v3/openapi.json
    base_url: https://petstore3.swagger.io/api/v3

  - name: github
    spec: ./github-openapi.json
    base_url: https://api.github.com
    path_prefix: gh              # mount at /gh instead of /github
    auth:
      type: bearer
      token_env: GITHUB_TOKEN
    policy:
      allow:
        - "GET /repos/*"
        - "GET /users/*"
```

```bash
openapi-mcp-gateway --config servers.yml
```

Each server is mounted at its own path: `/petstore/mcp`, `/gh/mcp`.

### Claude Desktop / IDE Integration (stdio)

```bash
openapi-mcp-gateway --spec ./my-api.json --transport stdio
```

Or in your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "my-api": {
      "command": "openapi-mcp-gateway",
      "args": ["--spec", "/path/to/openapi.json", "--transport", "stdio"]
    }
  }
}
```

### Python API

```python
from openapi_mcp_gateway import Gateway

gateway = Gateway()

gateway.add_server(
    name="petstore",
    spec="https://petstore3.swagger.io/api/v3/openapi.json",
    base_url="https://petstore3.swagger.io/api/v3",
)

gateway.add_server(
    name="github",
    spec="./github-openapi.json",
    base_url="https://api.github.com",
    path_prefix="gh",
    auth={"type": "bearer", "token_env": "GITHUB_TOKEN"},
    policy={"allow": ["GET /repos/*"]},
)

# Run standalone
gateway.run(port=8000)
```

Or mount into an existing FastAPI app:

```python
from fastapi import FastAPI
from openapi_mcp_gateway import Gateway

app = FastAPI()
gateway = Gateway()
gateway.add_server(name="petstore", spec="petstore.json")
gateway.mount(app)
```

## OAuth2 Support

For APIs that require OAuth2, the gateway acts as an intermediary:
- MCP clients authenticate with the **gateway** (MCP OAuth)
- The gateway authenticates with the **upstream API** (upstream OAuth)
- Tokens are mapped and managed automatically

The gateway auto-detects OAuth2 flows from the OpenAPI spec's `securitySchemes`. You only need to provide `client_id` and `client_secret`.

```yaml
servers:
  - name: my-saas
    spec: ./my-saas-openapi.json
    auth:
      type: oauth2
      client_id_env: MY_SAAS_CLIENT_ID
      client_secret_env: MY_SAAS_CLIENT_SECRET
      scopes:
        - read
        - write
```

### OAuth2 Flow

```
MCP Client → Gateway (OAuth Server) → Upstream API (OAuth Provider)
     │              │                        │
     │ 1. Connect   │                        │
     │ ──────────>  │                        │
     │              │ 2. Redirect to upstream │
     │ <──────────  │                        │
     │              │                        │
     │ 3. User authorizes ──────────────────>│
     │              │                        │
     │              │ 4. Callback + exchange  │
     │              │ <──────────────────────│
     │              │                        │
     │ 5. MCP token │                        │
     │ <──────────  │                        │
     │              │                        │
     │ 6. Tool call │ 7. API call with       │
     │ ──────────>  │    upstream token       │
     │              │ ──────────────────────> │
     │ 8. Result    │                        │
     │ <──────────  │ <──────────────────────│
```

## Configuration

### Gateway (top-level)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `0.0.0.0` | Bind host |
| `port` | int | `8000` | Bind port |
| `url` | string | `http://{host}:{port}` | Public-facing URL (for OAuth discovery) |
| `transport` | string | `streamable-http` | `sse`, `streamable-http`, or `stdio` |
| `debug` | bool | `false` | Enable debug mode |
| `enable_docs` | bool | `false` | Enable `/docs` and `/redoc` |
| `cors.allow_origins` | list | `["*"]` | CORS allowed origins |
| `cors.allow_methods` | list | `["*"]` | CORS allowed methods |
| `cors.allow_headers` | list | `["*"]` | CORS allowed headers |
| `cors.expose_headers` | list | `["*"]` | CORS exposed headers |
| `store.type` | string | `memory` | Token store backend: `memory` or `redis` |
| `store.redis_url` | string | `redis://localhost:6379` | Redis connection URL (when `store.type` is `redis`) |
| `store.key_prefix` | string | `mcp_gw` | Key prefix for Redis keys |

### Server Entry

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Unique name for this server |
| `spec` | string | required | Path or URL to OpenAPI spec (JSON/YAML) |
| `base_url` | string | from spec | Override upstream API base URL |
| `path_prefix` | string | `{name}` | Override mount path (default: `/{name}`) |
| `auth.type` | string | `none` | `bearer`, `api_key`, `oauth2`, or `none` |
| `auth.token` | string | - | Static token value |
| `auth.token_env` | string | - | Environment variable for the token |
| `auth.api_key_header` | string | `X-API-Key` | Header name for API key auth |
| `auth.client_id` | string | - | OAuth2 client ID |
| `auth.client_id_env` | string | - | Environment variable for OAuth2 client ID |
| `auth.client_secret` | string | - | OAuth2 client secret |
| `auth.client_secret_env` | string | - | Environment variable for OAuth2 client secret |
| `auth.scopes` | list | from spec | OAuth2 scopes to request |
| `policy.allow` | list | - | Only expose matching operations |
| `policy.deny` | list | - | Exclude matching operations |
| `policy.marked_only` | bool | `false` | Only expose ops with `x-mcp-integration` |
| `timeout` | float | `90` | HTTP request timeout (seconds) |

### Policy Patterns

Patterns use `fnmatch` syntax:

- **Operation ID**: `"getUsers"`, `"create*"`, `"*User*"`
- **METHOD path**: `"GET /users/*"`, `"POST /api/*"`, `"DELETE *"`

### `x-mcp-integration` Marker

Add this to individual operations in your OpenAPI spec to mark them for exposure:

```yaml
paths:
  /users:
    get:
      operationId: listUsers
      x-mcp-integration:
        expose:
          tool: {}
```

Then set `policy.marked_only: true` in your config to only expose marked operations.

## OAuth Discovery

The gateway automatically serves `.well-known` endpoints for each server:

- `GET /.well-known/oauth-authorization-server/{server_name}` (RFC 8414)
- `GET /.well-known/oauth-protected-resource/{server_name}/mcp` (RFC 9728)

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full class diagram and design patterns used.

## CLI Reference

```
Usage: openapi-mcp-gateway [OPTIONS]

Options:
  --spec TEXT                     Path or URL to a single OpenAPI spec.
  --config PATH                  Path to a YAML config file.
  --name TEXT                    Server name when using --spec (default: api).
  --base-url TEXT                Override the upstream API base URL.
  --transport [sse|streamable-http|stdio]
                                 MCP transport protocol.
  --host TEXT                    Bind host (default: 0.0.0.0).
  --port INTEGER                 Bind port (default: 8000).
  --help                         Show this message and exit.
```

## License

MIT
