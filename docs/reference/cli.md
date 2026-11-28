---
description: Every flag of the serve and eval commands, the rules the serve flags follow, and the output of a dry run.
---
# CLI

The command is `openapi-mcp-gateway`. Installed with `uv tool install openapi-mcp-gateway`, it is also available as `omgw`. Examples here use the full name with `uvx`, so they run without installing.

## `serve` Command

The `serve` command runs the gateway from one spec (`--spec`) or from a config file (`--config`). One of the two is required.

| Flag | Default | Description |
|---|---|---|
| `--spec` |  | Path or URL of a single OpenAPI spec |
| `--config` |  | Path of a YAML config file. [Configuration](configuration.md) lists its fields. |
| `--name` | `api` | Server name under `--spec`, which sets the mount path `/{name}` |
| `--base-url` | from spec | Override the upstream base URL |
| `--transport` | `streamable-http` | `streamable-http`, `stdio`, or `sse` (deprecated) |
| `--host` | `0.0.0.0` | Bind host |
| `--port` | `8000` | Bind port |
| `--auth-upstream-type` |  | `none` or `oauth2` |
| `--auth-upstream-credential` |  | `SCHEME=VALUE` or `SCHEME=${ENV_VAR}`, a static credential for a scheme the spec declares, repeatable |
| `--security-scheme` |  | `NAME=JSON`, a scheme the spec leaves out or one that replaces the spec's, as `security_schemes` takes it, repeatable |
| `--security` | from spec | `SCHEME`, or `SCHEME+SCHEME` for schemes needed together, one alternative of the root `security` per flag, repeatable |
| `--auth-upstream-client-id` |  | OAuth2 client ID or `${ENV_VAR}` |
| `--auth-upstream-client-secret` |  | OAuth2 client secret or `${ENV_VAR}` |
| `--auth-upstream-scopes` |  | Comma-separated scopes to request upstream |
| `--auth-upstream-authorization-url` | from spec | OAuth2 authorization URL |
| `--auth-upstream-token-url` | from spec | OAuth2 token URL |
| `--auth-upstream-flow` | from spec | `authorization_code` or `client_credentials` |
| `--log-level` | `info` | `debug`, `info`, `warning`, `error`, or `critical` |
| `--log-format` | `text` | `text` or `json` |
| `--log-file` |  | Also write logs to this file |
| `-v`, `--verbose` |  | Shortcut for `--log-level debug` |
| `-q`, `--quiet` |  | Shortcut for `--log-level warning` |
| `--dry-run` |  | Validate and print what would be served, then exit |
| `--output` | `text` | Format of the `--dry-run` summary, `text` or `json` |

### Flag Rules

The flags follow a few rules that `--help` does not state.

- **Config File First**: with `--config`, only `--host`, `--port`, `--transport` and the logging flags apply. `--name`, `--base-url`, `--security-scheme`, `--security` and the `--auth-upstream-*` flags are ignored, and so is `--spec`.
- **Inferred Auth Type**: without `--auth-upstream-type`, a client ID or a flow implies `oauth2`, and credentials alone imply `none`. Anything else needs an explicit type.
- **Undeclared Schemes**: a credential names a scheme, so a spec that declares none, such as GitHub's, takes `--security-scheme` and `--security` beside the credential, as below.
- **CLI Reach**: `passthrough` and `token_exchange` are not available as flags. Set them in a config file, as [Authorization](../guides/authorization.md) shows.
- **Exclusive Shortcuts**: `-v` and `-q` together are an error.
- **Precedence**: flags override YAML field by field, following [Precedence](configuration.md#precedence).

A bearer token for every operation of a spec that declares no security:

```bash
uvx openapi-mcp-gateway serve --spec https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json \
  --security-scheme 'token={"type": "http", "scheme": "bearer"}' --security token \
  --auth-upstream-credential 'token=${GITHUB_TOKEN}'
```

### Dry Run

`--dry-run` loads every spec, applies the policy, compiles the shaping, and exits without serving. Every environment variable the config references must be set, though a placeholder value is enough. With `auth.mcp.issuer` set, it also fetches the issuer's metadata, so the issuer must be reachable. An invalid config exits with status 1 and the reason. A valid one prints a summary per server:

```text
✓ Valid   1 server(s), 1 tool(s), 102 resource(s)
    transport      streamable-http

  pokeapi
    mount          /pokeapi
    base url       https://pokeapi.co
    upstream auth  none
    mcp auth       open
    policy         no filter, every operation exposed
    exposure       static
    instructions   none
    tools          1
      NAME          METHOD  PATH              SHAPING
      list_pokemon  GET     /api/v2/pokemon/  passthrough
    resources      meta_retrieve, ability_list, ability_retrieve, ...
```

`--output json` emits the same facts as data on stdout, with logs kept on stderr. `Gateway.describe()` returns the same document from Python, which holds these parts.

- **Names**: the sanitized names the model is shown, so an operation with no `operationId` appears under a name derived from its method and path.
- **Tools**: each tool's `method`, `path`, `shaping`, `description` and `input_schema`.
- **Auth**: `auth.upstream` holds `type` and only the fields that server resolved, such as the `flow` picked from the spec. It never holds a credential.

### Transport Limits

`stdio` serves one server and no MCP auth, as [Stdio](../guides/deployment.md#stdio) explains. `sse` is deprecated in the MCP 2026-07-28 spec and logs a warning at startup.

## `eval` Command

The `eval` command needs the `eval` extra, `-v` on the group logs progress, and [Measuring a Config](../guides/authoring-with-ai.md#measuring-a-config) explains its use.

### `eval check` Command

`eval check` takes these flags, and calls no model and no real API.

| Flag | Default | Description |
|---|---|---|
| `--task-file` | required | The tasks, in YAML |
| `--config` | required | A config as `NAME=PATH`, or `PATH` alone, then named `yours`. Repeat for several. |

### `eval run` Command

`eval run` takes these flags, and a replay server answers every upstream request, so no real API is called.

| Flag | Default | Description |
|---|---|---|
| `--task-file` | required | The tasks, in YAML |
| `--config` | required | A config as `NAME=PATH`, or `PATH` alone. Repeat for several. |
| `--model` | required | A model as Pydantic AI names it, such as `anthropic:claude-haiku-5-5`. Repeat for several. |
| `--against` | `baseline` | `baseline` (one tool per operation), `dynamic`, `tool-search`, or `none`. Repeat for several. |
| `--repeat` | `1` | Runs per task |
| `--output` |  | Write the full report as JSON to this file |

## Related

The authoring guide covers using `--dry-run` and `eval` on a generated config.

- **Guides**: [Inspecting a Config](../guides/authoring-with-ai.md#inspecting-a-config) for reading a dry run's output and [Measuring a Config](../guides/authoring-with-ai.md#measuring-a-config) for writing the tasks `eval` runs
