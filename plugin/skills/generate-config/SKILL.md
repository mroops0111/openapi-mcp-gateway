---
name: generate-config
description: >-
  Generate an openapi-mcp-gateway config.yml from scratch for an API the user
  names or points to. Use when the user wants to expose a REST API (Redmine,
  GitHub, an internal service, any OpenAPI backend) as MCP tools, asks to
  "generate", "build", "connect", "integrate", or "wire up" an API as MCP, or
  needs help writing or fixing a gateway config. Runs a three-stage pipeline:
  acquire an OpenAPI spec, select and optionally shape the operations, and emit
  a config that boots cleanly. Shaping is one optional capability, not required.
argument-hint: "[api] [operations] [auth] [shaping]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/*)
---

# Generate a Gateway Config

Your job is to hand the user a runnable `config.yml` for [openapi-mcp-gateway](https://github.com/mroops0111/openapi-mcp-gateway), starting from nothing more than an API they name or a spec they point at. The user may be non-technical, so you own the OpenAPI details and confirm intent in plain language.

The output is a config. Shaping (`params` / `strategy` and JSONata) is one optional capability you reach for when an operation makes a poor tool as-is. A minimal config that just exposes the right operations, or only renames them, is a perfectly good result. Do not shape for its own sake.

## Reading the request

The user may pass an opening brief as arguments, and `$ARGUMENTS` holds it verbatim, for example `github, only the issue endpoints, bearer, shape:light`. When it is present, parse out four things:

- **api**: the API name or an OpenAPI spec URL or path.
- **operations**: which endpoints, or in plain terms what the user wants to do.
- **auth**: `bearer`, `api_key`, or `oauth2`.
- **shaping**: how much to reshape, from `none` (expose as-is) through `light` to `full`.

Treat every field as optional. Anything the brief does not pin down, infer a sensible default or ask, following the stages below. If there are no arguments, drive the whole thing from the user's message. Never block on a missing argument you can reasonably ask about.

Then run the three stages, and keep the user in the loop between them. Do not silently guess a whole config end to end.

## Stage 1: Acquire the spec

Get a machine-readable OpenAPI spec before doing anything else. Try these in order and tell the user which one succeeded and how confident you are.

- **Given a URL or file.** If the user hands you an OpenAPI URL or a local path, fetch it and confirm it parses. Highest confidence.
- **Find the official spec.** Search for the API's published OpenAPI or Swagger document. Many vendors host one (GitHub, Stripe, Asana). Prefer an official raw URL, since the gateway can read `spec:` straight from a URL.
- **Synthesize from docs.** When no official spec exists (Redmine is a common case), build a minimal spec covering only the operations the user actually needs, from the API's HTML reference. Flag this as lower confidence and list every assumption (base URL, auth style, required parameters) for the user to confirm.

Run `${CLAUDE_SKILL_DIR}/scripts/fetch_spec.sh <url-or-path>` to fetch and sanity-check a spec candidate.

Confirm the base URL and the auth style (`bearer`, `api_key`, or `oauth2`) before moving on. Never invent a token value. Reference it as an environment variable, for example `token: ${REDMINE_API_KEY}`.

## Stage 2: Select the operations, shape only where it helps

Do not expose every operation. Map what the user wants to a short list of operations, and confirm the list before writing anything.

Selection is done by a `policy` block, not by the `operations` map, and this trips people up. The tool surface is filtered by `policy.allow` and `policy.deny`, which are glob patterns matched against operation ids, or by `policy.marked_only`. The `operations` map only attaches overrides (name, description, shaping) to operations that already survived the policy, and it errors on an unknown id. So to expose just the chosen operations from a real multi-operation spec, set `policy.allow`. Without it, every operation in the spec becomes a tool, so pointing `spec` at a large upstream and listing three operations does not give three tools. When the spec is huge, a `policy.allow` glob or a synthesized minimal spec is how you keep the surface small.

Then decide, per operation, how much reshaping it needs. Prefer the lightest option that gives a good tool, and honour the requested shaping level from the brief.

- **Expose as-is.** If the raw operation already makes a clean tool, keep it in the surface via `policy.allow` and give it no `tool` block, or only a name and description override. This is the default, and the whole of `shaping: none`.
- **Shape it.** Reach for shaping when the operation exposes parameters the model should not set, speaks in cryptic values, or returns a bloated envelope. Then:
  - **Hide what the model should not choose.** Pinned scopes, pagination internals, locale, and content flags become injected constants, not inputs.
  - **Expose only the friendly inputs**, with `enum` where the raw API takes cryptic values, and short `description`s.
  - **Pick a strategy.** `replace` declares the params as the whole input schema and drops the spec's parameters, for a full reshape. `merge` layers onto the spec and keeps the rest, for a light touch. Naming a parameter the spec does not define is a startup error, so keep merged names honest.
  - **Write the JSONata.** `request` maps the friendly arguments onto the upstream request. `response` trims and renames the body.

JSONata idioms that carry most of the weight:

- `$lookup(table, key)` maps a friendly enum onto the raw API value, for example `popular` to `popularity.desc`.
- `[ results.{ ... } ]` forces a list, because a single-match projection unwraps to one object otherwise.
- `$merge([$, { ... }])` passes most arguments through and overrides only a few, where `$` is the whole input.

How the `request` result routes to the upstream call matters when you build it. A top-level key whose name matches a `{placeholder}` in the path fills that path segment. Of the rest, each key becomes a JSON body field for `POST` / `PUT` / `PATCH`, or a query parameter for `GET` / `DELETE`. A `null` value is dropped, so an omitted optional friendly argument leaves no trace upstream. To match an upstream that wants a wrapped body, like Redmine's `{"issue": {...}}`, nest the fields under that key in the `request` result, for example `{ "issue": { "subject": subject, "project_id": project_id } }`.

Study `examples/movie-shaping.yml` in the repo as the canonical shaped config, and its raw spec in `examples/specs/movie-api.yaml`. Use the TMDB example as your golden reference when shaping is warranted.

## Stage 3: Emit and verify

Write the config in the documented format, then verify it boots. A minimal server needs only `spec`, `base_url`, and `auth`; the `policy`, `operations`, and any `tool` shaping are additive.

```yaml
host: "127.0.0.1"
port: 8000
transport: streamable-http

servers:
  - name: <server-name>
    spec: <url-or-local-path>
    base_url: <upstream-base-url>          # required; a spec with a relative server needs the real host
    auth:
      type: bearer                          # or api_key / oauth2 / none
      token: ${SOME_TOKEN}
    policy:                                 # the selector: which operations become tools
      allow: ["<operation_id>", "..."]     # globs matched against operation ids; omit to expose all
    operations:                             # overrides only, applied to operations the policy kept
      <operation_id>:
        tool:                               # optional: omit for an as-is passthrough
          strategy: replace                 # or merge
          params:
            <friendly_param>:
              type: string
              enum: [...]                   # when the raw value is cryptic
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
      api_key_header: X-Redmine-API-Key
```

Both JSONata expressions compile at gateway startup, so a broken expression fails the boot with a message naming the side that broke. Use that as your verifier:

- Run `${CLAUDE_SKILL_DIR}/scripts/validate_config.sh <config.yml>` to boot the gateway against the config and report success or the compile error.
- On failure, read the named side (request or response), fix the JSONata, and re-run. Loop until it boots clean.

Hand the finished config to the user with a one-line summary of each tool, the environment variables they must set, and the exact run command:

```bash
uv add openapi-mcp-gateway
uv run openapi-mcp-gateway --config <their-config>.yml
```

## Boundaries

- You produce a config only. You do not start a long-running server for the user, and you do not wire the gateway into their Claude Code. The gateway is a neutral runtime the user points any MCP client at.
- When confidence is low (a synthesized spec, a guessed auth style), say so plainly and list what needs human confirmation. A wrong assumption should surface, not hide.
