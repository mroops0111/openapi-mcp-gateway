# OpenAPI MCP Gateway

[![CI](https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/openapi-mcp-gateway.svg?v=1)](https://pypi.org/project/openapi-mcp-gateway/)
[![PyPI Downloads](https://static.pepy.tech/badge/openapi-mcp-gateway/month)](https://pepy.tech/projects/openapi-mcp-gateway)
[![Python Version](https://img.shields.io/pypi/pyversions/openapi-mcp-gateway.svg?v=1)](https://pypi.org/project/openapi-mcp-gateway/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Mount any OpenAPI (Swagger) spec as a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server, or expose your existing FastAPI app the same way. Several APIs in one process, each on its own mount path with its own auth.

```bash
uvx openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json
# Server live at http://127.0.0.1:8000/api/mcp
```

- **Multi-spec, multi-auth.** Mount GitHub, an OAuth2 SaaS, and your internal API in one process. Each `(server, user)` pair has its own token namespace, no cross-talk. Bearer, API key, OAuth2 `authorization_code` for end-user delegation, and `client_credentials` for service flows all coexist.
- **FastAPI native, route-level.** Decorate individual routes with `@mcp_tool` to opt them in one by one, no whole-app exposure. Routes run in-process via `httpx.ASGITransport`, no extra network hop and no second spec to maintain.
- **Dynamic exposure.** For specs with hundreds of operations that blow the LLM context window, flip a server to `exposure: dynamic` and the agent walks `list → get → call` meta-tools on demand.
- **Resource auto-promotion.** Set `mode: auto` and the gateway promotes eligible GETs to MCP resources instead of tools, so the LLM's tool list stays small and addressable reads attach by URI on demand. Layer per-operation overrides from YAML when you do not own the upstream spec.
- **Spec-compliant authorization.** Audience-bound tokens, no silent passthrough to third-party upstreams [[MCP Authorization Spec: Access Token Privilege Restriction](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization#access-token-privilege-restriction)].
- **Pluggable token store.** Memory by default. Switch to Redis when you need to share state across replicas.

Streamable HTTP, SSE, and stdio all supported on the same binary. Works with Claude Desktop, Cursor, or any other MCP client.

---

## Installation

Add the gateway to your project with [uv](https://docs.astral.sh/uv/):

```bash
uv add openapi-mcp-gateway
```

Optional extras:

```bash
uv add "openapi-mcp-gateway[redis]"   # Redis token store, used for auth memoization
```

Requires Python 3.11+.

## Quick Start

### 1. Public API, No Auth

```bash
# `uvx` runs the published package without installing it into your project;
# swap in `uv run` once you've added the gateway as a dependency.
uvx openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json --name petstore
```

Connect an MCP client to `http://127.0.0.1:8000/petstore/mcp`.

### 2. Bearer Token

```bash
export GITHUB_TOKEN="ghp_..."
uv run openapi-mcp-gateway \
    --spec https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json \
    --name github \
    --auth-type bearer \
    --auth-token '${GITHUB_TOKEN}'
```

### 3. OAuth2, Per-User Delegation (`authorization_code`)

The gateway runs its own OAuth server so each MCP client authenticates as its own end-user; tokens are minted per session.

```bash
export ASANA_CLIENT_ID="..." ASANA_CLIENT_SECRET="..."
uv run openapi-mcp-gateway \
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
uv run openapi-mcp-gateway \
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
host: "127.0.0.1"
port: 8000
url: http://127.0.0.1:8000   # public base URL for OAuth callbacks

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
uv run openapi-mcp-gateway --config servers.yml
```

Runnable variants live in [`examples/`](examples/); each YAML lists prerequisites at the top.

`${ENV_VAR}` and `${ENV_VAR:-default}` work in any string field, resolved at request time. For OAuth2, `authorizationUrl` / `tokenUrl` / `scopes` are auto-detected from the spec's `securitySchemes`; override with `auth.authorization_url` / `auth.token_url` / `auth.scopes` when the spec is incomplete.

### 6. Local Desktop Client (stdio)

For Claude Desktop, IDE integrations, or any MCP client that prefers stdio:

```json
{
  "mcpServers": {
    "petstore": {
      "command": "uv",
      "args": [
        "run",
        "--project", "/abs/path/to/your/project",
        "openapi-mcp-gateway",
        "--spec", "/abs/path/to/openapi.json",
        "--transport", "stdio"
      ]
    }
  }
}
```

## Configuration

Run `uv run openapi-mcp-gateway --help` for the CLI reference. The [Quick Start](#quick-start) examples cover most setups; the full field reference is below.

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
| `mode` | string | `tool_only` | `tool_only` forces every operation to a tool and ignores any `expose.resource` declaration. `auto` promotes eligible GETs (no required non-path parameter) to MCP resources; spec-side `expose.resource` opt-ins still apply as explicit overrides. |
| `operations` | map | `{}` | YAML-side `x-mcp-integration` overrides, keyed by `operationId`. Fully replaces (does not merge) the spec-side `x-mcp-integration` on that operation. Useful when you do not control the upstream spec. |

</details>

### Filtering Operations

Use `policy.allow` and `policy.deny` with `fnmatch` syntax against operation IDs (`getUsers`, `create*`) or method + path (`GET /users/*`):

```yaml
policy:
  allow: ["GET /repos/*"]
  deny:  ["GET /repos/*/actions/secrets*"]
```

Operations can also be opted in from the spec side with `x-mcp-integration: {expose: {tool: {}}}` plus `policy.marked_only: true`. Filters apply in order: `marked_only`, then `allow`, then `deny`.

### Resource Exposure

`GET` operations that return addressable read-only data are a better fit for the MCP **resource** primitive than for a tool. Most MCP clients do not auto-load resources into the LLM context, so promoting catalog-style endpoints to resources saves tokens without losing reachability.

The default `mode: tool_only` exposes every operation as a tool. Switch to `mode: auto` to auto-promote eligible GETs (no required `query` / `header` / `body` parameter) to MCP resources:

```yaml
servers:
  - name: petstore
    spec: https://petstore3.swagger.io/api/v3/openapi.json
    mode: auto
```

That alone is enough for the common case. Against the vanilla Petstore3 spec it produces 13 tools, 3 concrete resources, and 3 resource templates with zero spec edits.

For finer per-operation control (rename the resource, set a custom URI template, set a non-JSON MIME type), use the `operations` map in YAML:

```yaml
servers:
  - name: petstore
    spec: https://petstore3.swagger.io/api/v3/openapi.json
    mode: auto
    operations:
      getPetById:
        expose:
          resource:
            name: pet
            mime_type: application/json
      getInventory:
        expose:
          resource:
            name: inventory
```

The `operations` keys are matched against `operationId` in the spec; an unknown id raises at startup so typos do not silently no-op. Each entry fully replaces (does not merge with) the spec-side `x-mcp-integration` for that operation. A runnable demo lives at [`examples/petstore-override.yml`](examples/petstore-override.yml).

If you own the upstream spec, you can write the same opt-in inline with `x-mcp-integration.expose.resource`:

```yaml
paths:
  /pets/{petId}:
    get:
      operationId: getPet
      x-mcp-integration:
        expose:
          resource:
            name: pet
            mime_type: application/json
            # uri_template: petstore://v2/pets/{petId}  # optional override; must start with "<server>://"
```

Declaring both `expose.tool` and `expose.resource` registers the operation in both surfaces. Resource reads are validated at startup: non-`GET` methods, required non-path parameters, and `uri_template` overrides that do not start with `<server>://` all abort `Gateway.from_config` with a concrete error. Subscriptions are not implemented because REST has no native push.

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

Auth is auto-detected from the app's `securitySchemes`. Override by passing an explicit `auth=AuthConfig(...)` to `Gateway.from_fastapi`.

<details>
<summary>How auth works for the FastAPI integration</summary>

Because the gateway runs in-process and routes through `httpx.ASGITransport`, gateway and upstream share the same OAuth audience, so the MCP client's `Authorization` header passes through verbatim (`auth.flow: passthrough`, set automatically for this integration only). For `client_credentials` schemes the gateway mints upstream tokens from its own credentials instead.

</details>

## License

[MIT](LICENSE)
