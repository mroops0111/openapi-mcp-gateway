![openapi-mcp-gateway. Point one YAML file at your OpenAPI specs, and each API gets its own MCP endpoint and auth.](docs/assets/cover.svg)

<p align="center">
  <a href="https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml"><img src="https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/openapi-mcp-gateway/"><img src="https://img.shields.io/pypi/v/openapi-mcp-gateway.svg?v=1" alt="PyPI version"></a>
  <a href="https://pepy.tech/projects/openapi-mcp-gateway"><img src="https://static.pepy.tech/badge/openapi-mcp-gateway/month" alt="PyPI Downloads"></a>
  <a href="https://pypi.org/project/openapi-mcp-gateway/"><img src="https://img.shields.io/pypi/pyversions/openapi-mcp-gateway.svg?v=1" alt="Python Version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

Mount any OpenAPI (Swagger) spec as a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server, or expose an existing FastAPI app the same way. One YAML file runs several APIs in one process, each with its own endpoint and auth, with no code to write.

With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, one command serves the Petstore API:

```bash
uvx openapi-mcp-gateway serve --spec https://petstore3.swagger.io/api/v3/openapi.json --name petstore
```

Then connect any MCP client, such as Claude Code, Cursor or VS Code, to `http://127.0.0.1:8000/petstore/mcp`. [Read the documentation](https://mroops0111.github.io/openapi-mcp-gateway/), or start with [Getting Started](https://mroops0111.github.io/openapi-mcp-gateway/getting-started/).

## Highlights

Four things set it apart from a server generated once from a spec. The numbers come from [Evaluation](https://mroops0111.github.io/openapi-mcp-gateway/evaluation/).

- **Many APIs, No Code**: one YAML file mounts GitHub, an OAuth2 SaaS and your internal API side by side, and reshapes their tools with JSONata instead of a fork, so adding an API is a config change.
- **Authorization per Operation**: the gateway signs users in itself or accepts tokens from your identity provider, and grants access per operation, so each client reaches only the tools its task needs.
- **Large APIs That Fit**: a spec with 1,000+ operations goes behind three search tools, so the model's context stays free for the task. On Claude Haiku this completed every benchmark task on 8% of the input tokens of one tool per operation. One tool per operation could not fit one of the scenarios in the context window at all.
- **Configs Written by AI**: a Claude Code skill writes the config from one sentence, so you start from a working config rather than a raw spec. On Claude Haiku its configs used over 85% fewer input tokens than one tool per operation and passed 92% of runs.

## Architecture

Each tool call is authorized, shaped and sent upstream, and its response is reshaped on the way back.

![openapi-mcp-gateway architecture. An MCP client calls an MCP endpoint, one per API, which config.yml builds at startup. Each call is authorized first, by its token and then by the scopes the operation needs, getting an upstream token from the API's authorization server or your identity provider when the mode calls for one. The request is shaped by params and JSONata and sent with the upstream credential, over HTTP to a REST API or over ASGI to a FastAPI app in the same process. The response comes back reshaped by JSONata.](docs/assets/architecture-light.svg#gh-light-mode-only) ![openapi-mcp-gateway architecture, as described above.](docs/assets/architecture-dark.svg#gh-dark-mode-only)

## Documentation

The full documentation lives at [mroops0111.github.io/openapi-mcp-gateway](https://mroops0111.github.io/openapi-mcp-gateway/).

- [Getting Started](https://mroops0111.github.io/openapi-mcp-gateway/getting-started/) covers installation, a connected client and a config file
- [Guides](https://mroops0111.github.io/openapi-mcp-gateway/guides/authoring-with-ai/) covers AI authoring, exposure, tool shaping, authorization, scopes, identity providers, deployment and Python
- [Concepts](https://mroops0111.github.io/openapi-mcp-gateway/concepts/architecture/) covers the request path, security, context efficiency, the scope model, token lifecycles and dynamic exposure
- [Reference](https://mroops0111.github.io/openapi-mcp-gateway/reference/configuration/) lists every config field and CLI flag
- [Evaluation](https://mroops0111.github.io/openapi-mcp-gateway/evaluation/) shows how the gateway is measured and what it gains

## License

[MIT](LICENSE)
