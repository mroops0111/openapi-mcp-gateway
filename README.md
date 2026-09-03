# OpenAPI MCP Gateway

[![CI](https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/openapi-mcp-gateway.svg?v=1)](https://pypi.org/project/openapi-mcp-gateway/)
[![PyPI Downloads](https://static.pepy.tech/badge/openapi-mcp-gateway/month)](https://pepy.tech/projects/openapi-mcp-gateway)
[![Python Version](https://img.shields.io/pypi/pyversions/openapi-mcp-gateway.svg?v=1)](https://pypi.org/project/openapi-mcp-gateway/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Mount any OpenAPI (Swagger) spec as a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server, or expose an existing FastAPI app the same way. Multiple APIs in one process, each with its own mount path and auth.

<p align="center">
  <img src="architecture.png" alt="OpenAPI MCP Gateway architecture: MCP clients (Claude Desktop, Cursor, AI agents) connect over stdio / SSE / streamable-http to the gateway, which at startup ingests OpenAPI specs or FastAPI apps and exposes them as MCP tools, meta-tools, and resources, then per call authorizes with bearer / API key / OAuth2 against its own issuer or an external one, never forwarding the caller's token, shapes the request and response with JSONata, and emits MCP-native output, calling upstream REST APIs over HTTP or an in-process FastAPI app over ASGI." width="100%">
</p>

```bash
uvx openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json
# Server live at http://127.0.0.1:8000/api/mcp
```

- **Multi-Spec, Multi-Auth.** Mount GitHub, an OAuth2 SaaS, and your internal API side by side, each with its own auth and token namespace.
- **Spec-Compliant Authorization.** The gateway runs its own OAuth server and mints audience-bound upstream tokens, so the MCP client's credential is never replayed against a third party.
- **Tool Shaping.** Rename ugly `operationId`s, hide knobs the model should never touch, and rewrite requests and responses with JSONata, all in YAML with no fork required.
- **Dynamic Exposure.** Front a 1,200-operation spec with three `list → get → call` meta-tools, so connecting to it does not spend the LLM's whole context window on tool schemas.
- **Resources, Not Just Tools.** Eligible read-only GETs register as MCP resources instead, addressable by URI and surfaced by the client rather than guessed at by the model.
- **FastAPI-Native.** Decorate routes with `@mcp_tool` to expose them in-process over ASGI, no extra hop and no second spec to maintain.

---

## Installation

```bash
uv add openapi-mcp-gateway
uv add "openapi-mcp-gateway[redis]"   # optional, Redis token store for multi-replica OAuth
```

Requires Python 3.11+. To skip the install entirely, `uvx openapi-mcp-gateway` runs the published package directly.

## Quick Start

Every example below uses `uv run`, which assumes the install above.

### 1. Public API, No Auth

```bash
uv run openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json --name petstore
```

Connect an MCP client to `http://127.0.0.1:8000/petstore/mcp`.

### 2. Bearer Token or API Key

```bash
export GITHUB_TOKEN="ghp_..."
uv run openapi-mcp-gateway \
    --spec https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json \
    --name github \
    --auth-type bearer \
    --auth-token '${GITHUB_TOKEN}'
```

<details>
<summary><b>API-Key Header instead of a Bearer Token</b></summary>

Use a config file so the header name is explicit:

```yaml
servers:
  - name: petstore
    spec: https://petstore3.swagger.io/api/v3/openapi.json
    auth:
      type: api_key
      token: ${PETSTORE_API_KEY}
      api_key_header: api_key
```

</details>

### 3. OAuth2

Rather than asking you to paste an upstream token into config, the gateway obtains one per caller. `authorization_code` runs the gateway as the authorization server and mints each end-user their own upstream token. `client_credentials` shares a single service token across every client. `token_exchange` hands issuance to an identity provider you already run. See [Authorization](#authorization) for how each pairs a check on the MCP endpoint with a credential for the API.

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

For the service-token flow, add `--auth-flow client_credentials`. Those two are what the CLI reaches. `token_exchange` needs an issuer and an audience, so it is configured per server under `auth:` in YAML, described in [Authorization](#authorization).

### 4. Multiple APIs at Once

Mix public, bearer, and OAuth2 services in a single config. Each server is mounted at `/{name}/mcp`:

```yaml
# servers.yml
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

That one file serves 13 tools with 3 concrete resources and 3 resource templates at `/petstore/mcp`, three meta-tools fronting ~1,200 endpoints at `/github/mcp`, and per-user OAuth2 against Asana's IdP at `/asana/mcp`. No spec edits anywhere. Run it with `uv run openapi-mcp-gateway --config servers.yml`.

### 5. Local Desktop Client (stdio)

For Claude Desktop, IDE integrations, or any MCP client that prefers stdio:

```json
{
  "mcpServers": {
    "petstore": {
      "command": "uvx",
      "args": [
        "openapi-mcp-gateway",
        "--spec", "/abs/path/to/openapi.json",
        "--transport", "stdio"
      ]
    }
  }
}
```

### More Examples

Runnable configs for every scenario above live in [`examples/`](examples/), each with its prerequisites documented at the top.

## Authorization

Every request crosses two boundaries, and one `auth:` block settles both of them. One is who may call the MCP endpoint, the other is what credential reaches the API behind it. Setting `auth.type`, plus `auth.flow` under `oauth2`, picks a pairing of the two.

| `auth.type` / `auth.flow` | MCP Endpoint | Credential Sent Upstream |
| --- | --- | --- |
| `none` | open | none |
| `bearer`, `api_key` | open | a fixed one from config, shared by every caller |
| `passthrough` | open | the caller's own header, forwarded unchanged |
| `oauth2` + `client_credentials` | open | one service token, shared by every caller |
| `oauth2` + `authorization_code` | **the gateway is the authorization server** | a per-user token the gateway obtained on their behalf |
| `oauth2` + `token_exchange` | **an external issuer is the authorization server** | a per-user token exchanged from the caller's |

Only the last two put a check in front of the MCP endpoint. The others suit a gateway on localhost or inside a private network, and leave it open to anyone who can reach the port.

<details>
<summary><b>Token Forwarding Policy</b></summary>

The MCP spec requires a server to accept only tokens minted for itself, and forbids relaying one to an upstream API. So under both protected flows the upstream is reached with a second, separately obtained credential rather than the one the caller presented. See [Access Token Privilege Restriction](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization#access-token-privilege-restriction).

`passthrough` is the one exception, and it exists for the [FastAPI integration](#fastapi-integration), where the gateway runs in-process as part of the app it exposes. There is no separate upstream to be confused about. Setting it against a genuinely separate API is the confused-deputy pattern the spec forbids, which is why nothing selects it automatically.

</details>

<details>
<summary><b>An Upstream behind a Separate Identity Provider</b></summary>

An API with no authorization server of its own, which accepts tokens from a provider the deployment already runs, needs the gateway to say which API its upstream token is for. Point the OAuth URLs at that provider and name the API:

```yaml
servers:
  - name: internal
    spec: https://internal.example.com/openapi.json
    auth:
      type: oauth2
      flow: authorization_code
      authorization_url: https://you.auth0.com/authorize
      token_url: https://you.auth0.com/oauth/token
      client_id: ${GATEWAY_CLIENT_ID}
      client_secret: ${GATEWAY_CLIENT_SECRET}
      upstream_audience: https://internal.example.com
```

Without it the provider mints for its own default audience and the API refuses the result. The parameter rides on the authorization request and on every token request, refreshes included, so a rotated token stays usable.

Authorization servers disagree on the spelling. `upstream_audience` is what Auth0 expects, `upstream_resource` is the [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707) parameter. Set whichever yours accepts.

MCP clients still authorize against the gateway and receive a gateway-issued token, while the provider-issued one is a second credential held on their behalf. End users see whatever login the provider federates to, so this works on any plan and needs nothing of the upstream but that it accept what the provider issues.

The Auth0 Management API is a worked example of exactly this shape, since its own audience differs from the tenant that issues for it. See [`examples/auth0-management.yml`](examples/auth0-management.yml).

</details>

<details>
<summary><b>Full Delegation with <code>token_exchange</code></b></summary>

`authorization_code` leaves the gateway issuing credentials of its own, so revoking someone at the provider has no effect until the gateway's token expires. `token_exchange` removes that second issuer. The provider mints tokens for the MCP endpoint directly, the gateway validates them, and each call exchanges one under [RFC 8693](https://www.rfc-editor.org/rfc/rfc8693) for a second token naming the upstream:

```yaml
servers:
  - name: internal
    spec: https://internal.example.com/openapi.json
    auth:
      type: oauth2
      flow: token_exchange
      issuer: https://auth.example.com/realms/internal
      upstream_audience: https://internal.example.com
      client_id: ${GATEWAY_CLIENT_ID}
      client_secret: ${GATEWAY_CLIENT_SECRET}
```

The gateway serves no `/authorize` or `/token` here. Its protected resource metadata names the issuer, clients authorize there, and the JWKS comes from the issuer's own metadata so key rotation needs no restart. JWT verification needs the `oidc` extra, so run it as `uvx --from "openapi-mcp-gateway[oidc]" openapi-mcp-gateway`.

Two things to check before committing to this mode. Token exchange support varies:

| Authorization Server | Token Exchange |
| --- | --- |
| Keycloak | generally available, enabled by default |
| authentik | 2026.8 and later |
| Zitadel | can only narrow an audience the token already carries |
| Auth0 | Custom Token Exchange, on Professional and Enterprise plans, with an Action to write |
| Logto | not implemented |

And because the issuer is the authorization server for this endpoint, MCP clients register there rather than with the gateway. Check whether yours supports dynamic client registration, or whether each client needs pre-registering.

</details>

<details>
<summary><b>Token Lifetimes</b></summary>

Under `authorization_code` the gateway's own access token lives 1 hour and its refresh token 24 hours. Each refresh issues a fresh refresh token, so the refresh TTL is the practical re-authorization cadence. A client refreshing within it never signs in again, while one idle past it must re-authorize. Tune both with `auth.mcp_access_token_ttl` and `auth.mcp_refresh_token_ttl`.

`token_exchange` mints nothing, so neither applies. Lifetimes are the issuer's to set.

</details>

## Tool Results

Every registered tool carries a protocol-native `title` and `annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`), so an agent can judge a tool before calling it. Results carry `structuredContent`, so a client reads a typed body and structured error payloads without re-parsing text. None of this needs configuration.

## Configuration

Run `uv run openapi-mcp-gateway --help` for the CLI reference. The [Quick Start](#quick-start) covers most setups, and the full field reference is below.

Configuration merges in this order, with each layer overriding the previous one. **Defaults → YAML (`--config`) → CLI flags → `Gateway.run(...)` kwargs**. A layer only overrides the fields it actually sets, so `--log-level=DEBUG` won't reset `logging.format` from your YAML. Nested objects like `logging` and per-server `auth` merge field-by-field. The `servers` list is the exception, replaced wholesale rather than merged entry-by-entry.

`${ENV_VAR}` and `${ENV_VAR:-default}` work in any string field, resolved at request time. For OAuth2, `authorizationUrl` / `tokenUrl` / `scopes` are auto-detected from the spec's `securitySchemes`, and the `auth.*` fields below override them when the spec is incomplete.

<details>
<summary><b>Top-Level Fields</b></summary>

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | string | `0.0.0.0` | Bind address (`0.0.0.0` = all interfaces). Clients on the same machine usually open `http://localhost:{port}` or `http://127.0.0.1:{port}`. |
| `port` | int | `8000` | Bind port |
| `url` | string | *(empty)* | Public base URL for OAuth redirects and discovery. When unset: `http://localhost:{port}` if `host` is `0.0.0.0`, otherwise `http://{host}:{port}`. Override when your registered redirect URI uses another host (tunnel, reverse proxy, etc.). |
| `transport` | string | `streamable-http` | `streamable-http`, `stdio`, or `sse` (deprecated) |
| `store.type` | string | `memory` | `memory` or `redis`. Redis shares OAuth credential state across replicas. It holds OAuth tokens and client registrations, never MCP protocol sessions, so single-replica or non-OAuth deployments can stay on `memory`. |
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
| `auth.type` | string | `none` | `none`, `bearer`, `api_key`, `oauth2`, or `passthrough`. Says where the upstream credential comes from: a fixed one, one the gateway obtains, or the caller's own forwarded |
| `auth.token` | string |  | Required for `bearer` / `api_key` |
| `auth.api_key_header` | string | `X-API-Key` | Header name for `api_key` |
| `auth.client_id`, `auth.client_secret` | string |  | Required for `oauth2` |
| `auth.flow` | string | from spec | `authorization_code` for per-user delegation, `client_credentials` for a shared service token, `token_exchange` to delegate this endpoint's authorization to an external issuer. When unset the gateway prefers the spec's declared `authorizationCode` flow, falling back to whatever else it declares. |
| `auth.issuer` | string |  | Required for `token_exchange`. The authorization server that mints tokens for this MCP endpoint |
| `auth.scopes`, `auth.authorization_url`, `auth.token_url` |  | from spec | OAuth2 overrides when `securitySchemes` is incomplete |
| `auth.upstream_resource`, `auth.upstream_audience` | string |  | Names the API the upstream token is for, when the API and its authorization server are different parties. `upstream_resource` is the [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707) parameter, `upstream_audience` is the spelling Auth0 uses. Set whichever your authorization server accepts; only what you set is sent |
| `auth.mcp_access_token_ttl` | int | `3600` | Lifetime in seconds of the MCP access token the gateway mints for `authorization_code` |
| `auth.mcp_refresh_token_ttl` | int | `86400` | Lifetime in seconds of the MCP refresh token. This is the practical re-authorization cadence, since each refresh slides the window forward |
| `policy.allow` | list |  | Only expose matching operations |
| `policy.deny` | list |  | Exclude matching operations |
| `timeout` | float | `90` | HTTP timeout in seconds |
| `exposure` | string | `static` | `static` registers one MCP tool per operation. `dynamic` registers three meta-tools (`list_operations`, `get_operation`, `call_operation`) for the LLM to walk on demand. |
| `mode` | string | `tool_only` | `tool_only` forces every operation to a tool and ignores any `resource` declaration. `auto` promotes eligible GETs (no required non-path parameter) to MCP resources, and spec-side `resource` opt-ins still apply as explicit overrides. |
| `operations` | map | `{}` | YAML-side `x-mcp-integration` overrides, keyed by `operationId`. Fully replaces (does not merge) the spec-side `x-mcp-integration` on that operation. Useful when you do not control the upstream spec. |

</details>

### Filtering Operations

Use `policy.allow` and `policy.deny` with `fnmatch` syntax against operation IDs (`getUsers`, `create*`) or method + path (`GET /users/*`).

<details>
<summary><b>Filter Syntax and Ordering</b></summary>

```yaml
policy:
  allow: ["GET /repos/*"]
  deny:  ["GET /repos/*/actions/secrets*"]
```

Operations can also be opted in from the spec side with `x-mcp-integration: {tool: {}}` plus `policy.marked_only: true`. Filters apply in the order `marked_only`, then `allow`, then `deny`.

</details>

### Resource Exposure

Read-only `GET` operations are a better fit for the MCP **resource** primitive than for a tool. Tools are model-controlled, so the LLM decides when to call one. Resources are application-controlled, surfaced by the client or picked by the user. A `GET` that is fully identified by its URL is a thing that exists at an address, which is what a URI is for.

Set `mode: auto` and every eligible GET promotes automatically. Eligible means no required `query`, `header`, or body parameter. Required path parameters are fine and turn the operation into a resource template. Against the vanilla Petstore3 spec that yields 13 tools, 3 concrete resources, and 3 resource templates with zero spec edits.

Keeping those endpoints off the tool list also saves context, since most clients do not auto-load resources. Resource support is uneven across the ecosystem, though, and an agent framework that ignores resources entirely will not reach a promoted operation at all. Stay on the default `mode: tool_only` when that is your target.

<details>
<summary><b>Per-Operation Control, from YAML or from the Spec</b></summary>

To rename a resource, set a custom URI template, or set a non-JSON MIME type, use the `operations` map keyed by `operationId`:

```yaml
servers:
  - name: petstore
    spec: https://petstore3.swagger.io/api/v3/openapi.json
    mode: auto
    operations:
      getPetById:
        resource:
          name: pet
          mime_type: application/json
      getInventory:
        resource:
          name: inventory
```

If you own the upstream spec, write the same opt-in inline instead:

```yaml
paths:
  /pets/{petId}:
    get:
      operationId: getPet
      x-mcp-integration:
        resource:
          name: pet
          mime_type: application/json
          # uri_template: petstore://v2/pets/{petId}  # optional, must start with "<server>://"
```

Declaring both `tool` and `resource` registers the operation on both surfaces. Each entry fully replaces (does not merge with) the spec-side `x-mcp-integration`. A runnable demo lives at [`examples/petstore-override.yml`](examples/petstore-override.yml).

An unknown `operationId` raises at startup so typos do not silently no-op. Resource declarations are validated there too, so non-`GET` methods, required non-path parameters, and `uri_template` values that do not start with `<server>://` abort `Gateway.from_config` with a concrete error. Subscriptions are not implemented because REST has no native push.

</details>

### Tool Shaping

A raw operation rarely makes a good tool. Its `operationId` is ugly (GitHub's `actions/list-jobs-for-workflow-run-attempt`), its description is empty (most of `gists/*`), it takes a cryptic filter DSL alongside a dozen knobs the model should never touch, and it wraps the few useful fields in a large envelope. `x-mcp-integration.tool` fixes all of that without forking the spec. `name` and `description` fix how the tool presents itself, while `params`, `strategy`, `request`, and `response` reshape the interface behind it.

<details>
<summary><b>Renaming a GitHub Operation</b></summary>

```yaml
servers:
  - name: github
    spec: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
    operations:
      pulls/list-files:
        tool:
          name: list_pull_request_files
          description: |
            List files changed in a pull request. Returns up to 3000 files,
            each with status (added / modified / removed), patch text, and
            line counts.
```

If you own the upstream spec, write the same block inline as `x-mcp-integration.tool` on the operation.

</details>

<details>
<summary><b>Reshaping the Interface with <code>params</code>, <code>strategy</code>, <code>request</code>, and <code>response</code></b></summary>

The input layer is declarative and the value transforms are [JSONata](https://jsonata.org/) expressions.

**`params` and `strategy` shape what the model sees.** Each `params` entry is a JSON Schema fragment (`type`, `enum`, `default`, `description`, `format`, `minimum`, `items`, and so on) plus two flags. `required` lifts the parameter into the schema's required list, and `hidden` removes a spec parameter from the surface. `strategy` is mandatory whenever `params` is set:

- **`merge`**: tweaks the operation's existing parameters and keeps the rest visible, so declaring a parameter the spec does not define is an error.
- **`replace`**: makes the declared entries the whole schema and drops every spec parameter, so it always needs a `request` to route the friendly arguments upstream.

```yaml
operations:
  searchIssues:
    tool:
      strategy: merge
      params:
        internal_flag: { hidden: true }
        per_page: { default: 30 }
        sort: { description: "One of comments, created, updated." }
```

**`request` and `response` transform the values.** Both are optional and independent of each other. `request` builds the entire upstream request, and `response` reshapes a successful body before it reaches the client.

```yaml
operations:
  searchIssues:
    tool:
      request: |
        $merge([$, { "per_page": 30, "state": "open" }])
      response: |
        [items.{ "title": title, "url": html_url }]
```

- **Routing**: a key that names a path placeholder fills the path, and the rest become query parameters for a body-less method or the JSON body otherwise.
- **Passthrough**: `$merge([$, { ... }])` forwards the incoming arguments and overrides only the keys you name, as above.
- **Lists**: wrapping a mapping in `[ ... ]` keeps the result an array even when a single item matches.
- **Errors**: a broken expression is rejected at startup, and a runtime failure returns an `isError` result naming the side that broke.

For a full `replace` example, where the declared `params` are the entire surface and `request` maps a friendly enum onto the raw query with `$lookup`, see [`examples/movie-shaping.yml`](examples/movie-shaping.yml).

</details>

### Dynamic Exposure

For APIs with hundreds of operations (GitHub, Stripe, etc.), registering each as its own tool can blow the LLM's context window before the agent does anything. Set `exposure: dynamic` and the client sees three meta-tools instead, which the LLM walks as `list → get → call` to discover and invoke operations on demand. It is per-server, so `/github/mcp` can run `dynamic` while `/petstore/mcp` runs `static` in the same process.

<details>
<summary><b>The Three Meta-Tools</b></summary>

- `list_operations()` returns `[{name, description}, ...]` for every operation on this server.
- `get_operation(name)` returns one operation's JSON Schema for input arguments.
- `call_operation(name, arguments)` invokes that operation against the upstream.

Auth, path templating, and per-operation request shape match static mode, so only the surfacing changes. See [`examples/github-dynamic.yml`](examples/github-dynamic.yml) for a runnable config.

</details>

### Logging

Configure via the `logging.*` YAML keys or via CLI flags (`--log-level`, `--log-format`, `--log-file`). `-v` and `-q` are shortcuts for `DEBUG` and `WARNING`. CLI flags override YAML field-by-field, following the precedence rule above.

### Authoring Configs with AI

`generate-config` is a companion Claude Code skill that writes a `config.yml` from a plain-language request, deriving the operations, auth, and shaping for you. This repo doubles as its plugin marketplace:

```
/plugin marketplace add mroops0111/openapi-mcp-gateway
/plugin install openapi-mcp-gateway

/generate-config connect our GitHub so my assistant can manage issues
```

## Python API

The gateway works as a library, either standalone or wrapped around an app you already run.

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

### FastAPI Integration

If you already run FastAPI, decorate the routes you want exposed with `@mcp_tool` and the gateway picks them up. No second spec, no separate process, and no extra network hop, since calls go in-process through `httpx.ASGITransport`. Auth is auto-detected from the app's `securitySchemes`, and passing an explicit `auth=AuthConfig(...)` to `Gateway.from_fastapi` overrides it.

<details>
<summary><b>Decorating Routes with <code>@mcp_tool</code></b></summary>

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

</details>

<details>
<summary><b>Auth in the FastAPI Integration</b></summary>

Because the gateway runs in-process and routes through `httpx.ASGITransport`, gateway and upstream share the same OAuth audience, so the MCP client's `Authorization` header passes through verbatim (`auth.type: passthrough`, set automatically for this integration only). For `client_credentials` schemes the gateway mints upstream tokens from its own credentials instead.

</details>

### Mounting into an Existing App

To serve MCP alongside your own routes, build a `Gateway` and mount it onto your app. `mount` attaches every MCP sub-app at its configured path and also registers the OAuth authorization-server and `.well-known` discovery routes those servers own, so an OAuth flow works end to end.

<details>
<summary><b>Mounting onto Your Own FastAPI App</b></summary>

```python
from fastapi import FastAPI
from openapi_mcp_gateway import Gateway, GatewayConfig, ServerConfig

app = FastAPI()

gateway = Gateway.from_config(
    GatewayConfig(
        url="https://your-app.example.com",  # public URL, used for discovery and redirect URLs
        servers=[ServerConfig(name="petstore", spec="petstore.json")],
    )
)
gateway.mount(app)  # mounts /petstore/mcp plus its OAuth and .well-known routes
```

Set `GatewayConfig.url` to the host app's public URL so discovery documents and OAuth redirect URLs point at the right origin. The upstream OAuth callback for a server named `<server>` is fixed at `/<server>/auth/callback`, so keep it clear of your app's own callback paths.

</details>

## License

[MIT](LICENSE)
