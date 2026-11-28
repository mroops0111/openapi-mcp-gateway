---
description: Serve the public PokéAPI over MCP, connect a client and call a tool, then move to a config file with two APIs and a desktop client.
---
# Getting Started

In this tutorial you serve the public PokéAPI over MCP, connect a client, and call a tool. You then move to a config file with two APIs, and finish with a desktop client over stdio.

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

## Serving PokéAPI

Point the gateway at the PokéAPI spec and name the server `pokeapi`:

```bash
uvx openapi-mcp-gateway serve \
    --spec https://pokeapi.co/openapi.yml --name pokeapi
```

The MCP endpoint is now `http://127.0.0.1:8000/pokeapi/mcp`. In a second terminal, confirm that the server is running:

```bash
curl http://127.0.0.1:8000/healthz
```

```json
{
  "status": "ok",
  "servers": [
    {
      "name": "pokeapi",
      "path": "/pokeapi",
      "title": "PokéAPI",
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
    claude mcp add --transport http pokeapi http://127.0.0.1:8000/pokeapi/mcp
    ```

=== "Cursor"

    Add the server to `~/.cursor/mcp.json`:

    ```json
    {
      "mcpServers": {
        "pokeapi": {"url": "http://127.0.0.1:8000/pokeapi/mcp"}
      }
    }
    ```

=== "VS Code"

    Add the server to `.vscode/mcp.json` in your workspace:

    ```json
    {
      "servers": {
        "pokeapi": {
          "type": "http",
          "url": "http://127.0.0.1:8000/pokeapi/mcp"
        }
      }
    }
    ```

=== "MCP Inspector"

    The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is a debugging client that needs Node.js. Its `--cli` mode lists the tools from a terminal:

    ```bash
    npx @modelcontextprotocol/inspector --cli \
        http://127.0.0.1:8000/pokeapi/mcp --method tools/list
    ```

    Without `--cli`, it opens a web UI where you enter the same URL.

The server lists 102 tools, one per PokéAPI operation, such as `pokemon_list` and `pokemon_retrieve`. Each name comes from the operation's `operationId`, and each title from its summary.

## Calling a Tool

Call `pokemon_retrieve` for Pikachu. In your client, ask "What type is Pikachu?". With the Inspector:

```bash
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:8000/pokeapi/mcp \
    --method tools/call --tool-name pokemon_retrieve \
    --tool-arg id=pikachu
```

The gateway sends `GET /api/v2/pokemon/pikachu/` upstream and returns the body as text:

```json
{
  "content": [
    {"type": "text", "text": "{\"id\": 25, \"name\": \"pikachu\", ..."}
  ],
  "isError": false
}
```

The body is a JSON object, so it also arrives as `structuredContent`, and a client reads it without parsing the text. An upstream error comes back as a result with `isError: true` and the upstream's status and body.

Stop the server with Ctrl+C before the next step.

## Multiple APIs at Once

A config file serves several APIs from one process, each at its own mount path. Save this as `config.yml`:

```yaml
servers:
  # Eligible GETs become MCP resources, and the rest stay tools.
  - name: pokeapi
    spec: https://pokeapi.co/openapi.yml
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
✓ Valid   2 server(s), 3 tool(s), 102 resource(s)
```

Every PokéAPI operation is now a resource, since each is a GET a client can read by its URL, and GitHub has three meta-tools, `search_operations`, `get_operation` and `call_operation`, which search, describe and call its operations. Then start the server:

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
    "pokeapi": {
      "command": "uvx",
      "args": [
        "openapi-mcp-gateway", "serve",
        "--spec", "https://pokeapi.co/openapi.yml",
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
