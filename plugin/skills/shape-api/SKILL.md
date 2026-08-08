---
name: shape-api
description: >-
  Author an openapi-mcp-gateway config from an API the user names or points to.
  Use when the user wants to expose a REST API (Redmine, GitHub, an internal
  service, any OpenAPI backend) as MCP tools, asks to "connect", "integrate",
  "wire up", or "shape" an API into MCP, or needs help writing or fixing a
  gateway config.yml. Runs a three-stage pipeline: acquire an OpenAPI spec,
  shape the operations, and emit a config that boots cleanly.
---

# Shape an API into MCP Tools

Your job is to hand the user a runnable `config.yml` for [openapi-mcp-gateway](https://github.com/mroops0111/openapi-mcp-gateway), starting from nothing more than an API they name or a spec they point at. The user may be non-technical, so you own the OpenAPI and JSONata details and confirm intent in plain language.

Work in three stages, and keep the user in the loop between them. Do not silently guess a whole config end to end.

## Stage 1: Acquire the spec

Get a machine-readable OpenAPI spec before doing anything else. Try these in order and tell the user which one succeeded and how confident you are.

- **Given a URL or file.** If the user hands you an OpenAPI URL or a local path, fetch it and confirm it parses. Highest confidence.
- **Find the official spec.** Search for the API's published OpenAPI or Swagger document. Many vendors host one (GitHub, Stripe, Asana). Prefer an official raw URL, since the gateway can read `spec:` straight from a URL.
- **Synthesize from docs.** When no official spec exists (Redmine is a common case), build a minimal spec covering only the operations the user actually needs, from the API's HTML reference. Flag this as lower confidence and list every assumption (base URL, auth style, required parameters) for the user to confirm.

Run `scripts/fetch_spec.sh <url-or-path>` to fetch and sanity-check a spec candidate.

Confirm the base URL and the auth style (`bearer`, `api_key`, or `oauth2`) before moving on. Never invent a token value. Reference it as an environment variable, for example `token: ${REDMINE_API_KEY}`.

## Stage 2: Shape the operations

Do not expose every operation. Ask the user what they want to do with the API in their own words, map that to a short list of operations, and confirm the list before shaping.

For each chosen operation, decide the model-facing surface:

- **Hide what the model should not choose.** Pinned scopes, pagination internals, locale, and content flags become injected constants, not inputs.
- **Expose only the friendly inputs**, with `enum` where the raw API takes cryptic values, and short `description`s.
- **Pick a strategy.** `replace` declares the params as the whole input schema and drops the spec's parameters, for a full reshape. `merge` layers onto the spec and keeps the rest, for a light touch. Naming a parameter the spec does not define is a startup error, so keep merged names honest.
- **Write the JSONata.** `request` maps the friendly arguments onto the upstream request. `response` trims and renames the body.

JSONata idioms that carry most of the weight:

- `$lookup(table, key)` maps a friendly enum onto the raw API value, for example `popular` to `popularity.desc`.
- `[ results.{ ... } ]` forces a list, because a single-match projection unwraps to one object otherwise.
- `$merge([$, { ... }])` passes most arguments through and overrides only a few, where `$` is the whole input.

Study `examples/movie-shaping.yml` in the repo as the canonical shaped config, and its raw spec in `examples/specs/movie-api.yaml`. Use the TMDB example as your golden reference when in doubt.

## Stage 3: Emit and verify

Write the config in the documented format, then verify it boots.

```yaml
host: "127.0.0.1"
port: 8000
transport: streamable-http

servers:
  - name: <server-name>
    spec: <url-or-local-path>
    base_url: <upstream-base-url>
    auth:
      type: bearer
      token: ${SOME_TOKEN}
    operations:
      <operation_id>:
        tool:
          strategy: replace   # or merge
          params:
            <friendly_param>:
              type: string
              enum: [...]        # when the raw value is cryptic
              default: ...
              description: ...
          request: |
            { ... JSONata ... }
          response: |
            [ ... JSONata ... ]
```

Both JSONata expressions compile at gateway startup, so a broken expression fails the boot with a message naming the side that broke. Use that as your verifier:

- Run `scripts/validate_config.sh <config.yml>` to boot the gateway against the config and report success or the compile error.
- On failure, read the named side (request or response), fix the JSONata, and re-run. Loop until it boots clean.

Hand the finished config to the user with a one-line summary of each shaped tool, the environment variables they must set, and the exact run command:

```bash
uv add openapi-mcp-gateway
uv run openapi-mcp-gateway --config <their-config>.yml
```

## Boundaries

- You produce a config only. You do not start a long-running server for the user, and you do not wire the gateway into their Claude Code. The gateway is a neutral runtime the user points any MCP client at.
- When confidence is low (a synthesized spec, a guessed auth style), say so plainly and list what needs human confirmation. A wrong assumption should surface, not hide.
