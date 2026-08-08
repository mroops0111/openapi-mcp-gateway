# openapi-mcp-gateway (Claude Code plugin)

Skills for [openapi-mcp-gateway](https://github.com/mroops0111/openapi-mcp-gateway). Ships the `generate-config` skill, which generates a config for you: name an API, and it finds the OpenAPI spec, selects the operations you want, shapes them into clean MCP tools where that helps, and emits a `config.yml` that boots.

Shaping is optional. The output is a config, and a plain passthrough of the right operations is a valid result. The plugin produces a config only. It does not start or bundle the gateway server. Run the server yourself and point any MCP client at it.

## Install

```
/plugin marketplace add mroops0111/openapi-mcp-gateway
/plugin install openapi-mcp-gateway@openapi-mcp-gateway
```

## Use

Invoke it directly with arguments, or just describe what you want and let Claude trigger it.

```
/generate-config github, only the issue endpoints, bearer, shape:light
```

The four hinted slots are `[api] [operations] [auth] [shaping]`, all optional. The namespaced form `/openapi-mcp-gateway:generate-config` works too, and "connect Redmine as MCP tools" in plain language triggers the same skill. It runs three stages: acquire a spec, select and optionally shape the operations with you, and emit a verified config.

Then start the gateway:

```bash
uv add openapi-mcp-gateway
uv run openapi-mcp-gateway --config <your-config>.yml
```

## What it contains

- `skills/generate-config/SKILL.md` — the authoring principles and the three-stage pipeline.
- `skills/generate-config/scripts/fetch_spec.sh` — fetch and sanity-check a spec candidate.
- `skills/generate-config/scripts/validate_config.sh` — boot the gateway to verify a config compiles.
