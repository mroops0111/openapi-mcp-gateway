# OpenAPI MCP Gateway

[![CI](https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/openapi-mcp-gateway.svg?v=1)](https://pypi.org/project/openapi-mcp-gateway/)
[![PyPI Downloads](https://static.pepy.tech/badge/openapi-mcp-gateway/month)](https://pepy.tech/projects/openapi-mcp-gateway)
[![Python Version](https://img.shields.io/pypi/pyversions/openapi-mcp-gateway.svg?v=1)](https://pypi.org/project/openapi-mcp-gateway/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Turn any OpenAPI (Swagger) spec into a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server with one command, or expose your existing FastAPI app the same way by decorating routes with `@mcp_tool`. Run several APIs from one process, each on its own mount path, with Bearer, API Key, and OAuth2 (`authorization_code` for per-user delegation, `client_credentials` for service tokens) auth built in. Works with Claude Desktop, Cursor, Cline, and any other MCP client.

```bash
openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json
# Server live at http://127.0.0.1:8000/api/mcp
```

- **Zero glue code.** Every operation in your spec becomes an MCP tool automatically.
- **Multi-API.** Expose GitHub, Slack, and internal services from one process, each on its own mount path.
- **Auth built in.** Bearer, API Key, and OAuth2, including per-user delegation (`authorization_code`) and service tokens (`client_credentials`).
- **FastAPI native.** Decorate routes with `@mcp_tool` to expose them as MCP tools in-process, with no extra network hop and no separate spec to maintain.
- **Flexible transport.** Streamable HTTP, SSE, or stdio for desktop clients.

---

## Installation

```bash
pip install openapi-mcp-gateway
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add openapi-mcp-gateway
```

Optional extras:

```bash
pip install "openapi-mcp-gateway[redis]"   # Redis token store, used for auth memoization
```

Requires Python 3.11+.

## Quick Start

### 1. Public API, No Auth

```bash
openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json --name petstore
```

Connect an MCP client to `http://127.0.0.1:8000/petstore/mcp`.

### 2. Bearer Token

```bash
export GITHUB_TOKEN="ghp_..."
openapi-mcp-gateway \
    --spec https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json \
    --name github \
    --auth-type bearer \
    --auth-token '${GITHUB_TOKEN}'
```

### 3. OAuth2, Per-User Delegation (`authorization_code`)

The gateway runs its own OAuth server so each MCP client authenticates as its own end-user; tokens are minted per session.

```bash
export ASANA_CLIENT_ID="..." ASANA_CLIENT_SECRET="..."
openapi-mcp-gateway \
    --spec https://raw.githubusercontent.com/Asana/openapi/master/defs/asana_oas.yaml \
    --name asana \
    --auth-type oauth2 \
    --auth-client-id '${ASANA_CLIENT_ID}' \
    --auth-client-secret '${ASANA_CLIENT_SECRET}' \
    --auth-scopes "openid,email,profile,users:read,workspaces:read"
```

### 4. OAuth2, Service Token (`client_credentials`)

When the gateway holds its own credentials and shares one upstream token across every MCP client. No per-user OAuth dance:

```bash
export SVC_CLIENT_ID="..." SVC_CLIENT_SECRET="..."
openapi-mcp-gateway \
    --spec ./service-api.json \
    --name svc \
    --auth-type oauth2 \
    --auth-flow client_credentials \
    --auth-client-id '${SVC_CLIENT_ID}' \
    --auth-client-secret '${SVC_CLIENT_SECRET}'
```

### 5. Multiple APIs at Once

Mix public, bearer, and OAuth2 services in a single config. Each server is mounted at `/{name}/mcp`:

```yaml
# servers.yml
host: "0.0.0.0"
port: 8000
url: http://localhost:8000   # public base URL for OAuth callbacks

servers:
  - name: petstore
    spec: https://petstore3.swagger.io/api/v3/openapi.json

  - name: github
    spec: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
    auth:
      type: bearer
      token: ${GITHUB_TOKEN}
    policy:
      allow: ["GET /repos/*", "GET /users/*"]
      deny:  ["GET /repos/*/actions/secrets*"]

  - name: asana
    spec: https://raw.githubusercontent.com/Asana/openapi/master/defs/asana_oas.yaml
    auth:
      type: oauth2
      client_id: ${ASANA_CLIENT_ID}
      client_secret: ${ASANA_CLIENT_SECRET}
      scopes: [openid, email, profile, users:read, workspaces:read]
```

```bash
export GITHUB_TOKEN="ghp_..."
export ASANA_CLIENT_ID="..." ASANA_CLIENT_SECRET="..."
openapi-mcp-gateway --config servers.yml
```

Runnable variants of this multi-server setup live in [`examples/`](examples/): [`multi-server.yml`](examples/multi-server.yml), [`asana.yml`](examples/asana.yml), [`github.yml`](examples/github.yml), [`petstore.yml`](examples/petstore.yml). Each YAML lists its prerequisites at the top.

`${ENV_VAR}` and `${ENV_VAR:-default}` work in any string field, resolved at request time. For OAuth2, `authorizationUrl` / `tokenUrl` / `scopes` are auto-detected from the spec's `securitySchemes`. Override `auth.authorization_url`, `auth.token_url`, or `auth.scopes` when the spec is incomplete.

### 6. Local Desktop Client (stdio)

For Claude Desktop, IDE integrations, or any MCP client that prefers stdio:

```json
{
  "mcpServers": {
    "petstore": {
      "command": "openapi-mcp-gateway",
      "args": ["--spec", "/abs/path/to/openapi.json", "--transport", "stdio"]
    }
  }
}
```

## Configuration

Run `openapi-mcp-gateway --help` for the CLI reference. The [Quick Start](#quick-start) examples cover most setups; the full field reference is below.

When values appear in more than one place, the rule is **defaults < YAML (`--config`) < CLI flags < `Gateway.run(...)` kwargs**, and a layer only overrides what it actually sets. Sub-trees (`logging`, per-server `auth`) merge field-by-field; the `servers` list is replaced wholesale.

<details>
<summary><b>Top-Level Fields</b></summary>

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | string | `0.0.0.0` | Bind address (`0.0.0.0` = all interfaces). Clients on the same machine usually open `http://localhost:{port}` or `http://127.0.0.1:{port}`. |
| `port` | int | `8000` | Bind port |
| `url` | string | *(empty)* | Public base URL for OAuth redirects and discovery. When unset: `http://localhost:{port}` if `host` is `0.0.0.0`, otherwise `http://{host}:{port}`. Override when your registered redirect URI uses another host (tunnel, reverse proxy, etc.). |
| `transport` | string | `streamable-http` | `sse`, `streamable-http`, or `stdio` |
| `store.type` | string | `memory` | `memory` or `redis` |
| `store.redis_url` | string | `redis://localhost:6379` | Redis URL when `store.type: redis` |
| `logging.level` | string | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `logging.format` | string | `text` | `text` or `json` |
| `logging.file` | string |  | Mirror logs to this file |
| `servers` | list | required | List of per-server config entries |

</details>

<details>
<summary><b>Per-Server Fields</b></summary>

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Unique identifier; mount path defaults to `/{name}` |
| `spec` | string | required | Path or URL to OpenAPI document (JSON or YAML) |
| `base_url` | string | from spec | Override the upstream base URL |
| `auth.type` | string | `none` | `none`, `bearer`, `api_key`, or `oauth2` |
| `auth.token` | string |  | Required for `bearer` / `api_key` |
| `auth.api_key_header` | string | `X-API-Key` | Header name for `api_key` |
| `auth.client_id`, `auth.client_secret` | string |  | Required for `oauth2` |
| `auth.scopes`, `auth.authorization_url`, `auth.token_url` |  | from spec | OAuth2 overrides when `securitySchemes` is incomplete |
| `policy.allow` | list |  | Only expose matching operations |
| `policy.deny` | list |  | Exclude matching operations |
| `timeout` | float | `90` | HTTP timeout in seconds |
| `exposure` | string | `static` | `static` registers one MCP tool per operation. `dynamic` registers three meta-tools (`list_operations`, `get_operation`, `call_operation`) for the LLM to walk on demand. |

</details>

### Filtering Operations

Use `policy.allow` and `policy.deny` with `fnmatch` syntax against operation IDs (`getUsers`, `create*`) or method + path (`GET /users/*`):

```yaml
policy:
  allow: ["GET /repos/*"]
  deny:  ["GET /repos/*/actions/secrets*"]
```

Or mark operations directly in the spec and enable `marked_only`:

```yaml
# openapi.yml
paths:
  /users:
    get:
      operationId: listUsers
      x-mcp-integration:
        expose:
          tool: {}
```
```yaml
# servers.yml
policy:
  marked_only: true
```

Filters apply in order: `marked_only`, then `allow`, then `deny`.

### Dynamic Exposure

For APIs with hundreds of operations (GitHub, Stripe, etc.), registering every operation as its own MCP tool can blow the LLM's context window before the agent does anything. Flip the server to `exposure: dynamic` and the client sees three meta-tools instead:

```yaml
servers:
  - name: github
    spec: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
    exposure: dynamic   # default is 'static'
    auth:
      type: bearer
      token: ${GITHUB_TOKEN}
```

The three meta-tools:

- `list_operations()` returns `[{name, description}, ...]` for every operation on this server.
- `get_operation(name)` returns one operation's JSON Schema for input arguments.
- `call_operation(name, arguments)` invokes that operation against the upstream.

The LLM walks `list → get → call` to discover and invoke operations on demand. Auth, path templating, and per-operation request shape are identical to static mode; the only thing that changes is how the operations are surfaced to the client.

`exposure` is per-server, so `/github/mcp` can run `dynamic` while `/petstore/mcp` runs `static` in the same process.

### Logging

Configure via the `logging.*` YAML keys or via CLI flags (`--log-level`, `--log-format`, `--log-file`); `-v` and `-q` are shortcuts for `DEBUG` and `WARNING`. CLI flags override YAML field-by-field, following the precedence rule above.

## Python API

Use the gateway as a library inside your own Python application:

```python
from openapi_mcp_gateway import Gateway

gateway = Gateway()
gateway.add_server(
    name="petstore",
    spec="https://petstore3.swagger.io/api/v3/openapi.json",
)
gateway.add_server(
    name="github",
    spec="./github-openapi.json",
    auth={"type": "bearer", "token": "${GITHUB_TOKEN}"},
    policy={"allow": ["GET /repos/*"]},
)
gateway.run(port=8000)
```

### Expose Your FastAPI App as MCP Tools

Already running FastAPI? Decorate the routes you want to expose with `@mcp_tool` and the gateway picks them up. No second spec, no separate process. Routes run in-process via `httpx.ASGITransport`, so there is no extra network hop:

```python
from fastapi import FastAPI
from openapi_mcp_gateway import Gateway, mcp_tool

app = FastAPI()

@app.get("/items/{item_id}")
@mcp_tool()
def read_item(item_id: int):
    return {"id": item_id}

@app.get("/internal/health")  # not decorated → not exposed
def health():
    return {"ok": True}

Gateway.from_fastapi(app, name="myapp").run()
```

Auth is auto-detected from the app's `securitySchemes`. If the gateway and the app share an OAuth realm, the MCP client's `Authorization` header passes through verbatim; for `client_credentials` schemes the gateway mints upstream tokens from its own credentials. Mix and match by passing an explicit `auth=AuthConfig(...)` to `Gateway.from_fastapi`.

Got routes you can't decorate at definition (third-party app, `include_router` from another package)? Use `mark_tool(func)` to attach the same metadata imperatively.

To mount the MCP routes onto an existing FastAPI app instead of running standalone, use `Gateway.mount(app)`.

## License

[MIT](LICENSE)
