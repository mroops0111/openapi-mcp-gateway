![openapi-mcp-gateway. Point one YAML file at your OpenAPI specs, and each API gets its own MCP endpoint and auth.](docs/assets/cover.svg)

<p align="center">
  <a href="https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml"><img src="https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/openapi-mcp-gateway/"><img src="https://img.shields.io/pypi/v/openapi-mcp-gateway.svg?v=1" alt="PyPI version"></a>
  <a href="https://pepy.tech/projects/openapi-mcp-gateway"><img src="https://static.pepy.tech/badge/openapi-mcp-gateway/month" alt="PyPI Downloads"></a>
  <a href="https://pypi.org/project/openapi-mcp-gateway/"><img src="https://img.shields.io/pypi/pyversions/openapi-mcp-gateway.svg?v=1" alt="Python Version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

Mount any OpenAPI (Swagger) spec as a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server from one YAML file, with no code to write. Per-user OAuth and per-operation scopes give fine-grained access control, and tool shaping and dynamic exposure give context efficiency on large APIs. One process runs several APIs, each with its own endpoint and auth.

With [uv](https://docs.astral.sh/uv/getting-started/installation/) installed, one command serves the Petstore API:

```bash
uvx openapi-mcp-gateway serve --spec https://petstore3.swagger.io/api/v3/openapi.json --name petstore
```

Then connect any MCP client, such as Claude Code, Cursor or VS Code, to `http://127.0.0.1:8000/petstore/mcp`. [Getting Started](https://mroops0111.github.io/openapi-mcp-gateway/getting-started/) continues from there to a config file with several APIs.

## Highlights

Five things set it apart from a server generated once from a spec.

- **One Gateway for Many APIs**: one YAML file serves several OpenAPI specs, each at its own MCP endpoint with its own auth.
- **Per-User OAuth**: users sign in to the API through the gateway, or bring a token from your identity provider for the gateway to exchange.
- **Fine-Grained Access Control**: scopes assigned per operation decide which tools each client can list and call.
- **Tool Shaping**: rename operations, hide or fix parameters, and rewrite requests and responses with JSONata.
- **Dynamic Exposure**: an API with 1,000+ operations is served as three tools that search, describe and call them.

## Architecture

Each tool call is authorized, shaped and sent upstream, and its response is reshaped on the way back.

![openapi-mcp-gateway architecture. An MCP client calls an MCP endpoint, one per API, which config.yml builds at startup. Each call is authorized first, by its token and then by the scopes the operation needs, getting an upstream token from the API's authorization server or your identity provider when the mode calls for one. The request is shaped by params and JSONata and sent with the upstream credential, over HTTP to a REST API or over ASGI to a FastAPI app in the same process. The response comes back reshaped by JSONata.](docs/assets/architecture-light.svg#gh-light-mode-only) ![openapi-mcp-gateway architecture, as described above.](docs/assets/architecture-dark.svg#gh-dark-mode-only)

## Documentation

See the [documentation](https://mroops0111.github.io/openapi-mcp-gateway/) for the full guides, concepts and reference.

- [Getting Started](https://mroops0111.github.io/openapi-mcp-gateway/getting-started/) covers installation, a connected client and a config file
- [Guides](https://mroops0111.github.io/openapi-mcp-gateway/guides/authoring-with-ai/) covers AI authoring, exposure, tool shaping, authorization, scopes, identity providers, deployment and Python
- [Concepts](https://mroops0111.github.io/openapi-mcp-gateway/concepts/architecture/) covers the request path, security, context efficiency, the scope model, token lifecycles and dynamic exposure
- [Reference](https://mroops0111.github.io/openapi-mcp-gateway/reference/configuration/) lists every config field and CLI flag
- [Evaluation](https://mroops0111.github.io/openapi-mcp-gateway/evaluation/) shows how the gateway is measured and what it gains

## License

[MIT](LICENSE)
