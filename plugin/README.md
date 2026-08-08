# shape-api

A Claude Code plugin that authors an [openapi-mcp-gateway](https://github.com/mroops0111/openapi-mcp-gateway) config for you. Name an API, and it finds the OpenAPI spec, shapes the operations into clean MCP tools, and emits a `config.yml` that boots.

The plugin produces a config only. It does not start or bundle the gateway server. Run the server yourself and point any MCP client at it.

## Install

```
/plugin marketplace add mroops0111/openapi-mcp-gateway
/plugin install shape-api@openapi-mcp-gateway
```

## Use

Ask Claude to connect an API, for example "help me expose Redmine as MCP tools". The `shape-api` skill runs three stages: acquire a spec, shape the operations with you, and emit a verified config.

Then start the gateway:

```bash
uv add openapi-mcp-gateway
uv run openapi-mcp-gateway --config <your-config>.yml
```

## What it contains

- `skills/shape-api/SKILL.md` — the authoring principles and the three-stage pipeline.
- `skills/shape-api/scripts/fetch_spec.sh` — fetch and sanity-check a spec candidate.
- `skills/shape-api/scripts/validate_config.sh` — boot the gateway to verify a config compiles.
