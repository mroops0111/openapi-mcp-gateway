# OpenAPI MCP Gateway

[![CI](https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/mroops0111/openapi-mcp-gateway/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/openapi-mcp-gateway.svg)](https://pypi.org/project/openapi-mcp-gateway/)
[![Python Version](https://img.shields.io/pypi/pyversions/openapi-mcp-gateway.svg)](https://pypi.org/project/openapi-mcp-gateway/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Turn any OpenAPI specification into a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server with a single command.

```bash
openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json
# Server live at http://127.0.0.1:8000/api/mcp
```

- **No code generation.** Point it at a spec and every operation becomes an MCP tool.
- **Multi-API.** Run several OpenAPI services in one process, each on its own mount path.
- **Auth built in.** Bearer, API Key, and OAuth2 (with per-user delegation).
- **Flexible transport.** Streamable HTTP, SSE, or stdio.

---

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Authentication](#authentication)
- [Configuration](#configuration)
- [Python API](#python-api)
- [License](#license)

---

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

### 1. Public API, No Auth

```bash
openapi-mcp-gateway --spec https://petstore3.swagger.io/api/v3/openapi.json --name petstore
```

Connect an MCP client to `http://127.0.0.1:8000/petstore/mcp`.

### 2. Bearer Token

```bash
export GITHUB_TOKEN="ghp_..."
openapi-mcp-gateway \
    --spec https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json \
    --name github \
    --auth-type bearer \
    --auth-token '${GITHUB_TOKEN}'
```

### 3. OAuth2 (Auto-Detected from Spec)

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

### 4. Multiple APIs at Once

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

Runnable examples live in [`examples/`](examples/). Each YAML lists its prerequisites at the top.

### 5. Local Desktop Client (stdio)

For Claude Desktop, IDE integrations, or any MCP client that prefers stdio:

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

## Authentication

| Type | Use Case | Required Fields |
|------|----------|-----------------|
| `none` | Public APIs |  |
| `bearer` | Personal access tokens, API tokens | `token` |
| `api_key` | API key in `X-API-Key` header (override with `auth.api_key_header`) | `token` |
| `oauth2` | Per-user delegated access | `client_id`, `client_secret` |

All string fields support `${ENV_VAR}` and `${ENV_VAR:-default}` interpolation, resolved at request time:

```yaml
auth:
  type: bearer
  token: ${GITHUB_TOKEN}
```

### OAuth2 Details

`authorizationUrl`, `tokenUrl`, and `scopes` are read from the spec's `securitySchemes`. Override `auth.authorization_url`, `auth.token_url`, or `auth.scopes` when the spec is incomplete.

## Configuration

Run `openapi-mcp-gateway --help` for the CLI reference. For YAML config, the [Quick Start](#quick-start) examples cover most setups; the full field reference:

<details>
<summary><b>Top-Level Fields</b></summary>

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | string | `0.0.0.0` | Bind address (`0.0.0.0` = all interfaces). Clients on the same machine usually open `http://localhost:{port}` or `http://127.0.0.1:{port}`. |
| `port` | int | `8000` | Bind port |
| `url` | string | *(empty)* | Public base URL for OAuth redirects and discovery. When unset: `http://localhost:{port}` if `host` is `0.0.0.0`, otherwise `http://{host}:{port}`. Override when your registered redirect URI uses another host (tunnel, reverse proxy, etc.). |
| `transport` | string | `streamable-http` | `sse`, `streamable-http`, or `stdio` |
| `store.type` | string | `memory` | `memory` or `redis` |
| `store.redis_url` | string | `redis://localhost:6379` | Redis URL when `store.type: redis` |
| `logging.level` | string | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `logging.format` | string | `text` | `text` or `json` |
| `logging.file` | string |  | Mirror logs to this file |
| `servers` | list | required | List of per-server config entries |

</details>

<details>
<summary><b>Per-Server Fields</b></summary>

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Unique identifier; mount path defaults to `/{name}` |
| `spec` | string | required | Path or URL to OpenAPI document (JSON or YAML) |
| `base_url` | string | from spec | Override the upstream base URL |
| `auth.*` |  |  | See [Authentication](#authentication) |
| `policy.allow` | list |  | Only expose matching operations |
| `policy.deny` | list |  | Exclude matching operations |
| `timeout` | float | `90` | HTTP timeout in seconds |

</details>

### Filtering Operations

Use `policy.allow` and `policy.deny` with `fnmatch` syntax against operation IDs (`getUsers`, `create*`) or method + path (`GET /users/*`):

```yaml
policy:
  allow: ["GET /repos/*"]
  deny:  ["GET /repos/*/actions/secrets*"]
```

Or mark operations directly in the spec and enable `marked_only`:

```yaml
# openapi.yml
paths:
  /users:
    get:
      operationId: listUsers
      x-mcp-integration:
        expose:
          tool: {}
```
```yaml
# servers.yml
policy:
  marked_only: true
```

Filters apply in order: `marked_only`, then `allow`, then `deny`.

### Logging

Configure via the `logging.*` YAML keys or via CLI flags. `-v` and `-q` are shortcuts for `DEBUG` and `WARNING`.

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

## License

[MIT](LICENSE)
