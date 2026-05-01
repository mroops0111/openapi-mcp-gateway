# OpenAPI MCP Gateway

[![CI](https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/openapi-mcp-gateway.svg)](https://pypi.org/project/openapi-mcp-gateway/)
[![Python Version](https://img.shields.io/pypi/pyversions/openapi-mcp-gateway.svg)](https://pypi.org/project/openapi-mcp-gateway/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Turn any OpenAPI specification into a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server with a single command.

```bash
openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json
# → MCP server live at http://127.0.0.1:8000/api/mcp
```

No code generation, no scaffolding — point it at a spec and the gateway dynamically registers each operation as an MCP tool. Works with multiple APIs at once, handles Bearer / API key / OAuth2 authentication, and runs over Streamable HTTP, SSE, or stdio.

---

## Table of Contents

- [Why](#why)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Authentication](#authentication)
- [Configuration](#configuration)
- [Python API](#python-api)
- [Claude Desktop / IDE Integration](#claude-desktop--ide-integration)
- [Architecture](#architecture)
- [Development](#development)
- [License](#license)

---

## Why

LLM agents need tools, and most production APIs already document themselves with OpenAPI. Writing a bespoke MCP server for each API duplicates that work. This gateway closes the gap:

- **Zero glue code** — operations become MCP tools at startup, derived from the spec
- **Auth that just works** — Bearer / API key / OAuth2, with `${ENV_VAR}` interpolation
- **Multi-tenant** — one gateway, many APIs, each mounted under `/{name}/mcp`
- **Production-ready** — Redis-backed token store, CORS, policy filters, Streamable HTTP

## Installation

```bash
pip install openapi-mcp-gateway
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add openapi-mcp-gateway
```

Optional extras:

```bash
pip install "openapi-mcp-gateway[redis]"   # Redis token store
```

Requires Python 3.11+.

## Quick Start

### 1. Public API, no auth

```bash
openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json --name petstore
```

Connect an MCP client to `http://127.0.0.1:8000/petstore/mcp`.

### 2. Bearer token

```bash
export GITHUB_TOKEN="ghp_..."
openapi-mcp-gateway \
    --spec https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json \
    --name github \
    --auth-type bearer \
    --auth-token '${GITHUB_TOKEN}'
```

### 3. OAuth2 (auto-detected from spec)

```bash
export ASANA_CLIENT_ID="..." ASANA_CLIENT_SECRET="..."
openapi-mcp-gateway \
    --spec https://raw.githubusercontent.com/Asana/openapi/master/defs/asana_oas.yaml \
    --name asana \
    --auth-type oauth2 \
    --auth-client-id '${ASANA_CLIENT_ID}' \
    --auth-client-secret '${ASANA_CLIENT_SECRET}' \
    --auth-scopes "openid,email,profile,users:read,workspaces:read"
```

The MCP client redirects through the gateway, the gateway redirects through Asana, and the upstream token is mapped behind the scenes.

### 4. Multiple APIs at once

```yaml
# servers.yml
host: "0.0.0.0"
port: 8000

servers:
  - name: petstore
    spec: https://petstore3.swagger.io/api/v3/openapi.json

  - name: github
    spec: ./openapi/github.json
    auth:
      type: bearer
      token: ${GITHUB_TOKEN}
    policy:
      allow: ["GET /repos/*", "GET /users/*"]
```

```bash
openapi-mcp-gateway --config servers.yml
```

Full runnable examples live in [`examples/`](examples/) — each YAML documents its prerequisites at the top.

## Authentication

| Type | Use case | Required fields |
|------|----------|-----------------|
| `none` | Public APIs | — |
| `bearer` | Personal access tokens, API tokens | `token` |
| `api_key` | Custom-header API keys | `token`, `api_key_header` |
| `oauth2` | Per-user delegated access | `client_id`, `client_secret`, `scopes` |

All string fields support `${ENV_VAR}` and `${ENV_VAR:-default}` interpolation, resolved at request time:

```yaml
auth:
  type: bearer
  token: ${GITHUB_TOKEN}
```

### OAuth2 details

For OAuth2, the gateway acts as a bridge between the MCP client and the upstream API:

```
MCP client ──┐                                        ┌── upstream API
             │                                        │
             ▼          1. authorize                  ▼
        ┌────────┐ ─────────────────────────► ┌─────────────┐
        │Gateway │                            │ OAuth2 IdP  │
        │        │ ◄───────────────────────── │             │
        └────────┘     2. code → tokens       └─────────────┘
             │
             │  3. issue MCP token (mapped to upstream token)
             ▼
        MCP client uses MCP token; gateway swaps it for the upstream token on every tool call.
```

The gateway auto-detects `authorizationUrl` / `tokenUrl` / `scopes` from the spec's `securitySchemes`. Override with `auth.authorization_url` and `auth.token_url` if the spec is incomplete.

`.well-known` discovery endpoints are exposed per server:
- `GET /.well-known/oauth-authorization-server/{name}` — RFC 8414
- `GET /.well-known/oauth-protected-resource/{name}/mcp` — RFC 9728

## Configuration

### Top-level

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | string | `0.0.0.0` | Bind host |
| `port` | int | `8000` | Bind port |
| `url` | string | `http://{host}:{port}` | Public-facing URL (used for OAuth callbacks) |
| `transport` | string | `streamable-http` | `sse`, `streamable-http`, or `stdio` |
| `debug` | bool | `false` | Verbose logging |
| `enable_docs` | bool | `false` | Expose `/docs` and `/redoc` |
| `cors.*` | — | `["*"]` | `allow_origins`, `allow_methods`, `allow_headers`, `expose_headers` |
| `store.type` | string | `memory` | `memory` or `redis` |
| `store.redis_url` | string | `redis://localhost:6379` | Redis URL when `store.type: redis` |
| `store.key_prefix` | string | `mcp_gw` | Redis key prefix |

### Per-server

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Unique identifier; mount path defaults to `/{name}` |
| `spec` | string | required | Path or URL to OpenAPI document (JSON or YAML) |
| `base_url` | string | from spec | Override the upstream base URL |
| `path_prefix` | string | `{name}` | Override the mount path |
| `auth.*` | — | — | See [Authentication](#authentication) |
| `policy.allow` | list | — | Only expose matching operations |
| `policy.deny` | list | — | Exclude matching operations |
| `policy.marked_only` | bool | `false` | Only expose ops with `x-mcp-integration` |
| `timeout` | float | `90` | HTTP timeout in seconds |

### Policy patterns

Patterns use `fnmatch` syntax against either:
- Operation ID: `getUsers`, `create*`, `*User*`
- Method + path: `GET /users/*`, `DELETE *`

### `x-mcp-integration` marker

Mark operations directly in your OpenAPI spec for selective exposure:

```yaml
paths:
  /users:
    get:
      operationId: listUsers
      x-mcp-integration:
        expose:
          tool: {}
```

Combined with `policy.marked_only: true`, only marked operations become tools.

## Python API

Use the gateway as a library inside your own Python application:

```python
from openapi_mcp_gateway import Gateway

gateway = Gateway()
gateway.add_server(
    name="petstore",
    spec="https://petstore3.swagger.io/api/v3/openapi.json",
)
gateway.add_server(
    name="github",
    spec="./github-openapi.json",
    auth={"type": "bearer", "token": "${GITHUB_TOKEN}"},
    policy={"allow": ["GET /repos/*"]},
)
gateway.run(port=8000)
```

Or mount into an existing FastAPI app:

```python
from fastapi import FastAPI
from openapi_mcp_gateway import Gateway

app = FastAPI()
gateway = Gateway()
gateway.add_server(name="petstore", spec="petstore.json")
gateway.mount(app)
```

## Claude Desktop / IDE Integration

Run over stdio for local desktop clients:

```json
{
  "mcpServers": {
    "petstore": {
      "command": "openapi-mcp-gateway",
      "args": ["--spec", "/abs/path/to/openapi.json", "--transport", "stdio"]
    }
  }
}
```

## Architecture

The gateway parses each spec into operation metadata, generates a synthetic Python signature per operation (sanitizing tool/parameter names so identifiers like `meta/root` or `enterprise-team` round-trip cleanly), and delegates to FastMCP for transport. A per-server OAuth provider bridges MCP-side tokens to upstream-side tokens through a swappable token store.

## CLI Reference

```
Usage: openapi-mcp-gateway [OPTIONS]

Options:
  --spec TEXT                      Path or URL to a single OpenAPI spec.
  --config PATH                    Path to a YAML config file.
  --name TEXT                      Server name (default: api).
  --base-url TEXT                  Override the upstream API base URL.
  --transport [sse|streamable-http|stdio]
                                   MCP transport protocol.
  --host TEXT                      Bind host (default: 0.0.0.0).
  --port INTEGER                   Bind port (default: 8000).
  --auth-type [none|bearer|api_key|oauth2]
                                   Authentication type.
  --auth-token TEXT                Token or ${ENV_VAR} reference.
  --auth-client-id TEXT            OAuth2 client ID or ${ENV_VAR}.
  --auth-client-secret TEXT        OAuth2 client secret or ${ENV_VAR}.
  --auth-scopes TEXT               Comma-separated OAuth2 scopes.
  --auth-authorization-url TEXT    OAuth2 authorization URL.
  --auth-token-url TEXT            OAuth2 token URL.
  --help                           Show this message and exit.
```

## Development

```bash
git clone https://github.com/mroops0111/openapi-mcp-gateway
cd openapi-mcp-gateway

uv sync --extra dev          # install dev dependencies
uv run pytest                # run tests
uv run ruff check            # lint
uv run ruff format           # format
```

Patches welcome — please open an issue first for non-trivial changes.

## License

[MIT](LICENSE) © YunTai Yang
