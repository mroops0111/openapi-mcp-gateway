---
description: Run the gateway as a service, with the container image, a shared Redis token store, a public URL, Host and Origin checks, and stdio.
---
# Deployment

This guide covers running the gateway as a service, in a container or behind a proxy, and the settings that decide who can reach it.

## Container Image

Each release publishes an image to GHCR. Put `config.yml`, and any local spec it names, in a directory mounted at `/config`:

```bash
docker run -p 8000:8000 \
  --mount type=bind,src="$PWD/config",dst=/config,readonly \
  -e GITHUB_TOKEN \
  ghcr.io/mroops0111/openapi-mcp-gateway:latest
```

The image behaves as follows.

- **Extras**: `redis` and `oidc` are included, so every auth mode and the shared store work. `embeddings` and `eval` are not, so semantic search needs an image built with them, as the [Dockerfile](https://github.com/mroops0111/openapi-mcp-gateway/blob/master/Dockerfile) explains.
- **Listen Address**: the command pins `--host 0.0.0.0 --port 8000` over the config. Publish another host port with `-p <port>:8000`.
- **Relative Specs**: the working directory is `/config`, so a relative `spec` path resolves next to `config.yml`.
- **Health Check**: `GET /healthz` returns `status` and, per server, its name, mount path, spec title and auth summary. The image's `HEALTHCHECK` polls it.

## Shared Token Store

The gateway keeps OAuth state in memory by default, so it is lost on restart and not shared between replicas. Redis keeps it shared. It holds upstream tokens, MCP tokens and client registrations, never MCP sessions. The `token_exchange` cache and the `client_credentials` service token stay in each process, never in Redis. A server whose MCP endpoint is open stores nothing and can stay on `memory`.

```yaml
store:
  type: redis
  redis_url: redis://redis:6379
  # Change it when several gateways share one Redis.
  key_prefix: mcp_gw
```

[`compose.yaml`](https://github.com/mroops0111/openapi-mcp-gateway/blob/master/compose.yaml) runs the image beside Redis, reading `./config`.

## Public URL

`url` is the address clients use to reach the gateway, which OAuth redirect URLs and discovery documents carry. Its default, given in [Top-Level Fields](../reference/configuration.md#top-level-fields), points at the local machine and works only there. Behind a reverse proxy, tunnel or load balancer, set it to the public origin:

```yaml
url: https://mcp.example.com
```

An upstream OAuth app then needs `{url}{mount_path}/auth/callback` registered as its redirect URI, such as `https://mcp.example.com/github/auth/callback`. `{mount_path}` is the server's `path_prefix`, or `/{name}` when `path_prefix` is unset.

## Host and Origin Checks

DNS rebinding is an attack where a web page makes a hostname it controls resolve to your machine, so the visitor's browser sends requests to a local server. The gateway refuses it by checking the `Host` header, and the `Origin` header for browser clients.

| Bind | `allowed_hosts` Empty | `allowed_hosts` Set |
|---|---|---|
| Loopback, such as `127.0.0.1` | Only loopback names accepted | Only listed hosts accepted |
| Any other, such as `0.0.0.0` | Any `Host` accepted | Only listed hosts accepted |
| Mounted in your app | Any `Host` accepted | Only listed hosts accepted |

The default bind, given in [Top-Level Fields](../reference/configuration.md#top-level-fields), is not loopback, so list your hosts when the gateway is reachable from a network:

```yaml
dns_rebinding_protection:
  allowed_hosts: ["mcp.example.com", "localhost:*"]
  # Needs allowed_hosts.
  allowed_origins: ["https://app.example.com"]
```

`cors` sets which browser origins may call the gateway, and its defaults in [Top-Level Fields](../reference/configuration.md#top-level-fields) accept any. Narrow `cors.allow_origins` when only known web clients should call it.

## Stdio

`--transport stdio` lets a desktop client launch the gateway as a child process. It has two limits.

- **One Server**: stdio serves exactly one server, so a config with several fails to start.
- **No MCP Auth**: a stdio client presents no token, so a server that checks MCP tokens, such as one under `authorization_code`, is refused. A shared credential, such as static `credentials`, works.

## Security Notes

Check these settings before you expose the gateway to a network. [Security](../concepts/security.md) lists the access layers and safeguards the gateway applies, and what it leaves to the deployment.

> [!WARNING]
> A server the dry run shows as `mcp auth open` checks no MCP token, so keep it on loopback or add MCP auth, as [Mode Selection](authorization.md#mode-selection) warns.

> [!WARNING]
> Use `passthrough` only for the [FastAPI integration](python.md#auth-in-the-fastapi-integration), since [Token Forwarding Policy](../concepts/architecture.md#token-forwarding-policy) forbids it against a separate API.

- **Debug Mode**: keep `debug: false`, since debug responses include tracebacks.
- **Docs Endpoints**: `enable_docs: true` publishes the gateway's own `/docs` and `/openapi.json`, which most deployments do not need.

## Related

The security page states what the gateway leaves to a deployment, and the reference lists the process-wide fields and flags.

- **Concepts**: [Out of Scope](../concepts/security.md#out-of-scope) for what the gateway leaves to the proxy and the issuer
- **Reference**: [`serve` Command](../reference/cli.md#serve-command) for the `--host`, `--port` and `--transport` flags
