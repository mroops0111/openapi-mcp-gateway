# OpenAPI MCP Gateway

[![CI](https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/openapi-mcp-gateway.svg?v=1)](https://pypi.org/project/openapi-mcp-gateway/)
[![PyPI Downloads](https://static.pepy.tech/badge/openapi-mcp-gateway/month)](https://pepy.tech/projects/openapi-mcp-gateway)
[![Python Version](https://img.shields.io/pypi/pyversions/openapi-mcp-gateway.svg?v=1)](https://pypi.org/project/openapi-mcp-gateway/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Mount any OpenAPI (Swagger) spec as a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server, or expose an existing FastAPI app the same way. Multiple APIs in one process, each with its own mount path and auth.

```bash
uvx openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json
# Server live at http://127.0.0.1:8000/api/mcp
```

- **Multi-spec, multi-auth.** Mount GitHub, an OAuth2 SaaS, and your internal API side-by-side. Bearer, API key, OAuth2 `authorization_code` (per-user delegation), and `client_credentials` (service flows) coexist, with each `(server, user)` pair scoped to its own token namespace.
- **FastAPI native, route-level.** Decorate routes with `@mcp_tool` to opt in, no whole-app exposure. Calls run in-process via `httpx.ASGITransport`, no extra network hop and no second spec to maintain.
- **Dynamic exposure.** For specs whose operation count would blow the LLM context window, set `exposure: dynamic` and the agent walks `list → get → call` meta-tools on demand.
- **Resource auto-promotion.** Set `mode: auto` and eligible GETs register as MCP resources instead of tools, so the tool list stays small while reads remain addressable by URI. Layer per-operation overrides in YAML when you do not own the upstream spec.
- **Spec-compliant authorization.** Audience-bound tokens, no silent passthrough to third-party upstreams [[MCP Authorization Spec: Access Token Privilege Restriction](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization#access-token-privilege-restriction)]. Tools emit protocol-native `title`, `annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`), and `structuredContent` so the agent reads structured error bodies without re-parsing text.
- **Tool name and description overrides.** Rewrite ugly `operationId`s and empty descriptions in YAML when you do not own the upstream spec, no fork required.
- **Pluggable token store.** Memory by default. Switch to Redis when you need to share state across replicas.

Streamable HTTP, SSE, and stdio on the same binary. Works with Claude Desktop, Cursor, or any other MCP client.

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
# `uv run` assumes you ran `uv add openapi-mcp-gateway` (see Installation above).
# To skip the install, swap in `uvx openapi-mcp-gateway` to run the published package directly.
uv run openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json --name petstore
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

The gateway runs its own OAuth server so each MCP client authenticates as its own end-user, with tokens minted per session.

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

The gateway holds its own credentials and shares one upstream token across every MCP client, no per-user OAuth dance:

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
  # Resource auto-promotion: eligible GETs become MCP resources, the rest stay tools.
  - name: petstore
    spec: https://petstore3.swagger.io/api/v3/openapi.json
    base_url: https://petstore.swagger.io/v2
    mode: auto

  # Dynamic exposure: ~1,200 GitHub ops behind three meta-tools instead of 1,200 tool schemas.
  - name: github
    spec: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
    exposure: dynamic
    auth:
      type: bearer
      token: ${GITHUB_TOKEN}

  # Per-user OAuth2 with audience-bound tokens, no passthrough.
  - name: asana
    spec: https://raw.githubusercontent.com/Asana/openapi/master/defs/asana_oas.yaml
    auth:
      type: oauth2
      client_id: ${ASANA_CLIENT_ID}
      client_secret: ${ASANA_CLIENT_SECRET}
      scopes: [openid, email, profile, users:read, workspaces:read]
```

What this gives you at `http://127.0.0.1:8000`:

- `/petstore/mcp`: 13 tools + 3 concrete resources + 3 resource templates, partitioned by `mode: auto` with no spec edits.
- `/github/mcp`: three meta-tools (`list_operations`, `get_operation`, `call_operation`) fronting ~1,200 endpoints.
- `/asana/mcp`: per-user OAuth2 against Asana's IdP, with tokens minted server-side per [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707).

```bash
export GITHUB_TOKEN="ghp_..."
export ASANA_CLIENT_ID="..." ASANA_CLIENT_SECRET="..."
uv run openapi-mcp-gateway --config servers.yml
```

Runnable variants live in [`examples/`](examples/). Each YAML lists its prerequisites at the top.

`${ENV_VAR}` and `${ENV_VAR:-default}` work in any string field, resolved at request time. For OAuth2, `authorizationUrl` / `tokenUrl` / `scopes` are auto-detected from the spec's `securitySchemes`. Override with `auth.authorization_url` / `auth.token_url` / `auth.scopes` when the spec is incomplete.

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

Run `uv run openapi-mcp-gateway --help` for the CLI reference. The [Quick Start](#quick-start) examples cover most setups. The full field reference is below.

Configuration merges in this order, with each layer overriding the previous: **defaults → YAML (`--config`) → CLI flags → `Gateway.run(...)` kwargs**. A layer only overrides the fields it actually sets, so `--log-level=DEBUG` won't reset `logging.format` from your YAML. Nested objects like `logging` and per-server `auth` merge field-by-field. The `servers` list is the exception, replaced wholesale rather than merged entry-by-entry.

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
| `name` | string | required | Unique identifier. Mount path defaults to `/{name}` |
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
| `mode` | string | `tool_only` | `tool_only` forces every operation to a tool and ignores any `expose.resource` declaration. `auto` promotes eligible GETs (no required non-path parameter) to MCP resources, and spec-side `expose.resource` opt-ins still apply as explicit overrides. |
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

Read-only `GET` operations are a better fit for the MCP **resource** primitive than for a tool. Most MCP clients do not auto-load resources into the LLM context, so promoting catalog-style endpoints to resources saves tokens without losing reachability.

The default `mode: tool_only` exposes every operation as a tool. Set `mode: auto` to promote eligible GETs (no required `query` / `header` / `body` parameter) to resources:

```yaml
servers:
  - name: petstore
    spec: https://petstore3.swagger.io/api/v3/openapi.json
    mode: auto
```

That covers the common case: against the vanilla Petstore3 spec it produces 13 tools, 3 concrete resources, and 3 resource templates, zero spec edits.

For finer per-operation control (rename the resource, set a custom URI template, set a non-JSON MIME type), use the `operations` map:

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

Keys are matched against `operationId`. An unknown id raises at startup so typos do not silently no-op. Each entry fully replaces (does not merge with) the spec-side `x-mcp-integration`. A runnable demo lives at [`examples/petstore-override.yml`](examples/petstore-override.yml).

If you own the upstream spec, write the same opt-in inline with `x-mcp-integration.expose.resource`:

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
            # uri_template: petstore://v2/pets/{petId}  # optional override, must start with "<server>://"
```

Declaring both `expose.tool` and `expose.resource` registers the operation on both surfaces. Resource declarations are validated at startup: non-`GET` methods, required non-path parameters, and `uri_template` values that do not start with `<server>://` abort `Gateway.from_config` with a concrete error. Subscriptions are not implemented because REST has no native push.

### Tool Name and Description Overrides

Real-world specs ship ugly `operationId`s (GitHub's `actions/list-jobs-for-workflow-run-attempt`) and empty descriptions (most of `gists/*`), leaving the LLM to guess intent from the name. The same `operations` map renames the tool and rewrites the description without forking the spec:

```yaml
servers:
  - name: github
    spec: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
    operations:
      pulls/list-files:
        expose:
          tool:
            name: list_pull_request_files
            description: |
              List files changed in a pull request. Returns up to 3000 files,
              each with status (added / modified / removed), patch text, and
              line counts.
```

If you own the upstream spec, the inline form is `x-mcp-integration.expose.tool` on the operation.

### Dynamic Exposure

For APIs with hundreds of operations (GitHub, Stripe, etc.), registering each as its own tool can blow the LLM's context window before the agent does anything. Set `exposure: dynamic` and the client sees three meta-tools instead:

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

The LLM walks `list → get → call` to discover and invoke operations on demand. Auth, path templating, and per-operation request shape match static mode. Only the surfacing changes.

`exposure` is per-server, so `/github/mcp` can run `dynamic` while `/petstore/mcp` runs `static` in the same process.

### Logging

Configure via the `logging.*` YAML keys or via CLI flags (`--log-level`, `--log-format`, `--log-file`). `-v` and `-q` are shortcuts for `DEBUG` and `WARNING`. CLI flags override YAML field-by-field, following the precedence rule above.

## Python API

Use the gateway as a library:

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

Already running FastAPI? Decorate the routes you want exposed with `@mcp_tool` and the gateway picks them up. No second spec, no separate process, and no extra network hop (calls go in-process through `httpx.ASGITransport`):

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
