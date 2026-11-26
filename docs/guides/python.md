---
description: Build a gateway in Python, expose marked routes of a FastAPI app as MCP tools, and mount the gateway into an app you already serve.
---
# Python and FastAPI

The gateway also works as a Python library. This guide covers building a gateway in code, exposing routes of a FastAPI app, and mounting the gateway into an app you already serve.

## Python API

`Gateway.add_server` registers a server with the same fields as a config entry, except `operations`:

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
    auth={"upstream": {"type": "bearer", "token": "${GITHUB_TOKEN}"}},
    policy={"allow": ["GET /repos/*"]},
)
gateway.run(port=8000)
```

`auth`, `policy`, `exposure` and `headers` take the same mappings as their YAML keys. To shape tools through `operations`, build a `ServerConfig` and pass it in a `GatewayConfig` to `Gateway.from_config` instead:

```python
from openapi_mcp_gateway import Gateway, GatewayConfig, ServerConfig

server = ServerConfig.model_validate({
    "name": "github",
    "spec": "./github-openapi.json",
    "operations": {
        "pulls/list-files": {"tool": {"name": "list_pull_request_files"}},
    },
})
Gateway.from_config(GatewayConfig(servers=[server])).run()
```

`GatewayConfig.from_yaml("config.yml")` loads a config file the same way.

## FastAPI Integration

`Gateway.from_fastapi` exposes the routes of a FastAPI app that you mark with `@mcp_tool`. It reads the app's own OpenAPI document, so there is no second spec. Calls go in-process through `httpx.ASGITransport`, with no network hop.

```python
from fastapi import FastAPI
from openapi_mcp_gateway import Gateway, mcp_tool

app = FastAPI()

@app.get("/items/{item_id}")
@mcp_tool(name="read_item", description="Read one item by its ID.")
def read_item(item_id: int):
    return {"id": item_id}

# Not marked, so not exposed.
@app.get("/internal/health")
def health():
    return {"ok": True}

Gateway.from_fastapi(app, name="myapp").run()
```

### Marking Routes

Two functions mark a route for exposure, and both take the same arguments.

| Argument | Default | Description |
|---|---|---|
| `name` | from the route | Tool name, in place of the OpenAPI-derived one |
| `description` | from the route | Tool description |
| `expose` | `True` | `False` opts the route out without removing the mark |

- **`@mcp_tool(...)`**: decorates a route you define.
- **`mark_tool(func, ...)`**: marks a route you cannot decorate, such as one from a third-party router. It returns `func`.

### `from_fastapi` Parameters

`Gateway.from_fastapi(app, ...)` takes these keyword arguments.

| Parameter | Default | Description |
|---|---|---|
| `name` | `fastapi` | Server name, which sets the mount path `/{name}` |
| `path_prefix` |  | Mount path, when it should differ from `/{name}` |
| `auth` | detected | An `AuthConfig` that replaces the detected auth |
| `timeout` | `90` | Call timeout in seconds |
| `passthrough_headers` | `("Authorization", "X-API-Key")` | Headers copied from the MCP request to the route. Auth the gateway sets takes precedence over a copied `Authorization`. |
| `config` | defaults | A `GatewayConfig` for process-wide settings, such as `url` or `store` |

### Auth in the FastAPI Integration

The gateway reads the app's `securitySchemes` to choose upstream auth.

- **OAuth Flow Declared**: `auth.upstream.type` becomes `passthrough`, and the MCP client's `Authorization` header reaches the route as is.
- **No OAuth Flow**: `auth.upstream.type` stays `none`.
- **Explicit `auth`**: an `AuthConfig` with credentials makes the gateway obtain its own upstream tokens instead.

[Architecture](../concepts/architecture.md#token-forwarding-policy) explains why `passthrough` belongs to this case only.

## Mounting Into an Existing App

`gateway.mount(app)` serves MCP beside your own routes. It attaches every MCP endpoint at its mount path and registers the OAuth and `.well-known` discovery routes those servers own:

```python
import contextlib

from fastapi import FastAPI
from openapi_mcp_gateway import Gateway, GatewayConfig, ServerConfig

gateway = Gateway.from_config(
    GatewayConfig(
        # The public URL, used for discovery and redirect URLs.
        url="https://your-app.example.com",
        servers=[ServerConfig(name="petstore", spec="petstore.json")],
    )
)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with gateway.lifespan():
        yield


app = FastAPI(lifespan=lifespan)
# Mounts /petstore/mcp plus its OAuth and .well-known routes.
gateway.mount(app)
```

> [!WARNING]
> Enter `gateway.lifespan()` in your app's lifespan. Starlette runs only the root app's lifespan, so without it the MCP session managers never start and every MCP request fails.

Set `GatewayConfig.url` to your app's public URL, so discovery documents and redirect URLs point at the right origin. The upstream OAuth callback of a server is `{mount_path}/auth/callback`, so keep your own routes clear of it. A mounted gateway checks the `Host` header only when `dns_rebinding_protection.allowed_hosts` lists hosts, as [Host and Origin Checks](deployment.md#host-and-origin-checks) shows.

## Related

Code takes the same fields as a config file, so the YAML guides and the field tables apply unchanged.

- **Guides**: [Authorization](authorization.md) for the `auth` mappings a server takes and [Tool Shaping](tool-shaping.md) for the `operations` a `ServerConfig` carries
- **Reference**: [Top-Level Fields](../reference/configuration.md#top-level-fields) for what `GatewayConfig` takes and [Per-Server Fields](../reference/configuration.md#per-server-fields) for what `add_server` and `ServerConfig` take
