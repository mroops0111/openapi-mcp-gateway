---
description: Serve the public Petstore API over MCP, connect a client and call a tool, then move to a config file with two APIs and a desktop client.
---
# Getting Started

In this tutorial you serve the public Petstore API over MCP, connect a client, and call a tool. You then move to a config file with two APIs, and finish with a desktop client over stdio.

## Installation

`uvx` runs the published package without a project or a virtual environment. It comes with uv, so [install uv](https://docs.astral.sh/uv/getting-started/installation/) first if you do not have it. To confirm that it runs:

```bash
uvx openapi-mcp-gateway --help
```

The command is `openapi-mcp-gateway`, and needs Python 3.11 or later. [CLI](reference/cli.md) gives its shorter alias and every flag. Features with heavier dependencies come as extras, which `uvx --from` adds:

| Extra | Adds |
|---|---|
| `redis` | A Redis token store, shared across replicas |
| `oidc` | Checks that tokens from your identity provider are genuine, needed whenever `auth.mcp.issuer` is set |
| `embeddings` | Search by meaning under dynamic exposure |
| `eval` | The `eval` command, as [Measuring a Config](guides/authoring-with-ai.md#measuring-a-config) describes |

```bash
uvx --from 'openapi-mcp-gateway[redis,oidc]' openapi-mcp-gateway --help
```

To add the gateway to a project instead, run `uv add openapi-mcp-gateway`. [Deployment](guides/deployment.md#container-image) covers the container image.

## Serving Petstore

Point the gateway at the Petstore spec and name the server `petstore`:

```bash
uvx openapi-mcp-gateway serve \
    --spec https://petstore3.swagger.io/api/v3/openapi.json --name petstore
```

The MCP endpoint is now `http://127.0.0.1:8000/petstore/mcp`. In a second terminal, confirm that the server is running:

```bash
curl http://127.0.0.1:8000/healthz
```

```json
{
  "status": "ok",
  "servers": [
    {
      "name": "petstore",
      "path": "/petstore",
      "title": "Swagger Petstore - OpenAPI 3.0",
      "auth": {"mcp": "open", "upstream": "none"}
    }
  ]
}
```

`"mcp": "open"` means anyone who can reach the port may call the endpoint without a token. This is acceptable for local use. [Authorization](guides/authorization.md) shows how to require one.

## Connecting a Client

Any MCP client that speaks Streamable HTTP, the MCP transport over ordinary HTTP requests, can connect to the endpoint. Each client is set up as follows:

=== "Claude Code"

    ```bash
    claude mcp add --transport http petstore http://127.0.0.1:8000/petstore/mcp
    ```

=== "Cursor"

    Add the server to `~/.cursor/mcp.json`:

    ```json
    {
      "mcpServers": {
        "petstore": {"url": "http://127.0.0.1:8000/petstore/mcp"}
      }
    }
    ```

=== "VS Code"

    Add the server to `.vscode/mcp.json` in your workspace:

    ```json
    {
      "servers": {
        "petstore": {
          "type": "http",
          "url": "http://127.0.0.1:8000/petstore/mcp"
        }
      }
    }
    ```

=== "MCP Inspector"

    The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is a debugging client that needs Node.js. Its `--cli` mode lists the tools from a terminal:

    ```bash
    npx @modelcontextprotocol/inspector --cli \
        http://127.0.0.1:8000/petstore/mcp --method tools/list
    ```

    Without `--cli`, it opens a web UI where you enter the same URL.

The server lists 19 tools, one per Petstore operation, such as `find_pets_by_status` and `get_pet_by_id`. Each name comes from the operation's `operationId`, and each title from its summary.

## Calling a Tool

Call `find_pets_by_status` for available pets. In your client, ask "Which pets are available in the petstore?". With the Inspector:

```bash
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:8000/petstore/mcp \
    --method tools/call --tool-name find_pets_by_status \
    --tool-arg status=available
```

The gateway sends `GET /pet/findByStatus?status=available` upstream and returns the body as text:

```json
{
  "content": [
    {"type": "text", "text": "[{\"id\": 4, \"name\": \"Dog 1\", ..."}
  ],
  "isError": false
}
```

A body that is a JSON object also arrives as `structuredContent`, so a client reads it without parsing the text.

> [!NOTE]
> Petstore is a shared public demo, so other people change its data and some calls return a server error at times. An upstream error comes back as a result with `isError: true` and the upstream's status and body.

Stop the server with Ctrl+C before the next step.

## Multiple APIs at Once

A config file serves several APIs from one process, each at its own mount path. Save this as `config.yml`:

```yaml
servers:
  # Eligible GETs become MCP resources, and the rest stay tools.
  - name: petstore
    spec: https://petstore3.swagger.io/api/v3/openapi.json
    exposure:
      promote_resources: true

  # GitHub's 1,000+ operations sit behind three meta-tools.
  - name: github
    spec: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
    exposure:
      style: dynamic
```

Check it before serving. `--dry-run` loads both specs and prints what each server exposes:

```bash
uvx openapi-mcp-gateway serve --config config.yml --dry-run
```

```text
✓ Valid   2 server(s), 16 tool(s), 6 resource(s)
```

Petstore now has 13 tools and 6 resources, and GitHub has three meta-tools, `search_operations`, `get_operation` and `call_operation`, which search, describe and call its operations. Then start the server:

```bash
uvx openapi-mcp-gateway serve --config config.yml
```

Connect your client to `http://127.0.0.1:8000/github/mcp` and ask "What are the latest open issues in modelcontextprotocol/python-sdk?". The model finds the operation with `search_operations`, reads its parameters with `get_operation`, and runs it with `call_operation`. It never loads GitHub's 1,000+ tool schemas. With the Inspector, the first and last steps are:

```bash
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:8000/github/mcp \
    --method tools/call --tool-name search_operations \
    --tool-arg "query=list open issues in a repository"
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:8000/github/mcp \
    --method tools/call --tool-name call_operation \
    --tool-arg name=issues_list_for_repo \
    --tool-arg 'arguments={"owner": "modelcontextprotocol",
                           "repo": "python-sdk", "per_page": 1}'
```

The search ranks `issues_list_for_repo` first. Without a token, GitHub answers only public reads at a low rate limit. [Authorization](guides/authorization.md#shared-credentials) shows how to add a token.

## Desktop Client Over Stdio

A desktop client such as Claude Desktop or Cursor can launch the gateway itself and talk over stdin and stdout. Add it to the client's MCP config:

```json
{
  "mcpServers": {
    "petstore": {
      "command": "uvx",
      "args": [
        "openapi-mcp-gateway", "serve",
        "--spec", "https://petstore3.swagger.io/api/v3/openapi.json",
        "--transport", "stdio"
      ]
    }
  }
}
```

A stdio process serves one server and checks no MCP token, as [Stdio](guides/deployment.md#stdio) explains.

## Next Steps

[`examples/`](https://github.com/mroops0111/openapi-mcp-gateway/tree/master/examples) holds runnable configs, each with its prerequisites at the top. Each guide covers one goal.

- **A Token or a Sign-In for the API**: [Authorization](guides/authorization.md), including [token exchange](guides/authorization.md#token-exchange)
- **A Config Written for You**: [Authoring With AI](guides/authoring-with-ai.md)
- **Fewer or Different Tools**: [Exposure](guides/exposure.md) and [Tool Shaping](guides/tool-shaping.md)
- **A Python or FastAPI App**: [Python and FastAPI](guides/python.md)
- **A Server for Others**: [Deployment](guides/deployment.md)
