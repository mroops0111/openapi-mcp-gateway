---
name: generate-config
description: >-
  Generate an openapi-mcp-gateway config.yml from scratch for an API the user names or points to.
  Use when the user wants to expose a REST API (GitHub, Asana, an internal service, any OpenAPI backend) as MCP tools,
  asks to "generate", "build", "connect", "integrate", or "wire up" an API as MCP,
  or needs help writing or fixing a gateway config.
  Runs a three-stage pipeline that acquires an OpenAPI spec, selects and optionally shapes the operations,
  and emits a config that boots cleanly.
argument-hint: "[what to connect, in plain words]"
allowed-tools: Bash(uvx openapi-mcp-gateway *)
---

# Generate a Gateway Config

## Role

[openapi-mcp-gateway](https://github.com/mroops0111/openapi-mcp-gateway) reads an OpenAPI spec at startup and exposes its operations as MCP tools, then calls the upstream REST API on each tool call. One config file can mount several servers.

Your job is to hand the user a runnable `config.yml`, starting from nothing more than an API they name or a spec they point at. The user may be non-technical, so you own the OpenAPI details and confirm intent in plain language.

Most of the work is choosing which operations to expose and how to authenticate. Reach for shaping (`params` / `strategy` and JSONata) only when a raw operation makes a poor tool. A config that exposes the right operations under the right auth, with no shaping, is a fine result.

## Config Model

This skill carries the config keys you need. The Stage 2 guidance covers the `tool` block, and the Stage 3 skeleton shows the whole config shape. For the CLI flags and their allowed values, such as the transports and auth types, run:

```bash
uvx openapi-mcp-gateway --help
```

Do not invent a key you have not seen in one of those.

Follow these two rules:

- **Policy Selects, Operations Override.** The tool surface comes from the `policy` block, where `allow` and `deny` are globs over operation ids and `annotated_only` keeps only operations the spec annotates. The `operations` map only attaches overrides to operations the policy already kept, and it errors on an unknown id. Set `policy.allow` to narrow a large spec, or every operation becomes a tool.
- **Complete Spec, Lean Config.** Point `spec` at the full upstream document and never trim it. Narrow the surface with `policy.allow`, so the user can widen it later with a one-line change instead of re-acquiring the spec.

### Features by Scenario

Match features to the user's situation, do not reach for all of them.

- **Local, Single User, One API (stdio).** A static token is enough, `bearer` or `api_key` with the provider's header, such as a GitHub personal access token. No OAuth. Expose a handful of operations with `policy.allow`.
- **A Huge Spec of Hundreds of Operations.** Prefer dynamic exposure, which fronts the spec with a few meta-tools, over listing every operation, so the tool list never floods the model. Still scope it with `policy.allow`.
- **A Messy API That Makes Poor Tools.** Shape it. Hide the knobs the model should not set, and trim the response.
- **Multiple Users, or a Provider That Mandates It.** Use `oauth2`, so each user signs in with their own credentials. `authorization_code` is the default and fits nearly every case.
- **An API Behind an Identity Provider You Run.** Point the OAuth URLs at that provider and set `auth.upstream.audience` to the API, so the provider mints a token the API will accept rather than one for its own default audience. Reach for `flow: token_exchange` only when the provider should also issue for the MCP endpoint itself, which needs RFC 8693 support on its side.

## Process

Read the request, then run the three stages in order, keeping the user in the loop between them. Do not silently guess a whole config end to end.

### Reading the Request

The user gives a plain-language request, held verbatim in `$ARGUMENTS`, for example `connect our Github so my assistant can manage issues`. They are usually non-technical, so do not expect them to name auth types, operations, or shaping. Take two things from the request, the API (a name, a URL, or a spec) and the intent (what they want to do).

Derive the rest yourself, do not ask for it in jargon.

- **Auth**: you determine this from the spec, not by asking the user to name a type. You read the security schemes in Stage 1, then tell the user in plain words which credential they need and where to get it.
- **Operations**: you select these from the intent in Stage 2.
- **Shaping**: your own judgment, applied where an operation makes a poor tool. The user never asks for a shaping level.

Ask the user only for what you genuinely cannot derive, such as the host of a self-hosted API or which operations to include. Ask in plain language, offer concrete options, and mark the one you recommend. Prefer an interactive menu when the host provides one, which in Claude Code is the `AskUserQuestion` tool. When no such tool is available, list the options in your reply and let the user pick. Never block on something you can reasonably ask about.

### Stage 1: Acquire the Spec

Get a machine-readable OpenAPI spec first. Work down this ladder, stop at the first tier that yields a usable spec, and never skip to a lower tier because it is easier. Tell the user which tier it came from and how confident you are.

1. **User-Provided Spec.** If the user hands you an OpenAPI URL or a local path, use it. Fetch it and confirm it parses. Highest confidence, and it short-circuits the search below.
2. **Official Spec.** Search for the vendor's own published OpenAPI or Swagger document (GitHub, Stripe, and Asana each host one). Prefer an official raw URL, since the gateway reads `spec:` straight from a URL. This is the preferred source when the user gave none.
3. **Third-Party Spec.** Only when there is no official spec, look for a well-maintained community spec (a GitHub repo, SwaggerHub). Prefer one that names the upstream version it targets and shows signs of being tested against a live instance. Point `spec:` at its raw URL, or vendor a copy. Tell the user it is community-maintained and unofficial, and always pair it with a `policy.allow` (see Stage 2) so an unvetted spec cannot widen the tool surface past what you intend.
4. **Synthesize from Docs.** The last resort, only when no official or usable third-party spec exists (some self-hosted or internal APIs). Build a minimal spec covering just the operations the user needs, from the API's HTML reference. Flag it as lower confidence and list every assumption (base URL, auth style, required parameters) for the user to confirm.

Fetch the spec candidate yourself and confirm it parses as OpenAPI before you rely on it.

Once you have the spec, read its security schemes to fix the auth type and any header name, and confirm the base URL, before moving on. Never invent a token value. Reference it as an environment variable, for example `token: ${GITHUB_TOKEN}`.

### Stage 2: Select and Shape the Operations

Do not expose every operation. Turn the intent into a short list of operations, confirm it with the user, then set `policy.allow` to those operation ids.

Then decide, per operation, how much reshaping it needs. Prefer the lightest option that gives a good tool.

- **Expose As-Is.** If the raw operation already makes a clean tool, keep it in the surface via `policy.allow` and give it no `tool` block, or only a name and description override. This is the default, and the whole of `shaping: none`.
- **Shape It.** Reach for shaping when the operation exposes parameters the model should not set, speaks in cryptic values, or returns a bloated envelope. Pick the lightest mechanism that fixes it, not `replace` every time.
  - **Pin a Constant** the model should never set, like `format=json`. Use `merge` with `{hidden: true, default: <value>}`. The value is injected upstream and the parameter leaves the schema, with no `request` needed.
  - **Default an Optional Input.** Use `merge` with `{default: <value>}`, which is sent upstream when the model omits the parameter.
  - **Rename an Input or Map a Friendly Enum.** Use `merge` with `params` and a `request` `$lookup`. Both `request` and `response` compose with `merge`, not only with `replace`. Naming a parameter the spec does not define is a startup error, so keep merged names honest.
  - **Trim or Rename the Response.** Use a `response` expression alone, which works with any strategy, including no `params` at all.
  - **Fully Reshape the Input, or Wrap the Body.** Use `replace`, which drops the spec's parameters so you declare a fresh set of friendly params, then a `request` expression routes them upstream. Naming a param the spec never defined is fine here, the must-match rule only applies to `merge`.

The JSONata idioms you will use most:

- `$lookup(table, key)` maps a friendly enum onto the raw API value, for example `popular` to `popularity.desc`.
- `[ results.{ ... } ]` forces a list, because a single-match projection unwraps to one object otherwise.
- `$merge([$, { ... }])` passes most arguments through and overrides only a few, where `$` is the whole input.

The `request` result routes to the upstream call by its top-level keys. A key whose name matches a `{placeholder}` in the path fills that path segment. Of the rest, each key becomes a JSON body field for `POST` / `PUT` / `PATCH`, or a query parameter for `GET` / `DELETE`. A `null` value is dropped, so an omitted optional friendly argument leaves no trace upstream. To match an upstream that wants a wrapped body, such as `{"issue": {...}}`, nest the fields under that key in the `request` result, for example `{ "issue": { "subject": subject, "project_id": project_id } }`.

### Stage 3: Emit and Verify

Write the config in the documented format in the user's working directory, then verify it boots. A minimal server needs only `spec`, `base_url`, and `auth`. The `policy`, `operations`, and any `tool` shaping are additive.

```yaml
host: "127.0.0.1"
port: 8000
transport: streamable-http

servers:
  - name: <server-name>
    spec: <url-or-local-path>
    base_url: <upstream-base-url> # required, a spec with a relative server needs the real host
    auth:
      type: bearer # or api_key / oauth2 / passthrough / none
      token: ${SOME_TOKEN}
      # oauth2 also takes flow: authorization_code (default) / client_credentials / token_exchange
      # required_scopes: for token_exchange, what an inbound token must already carry
      upstream: # everything the gateway sends to the upstream authorization server
        client_id: ${SOME_ID}
        scopes: [...]
        audience: https://the-api.example.com # when the API and its AS differ
    policy: # the selector for which operations become tools
      allow: ["<operation_id>", "..."] # globs matched against operation ids, omit to expose all
    operations: # overrides only, applied to operations the policy kept
      <operation_id>:
        tool: # optional, omit for an as-is passthrough
          params_strategy: replace # or merge
          params:
            <friendly_param>:
              type: string
              enum: [...] # when the raw value is cryptic
              default: ...
              description: ...
          request: |
            { ... JSONata ... }
          response: |
            [ ... JSONata ... ]
```

For API-key auth with a custom header, name the header, since it defaults to `X-API-Key`:

```yaml
    auth:
      type: api_key
      token: ${SOME_TOKEN}
      api_key_header: X-Acme-Api-Key
```

Both JSONata expressions compile when the gateway loads the config, so a broken expression fails validation with a message naming the side that broke. Validate with:

```bash
uvx openapi-mcp-gateway --config <config.yml> --dry-run
```

It loads every spec, applies the policy, and compiles the shaping, then exits without serving and prints a summary of what would run. Exit 0 means the config is valid. On failure, read the named side (request or response), fix the JSONata, and re-run until it validates clean.

One config shape cannot be validated offline. `flow: token_exchange` contacts the issuer during startup to discover its JWKS and token endpoints, so `--dry-run` needs that provider reachable. Say so rather than reporting the config unverified.

Hand the finished config to the user with a one-line summary of each tool, the environment variables they must set, and the exact run command:

```bash
uvx openapi-mcp-gateway --config <their-config>.yml
```

## Notes

- You produce a config only. You do not start a long-running server for the user, and you do not wire the gateway into their Claude Code. The gateway is a neutral runtime the user points any MCP client at.
- When confidence is low (a synthesized spec, a guessed auth style), say so plainly and list what needs human confirmation. A wrong assumption should surface, not hide.
