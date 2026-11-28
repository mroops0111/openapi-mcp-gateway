---
description: Every field a config file takes, with its type and default, how the config layers merge, and how values are read and checked.
---
# Configuration

This page lists every field a config file takes, how the layers that set them merge, and how values are read.

## Precedence

Each layer overrides the one before it:

1. Defaults
2. YAML (`--config`)
3. CLI flags
4. `Gateway.run(transport, host, port)` arguments

A layer only overrides the fields it sets, so `--log-level DEBUG` keeps `logging.format` from your YAML. Nested objects such as `logging` merge field by field. The `servers` list is the exception, replaced whole rather than merged entry by entry.

With `--config`, only some flags apply, as [Flag Rules](cli.md#flag-rules) lists.

## Values and Validation

These rules govern how values are read and checked.

- **Environment Variables**: `${ENV_VAR}` and `${ENV_VAR:-default}` work as the whole value of the credential fields, the issuers, `resource`, `audience` and `headers`. They resolve at startup. Other fields, such as `base_url`, take literal values.
- **Unknown Keys**: an unrecognized key is refused at startup rather than ignored, so a typo in a field that narrows access fails closed.
- **OAuth2 Discovery**: `authorizationUrl` and `tokenUrl` come from the spec's `securitySchemes`, as do `scopes` under `client_credentials`. The `auth.upstream` fields override them when the spec is incomplete.

## Top-Level Fields

These fields sit at the root of the config file and apply to the whole process.

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | string | `0.0.0.0` | Bind address. `0.0.0.0` listens on all interfaces. |
| `port` | int | `8000` | Bind port |
| `url` | string | derived | Public base URL for OAuth redirects and discovery. Unset, it is `http://localhost:{port}` when `host` is `0.0.0.0`, otherwise `http://{host}:{port}`. Set it behind a tunnel or reverse proxy. |
| `transport` | string | `streamable-http` | `streamable-http`, `stdio`, or `sse` (deprecated) |
| `debug` | bool | `false` | Starlette debug mode, which returns tracebacks in error responses |
| `enable_docs` | bool | `false` | Serves the gateway's own `/docs`, `/redoc` and `/openapi.json` |
| `cors.allow_origins` | list | `["*"]` | Origins the CORS middleware accepts |
| `cors.allow_methods` | list | `["*"]` | Methods the CORS middleware accepts |
| `cors.allow_headers` | list | `["*"]` | Request headers the CORS middleware accepts |
| `cors.expose_headers` | list | `["*"]` | Response headers a browser client may read |
| `dns_rebinding_protection.allowed_hosts` | list | `[]` | `Host` values the MCP endpoints accept, as `host` or `host:port` (`host:*` for any port). Listing any turns the check on. [Host and Origin Checks](../guides/deployment.md#host-and-origin-checks) gives what an empty list accepts. |
| `dns_rebinding_protection.allowed_origins` | list | `[]` | `Origin` values accepted from browser clients, as `scheme://host[:port]`. Requires `allowed_hosts`. |
| `store.type` | string | `memory` | `memory` or `redis`. [Shared Token Store](../guides/deployment.md#shared-token-store) says what Redis holds. |
| `store.redis_url` | string | `redis://localhost:6379` | Redis URL when `store.type` is `redis` |
| `store.key_prefix` | string | `mcp_gw` | Prefix of every Redis key, so several gateways can share one Redis |
| `logging.level` | string | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `logging.format` | string | `text` | `text` or `json` |
| `logging.file` | string |  | Mirror logs to this file |
| `servers` | list | `[]` | Per-server entries, described below. An empty list is valid and serves nothing. |

[Deployment](../guides/deployment.md) explains DNS rebinding and when to set these.

## Per-Server Fields

Each entry under `servers` takes these fields, grouped by key.

### Basics

These fields name the server and say where its API is.

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Letters, digits, `-` and `_`. The mount path defaults to `/{name}`. |
| `path_prefix` | string | `name` | Mount path, when it should differ from `/{name}` |
| `spec` | string | required | Path or URL of the OpenAPI document, JSON or YAML. A relative path resolves against the working directory. |
| `base_url` | string | from spec | Override the upstream base URL |
| `timeout` | float | `90` | Upstream HTTP timeout in seconds |
| `headers` | map | `{}` | Static headers sent on every upstream call, such as an API version. Values take `${ENV_VAR}`, and an unset variable leaves the header out. A header the call itself sends takes precedence. `Authorization` is refused, since it belongs to `auth`. |
| `security_schemes` | map | `{}` | Security schemes in the OpenAPI `securitySchemes` shape, merged into the spec's, each replacing one of the same name. For a spec that declares none, or declares one wrongly |
| `security` | list | from spec | Replaces the spec's root `security`, in its OpenAPI shape, such as `[{token: []}]`. An operation that declares its own keeps it. Every scheme named must be declared |
| `instructions` | string |  | Guidance about the server as a whole, such as when to use it or which tool to call first. Sent in the MCP `server/discover` result, or in `initialize` for a client on an older protocol version. Clients place it in the model's context. Blank text counts as unset. |

### auth.upstream

These fields say how the gateway authenticates to the upstream API. [Authorization](../guides/authorization.md) explains how to combine them.

| Field | Type | Default | Description |
|---|---|---|---|
| `type` | string | `none` | `none`, `oauth2`, or `passthrough`. It says whether a token is obtained per user or for the service, or the caller's own is forwarded. `passthrough` is for the FastAPI integration |
| `credentials.<scheme>.value` | string |  | A static value for the security scheme named `<scheme>`, sent where the scheme places it. An `apiKey` scheme names a header, query parameter or cookie, and an HTTP `bearer` scheme sends `Authorization: Bearer`. Takes `${ENV_VAR}`, and one that resolves empty refuses startup. Combines with `none` or `oauth2`, and `passthrough` refuses it. [Which Credential Each Operation Carries](../guides/authorization.md#which-credential-each-operation-carries) gives the rules |
| `flow` | string | from spec | `authorization_code` for per-user sign-in, `client_credentials` for one shared service token, `token_exchange` to exchange each caller's token from `auth.mcp.issuer`. Unset, the gateway prefers the spec's `authorizationCode` flow, then `clientCredentials` |
| `client_id`, `client_secret` | string |  | The gateway's own credential at the upstream authorization server, or at the issuer under `token_exchange`. Required for `oauth2` |
| `authorization_url` | string | from spec | For `authorization_code`. Overrides an incomplete `securitySchemes` |
| `token_url` | string | from spec | For `authorization_code` and `client_credentials`. `token_exchange` uses the issuer's token endpoint |
| `scopes` | list | see description | The ceiling, the most a sign-in or exchange asks the upstream for. Under `client_credentials`, unset takes the spec flow's scopes. Under `enforce_scopes`, unset is every scope the exposed operations declare. Without `enforce_scopes`, every sign-in or exchange asks for all of it |
| `required_scopes` | list | `[]` | For `authorization_code` and `token_exchange`. Upstream scopes every sign-in or exchange asks for first, such as `offline_access` or `openid` |
| `enforce_scopes` | bool | `false` | For `authorization_code` and `token_exchange`. Asks the upstream for only what each MCP grant opens, and checks every call against the upstream grant, reading each operation's OpenAPI `security` |
| `scope_implies` | map |  | For `authorization_code` and `token_exchange`. Maps an upstream scope to the narrower scopes it covers, such as `repo: [public_repo]`, applied transitively |
| `resource`, `audience` | string |  | Names the API the upstream token is for, when the API and its authorization server are different parties. `resource` is the [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707) parameter, `audience` is the spelling Auth0 uses. Only what you set is sent. `token_exchange` requires one |
| `issuer` | string |  | For `authorization_code`. The upstream authorization server's issuer identifier, exactly as its metadata publishes it. Turns on the [RFC 9207](https://www.rfc-editor.org/rfc/rfc9207) `iss` check |

### auth.mcp

These fields say who may call the MCP endpoint.

| Field | Type | Default | Description |
|---|---|---|---|
| `issuer` | string |  | The external authorization server that mints this endpoint's tokens. Required for `token_exchange`, and accepted on a shared credential in place of `clients`. Needs the `oidc` extra |
| `required_scopes` | list | `[api]` where the gateway mints tokens without `operation_scopes` | Scopes every token must carry, listed as `scopes_supported` in the protected resource metadata, which every client reads to choose its first request. Required under `issuer`. Refused beside `operation_scopes` where the gateway mints tokens |
| `operation_scopes.auto` | string | `none` | `tags` derives read, write and delete scopes per tag, as [Defining MCP Scopes](../guides/scopes.md#defining-mcp-scopes) gives. `none` derives nothing |
| `operation_scopes.manual.<scope>.description` | string | required | What the consent page shows for the scope |
| `operation_scopes.manual.<scope>.operations` | list | required | Operations the scope opens, as `policy.allow` patterns |
| `operation_scopes.unscoped` | string | `deny` | What an operation no scope covers gets. `deny` closes it to every token, `allow` opens it to every token |
| `clients.<id>` | map |  | A client the gateway issues tokens to, keyed by `client_id`. A key may be a metadata document URL under `authorization_code` |
| `clients.<id>.client_secret` | string |  | Under `authorization_code`, unset makes the client public and held to PKCE. Required for agents on a shared credential. Refused for a metadata document URL |
| `clients.<id>.redirect_uris` | list | any loopback | Under `authorization_code`, the redirects the client may use. Unset accepts any loopback redirect, on any port. Refused for a metadata document URL |
| `clients.<id>.scopes` | list | `[]` | The most the client may be granted. Each must be one `operation_scopes` defines |
| `clients.<id>.default_scopes` | list | all of `scopes` | Under `authorization_code`, what a sign-in asking for no scope receives. Must be within `scopes` |
| `clients.<id>.consent` | bool | `true` | Under `authorization_code`, shows the consent page before the upstream sign-in. `false` skips the page for that client |
| `other_clients` | map or `false` | admits any when `clients` is empty | Under `authorization_code`, admits metadata document clients `clients` does not list. `false` admits none. Setting it turns off Dynamic Client Registration. These clients always see the consent page |
| `other_clients.trusted_hosts` | list | any host | Admits only documents on these hosts, matched exactly, as bare host names |
| `other_clients.scopes`, `other_clients.default_scopes` | list | `[]`, all of `scopes` | As for a listed client |
| `access_token_ttl` | int | `3600` | Lifetime in seconds of the MCP access token the gateway mints, under `authorization_code` or for agents. Under `authorization_code` it never outlives the upstream token |
| `refresh_token_ttl` | int | `86400` | For `authorization_code`. Lifetime in seconds of the MCP refresh token, renewed on each refresh |

### policy

These fields filter which operations the server exposes.

| Field | Type | Default | Description |
|---|---|---|---|
| `allow` | list |  | Keep only matching operations |
| `deny` | list |  | Drop matching operations |
| `annotated_only` | bool | `false` | Keep only operations the spec marks with `x-mcp-integration.tool` |

The filters apply in the order `annotated_only`, `allow`, `deny`. A pattern containing a space matches `METHOD /path`, both sides as `fnmatch` globs, such as `GET /repos/*`. Any other pattern globs the `operationId`, such as `create*`. A pattern that matches nothing logs a warning, and a server left with no operations fails at startup.

`annotated_only` reads the spec only. An `operations` entry cannot opt an operation in, and naming a filtered-out operation there is an error.

### exposure

These fields decide how operations surface as MCP primitives.

| Field | Type | Default | Description |
|---|---|---|---|
| `style` | string | `static` | `static` registers one tool per operation. `dynamic` registers `search_operations`, `get_operation` and `call_operation`. |
| `promote_resources` | bool | `false` | Under `static`, promotes each GET with no required non-path parameter to an MCP resource. Refused under `dynamic`. |
| `search.field_weights.<field>` | float | see [Search Ranking](../concepts/dynamic-exposure.md#search-ranking) | Weight of `name`, `summary`, `description`, `path`, `tags`, or `parameters` in search. `0` leaves the field out. Setting all to `0` is refused. |
| `search.semantic.model` | string | required | Embedding model as Pydantic AI names it, such as `openai:text-embedding-3-small` or `ollama:bge-m3`. Needs the `embeddings` extra. |
| `search.semantic.dimensions` | int |  | Shorter vectors, for a model that can return them |
| `search.semantic.cache_directory` | path |  | Keeps vectors on disk between restarts, in a subdirectory per server. `~` expands. |
| `search.semantic.retrieval` | string | `semantic` | `semantic` ranks by the embedding alone. `hybrid` merges that ranking with the word ranking. |

`exposure.search` is refused under `static`.

### operations

`operations` maps an `operationId` to an override of that operation, with the keys in [Operation Fields](#operation-fields).

| Field | Type | Default | Description |
|---|---|---|---|
| `operations.<operationId>` | map | `{}` | Replaces the spec-side `x-mcp-integration` of that operation, without merging, unless the entry sets only `security`. An `operationId` the spec lacks, or the policy filtered out, fails at startup. |

## Operation Fields

These keys sit under `operations.<operationId>` in the config, or under `x-mcp-integration` in the spec, except `security`, which only the config takes.

| Field | Type | Default | Description |
|---|---|---|---|
| `tool.name` | string | from `operationId` | Tool name the model sees |
| `tool.description` | string | from spec | Tool description. Dynamic search reads it too. |
| `tool.annotations` | map | from method | MCP tool annotations, such as `readOnlyHint` or `destructiveHint`, in camelCase or snake_case. Each key you set overrides the derived one. |
| `tool.params.<name>` | map | `{}` | A JSON Schema fragment for one parameter, plus `required` and `hidden` |
| `tool.params_strategy` | string |  | `merge` or `replace`. Required when `params` is set. |
| `tool.request` | string |  | JSONata expression that builds the upstream request from the arguments. Required under `replace`. |
| `tool.response` | string |  | JSONata expression that reshapes a successful response body |
| `tool.media.<path>.encoding` | string | required | `base64`, for a response field holding encoded media |
| `tool.media.<path>.media_type` | string | `application/octet-stream` | Media type of that field's content block |
| `resource.name` | string | from `operationId` | Resource name |
| `resource.description` | string | from spec | Resource description |
| `resource.mime_type` | string | `application/json` | MIME type of the resource contents |
| `resource.uri_template` | string | `{name}://{path}` | Custom URI template, which must start with `{name}://` |
| `security` | list |  | Config only. Replaces the operation's OpenAPI `security`, in the same form, for a spec that declares its upstream scopes wrongly. Only schemes the spec declares are accepted. An entry setting only `security` leaves `x-mcp-integration` as it is. |

[Tool Shaping](../guides/tool-shaping.md) shows the `tool` keys at work, and [Exposure](../guides/exposure.md#resource-exposure) the `resource` keys.

## Related

These guides use the scope and issuer fields, and build the same config from Python.

- **Guides**: [Scopes](../guides/scopes.md) for `operation_scopes`, `clients` and `enforce_scopes`, [Identity Providers](../guides/identity-providers.md) for `auth.mcp.issuer` under `token_exchange` and [Python and FastAPI](../guides/python.md) for setting these fields in code
