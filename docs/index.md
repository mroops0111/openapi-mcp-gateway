---
description: Mount OpenAPI specs and FastAPI apps as MCP servers from one YAML file, with per-API auth, operation scopes, tool shaping and dynamic exposure.
---
# openapi-mcp-gateway

Mount any OpenAPI (Swagger) spec as a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server, or expose an existing FastAPI app the same way. One YAML file runs several APIs in one process, each with its own endpoint and auth, with no code to write.

```bash
uvx openapi-mcp-gateway serve \
    --spec https://petstore3.swagger.io/api/v3/openapi.json --name petstore
```

[Getting Started](getting-started.md) covers the steps from this command to a connected client and a config file.

## Features

Features are grouped by what you want to do with an API.

### Exposure

Choose which operations the model sees, and how. Filters, resources and search are set up in [Exposure](guides/exposure.md), the rules of dynamic exposure are in [Dynamic Exposure](concepts/dynamic-exposure.md), and the ways to keep tools from filling the model's context are compared in [Context Efficiency](concepts/context-efficiency.md).

- **Several APIs in One Process**: each server in the config gets its own endpoint and auth.
- **Operation Filters**: allow and deny lists by operation ID or method and path.
- **Resources**: read-only GETs become MCP resources the client can surface.
- **Dynamic Exposure**: a spec with 1,000+ operations goes behind three search tools.

### Tool Shaping

Turn raw operations into tools a model uses well, without forking the spec. [Tool Shaping](guides/tool-shaping.md) covers all three.

- **Names and Descriptions**: replace an unclear `operationId` and an empty description.
- **Parameters and Transforms**: hide or pin parameters, and rewrite requests and responses with JSONata.
- **Media**: base64 fields arrive as image, audio or file content.

### Security

Decide who may call each endpoint and what reaches the API behind it. Start with [Authorization](guides/authorization.md), then [Scopes](guides/scopes.md). [Security](concepts/security.md) lists the access layers and built-in safeguards.

- **Authorization Modes**: from a fixed API key to per-user sign-in and token exchange with your identity provider.
- **Scopes per Operation**: each client gets only the operations its scopes open, and can ask for more scopes when a call needs them.

### Config Authoring

Write a config and check it before you rely on it. [Authoring With AI](guides/authoring-with-ai.md) covers all three.

- **Configs Written by AI**: a Claude Code skill writes the config from one sentence.
- **Dry Run**: validate a config and list what it exposes, without starting a server.
- **Your Own Tasks**: the `eval` command runs your tasks under your config and reports success and cost.

## FastAPI Apps

An app you already run exposes its routes with one decorator, in the same process, as [Python and FastAPI](guides/python.md) shows.

## Deployment

The container image, Redis for several replicas, and a reverse proxy in front are covered in [Deployment](guides/deployment.md).

## Evaluation

On Claude Haiku, dynamic exposure completed every benchmark task on 8% of the input tokens of one tool per operation. [Evaluation](evaluation.md) shows the method and full results.

## Architecture

Each tool call is authorized, shaped and sent upstream, and its response is reshaped on the way back. [Architecture](concepts/architecture.md) describes each step of a call.

![openapi-mcp-gateway architecture. An MCP client calls an MCP endpoint, one per API, which config.yml builds at startup. Each call is authorized first, by its token and then by the scopes the operation needs, getting an upstream token from the API's authorization server or your identity provider when the mode calls for one. The request is shaped by params and JSONata and sent with the upstream credential, over HTTP to a REST API or over ASGI to a FastAPI app in the same process. The response comes back reshaped by JSONata.](assets/architecture-light.svg#only-light) ![openapi-mcp-gateway architecture, as described above.](assets/architecture-dark.svg#only-dark)
