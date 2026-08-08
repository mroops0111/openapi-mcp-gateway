# generate-gateway-config

A Claude Code plugin that generates an [openapi-mcp-gateway](https://github.com/mroops0111/openapi-mcp-gateway) config for you. Name an API, and it finds the OpenAPI spec, selects the operations you want, shapes them into clean MCP tools where that helps, and emits a `config.yml` that boots.

Shaping is optional. The output is a config, and a plain passthrough of the right operations is a valid result. The plugin produces a config only. It does not start or bundle the gateway server. Run the server yourself and point any MCP client at it.

## Install

```
/plugin marketplace add mroops0111/openapi-mcp-gateway
/plugin install generate-gateway-config@openapi-mcp-gateway
```

## Use

Ask Claude to generate a config, for example "generate a github config with only the issue endpoints, shaped for triage" or "connect Redmine as MCP tools". The skill runs three stages: acquire a spec, select and optionally shape the operations with you, and emit a verified config.

Then start the gateway:

```bash
uv add openapi-mcp-gateway
uv run openapi-mcp-gateway --config <your-config>.yml
```

## What it contains

- `skills/generate-gateway-config/SKILL.md` — the authoring principles and the three-stage pipeline.
- `skills/generate-gateway-config/scripts/fetch_spec.sh` — fetch and sanity-check a spec candidate.
- `skills/generate-gateway-config/scripts/validate_config.sh` — boot the gateway to verify a config compiles.
