---
description: Choose which operations a server exposes, turn read-only GETs into MCP resources, and put a large API behind dynamic exposure and search.
---
# Exposure

A spec usually holds more operations than the model should see. This guide covers choosing which operations the gateway exposes, which become resources, and when to put a large API behind search. [Context Efficiency](../concepts/context-efficiency.md) compares these levers with tool shaping.

## Filtering Operations

`policy` keeps or drops operations by `operationId` or by method and path. This keeps GitHub's repository reads and drops its secrets:

```yaml
servers:
  - name: github
    spec: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
    policy:
      allow: ["GET /repos/*"]
      deny: ["GET /repos/*/actions/secrets*"]
```

To opt operations in from the spec instead, mark each with `x-mcp-integration: {tool: {}}` and set `policy.annotated_only: true`. [Configuration](../reference/configuration.md#policy) gives the matching rules and the order the filters apply in.

## Resource Exposure

A read-only `GET` fully identified by its URL suits an MCP resource, which the client or the user reads when they choose, better than a tool, which the model calls.

Set `exposure.promote_resources: true` and every eligible GET becomes a resource. Eligible means no required parameter outside the path. A required path parameter turns the operation into a resource template. Every PokéAPI operation is such a GET, so all 102 become resources with no spec edits. Each list becomes a resource, and each lookup by id a resource template. An operation with a required query parameter, or a method other than GET, stays a tool.

```yaml
servers:
  - name: pokeapi
    spec: https://pokeapi.co/openapi.yml
    exposure:
      promote_resources: true
```

> [!WARNING]
> Resource support is uneven across MCP clients and agent frameworks. One that ignores resources never reaches a promoted operation. Leave `promote_resources` off when you target such a client.

### Per-Operation Control

Each resource can take its own name, URI template, or MIME type, in either form [Config and Spec Forms](tool-shaping.md#config-and-spec-forms) describes:

=== "In config"

    ```yaml
    servers:
      - name: pokeapi
        spec: https://pokeapi.co/openapi.yml
        exposure:
          promote_resources: true
        operations:
          pokemon_retrieve:
            resource:
              name: pokemon
              mime_type: application/json
          pokemon_list:
            tool:
              name: list_pokemon
            resource:
              name: pokemon_index
    ```

=== "In the spec"

    ```yaml
    paths:
      /api/v2/pokemon/{id}/:
        get:
          operationId: pokemon_retrieve
          x-mcp-integration:
            resource:
              name: pokemon
              mime_type: application/json
              # Optional, and must start with "{name}://".
              # uri_template: pokeapi://pokemon/{id}
    ```

Declaring both `tool` and `resource` registers the operation on both surfaces, as `pokemon_list` does above. [`examples/pokeapi-override.yml`](https://github.com/mroops0111/openapi-mcp-gateway/blob/master/examples/pokeapi-override.yml) is a runnable demo.

The gateway checks these declarations at startup.

- **Config Entries**: an unknown `operationId`, a non-`GET` method, a required non-path parameter, or a `uri_template` outside `{name}://` fails startup. So does a `resource` entry without `promote_resources`.
- **Spec Declarations**: a spec-side `resource` without `promote_resources` only logs a warning, and the operation stays a tool.
- **Subscriptions**: not implemented, since REST has no native push.

## Dynamic Exposure

`exposure.style: dynamic` puts a server's operations behind three meta-tools, which search, describe and call operations. It is set per server, so one process can serve GitHub dynamically beside a static PokéAPI. [Dynamic Exposure](../concepts/dynamic-exposure.md) explains the meta-tools and the ranking.

```yaml
servers:
  - name: github
    spec: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
    exposure:
      style: dynamic
```

[`examples/github-dynamic.yml`](https://github.com/mroops0111/openapi-mcp-gateway/blob/master/examples/github-dynamic.yml) is a runnable config. `exposure.promote_resources` is refused under `dynamic`, where no operation becomes a resource. To have the model search before it calls, say so in the server's `instructions`, as [Basics](../reference/configuration.md#basics) describes.

### Tuning Search Weights

`exposure.search.field_weights` overrides the default weights per server, field by field. `0` leaves a field out of search:

```yaml
servers:
  - name: internal
    spec: ./internal-api.yml
    exposure:
      style: dynamic
      search:
        field_weights:
          # This spec's summaries are terse,
          # and its descriptions say what each call does.
          description: 2
```

Raise a field that describes operations well in your spec, and lower one that repeats boilerplate. To fix one operation that ranks poorly, rewrite its `description` through `operations`, since search reads the overridden text.

### Searching by Meaning

`exposure.search.semantic` ranks by meaning rather than by shared words, through an embedding model. Install the `embeddings` extra, and set the provider's key in its usual variable, such as `OPENAI_API_KEY`:

```yaml
servers:
  - name: github
    spec: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
    exposure:
      style: dynamic
      search:
        semantic:
          # Or a local model, such as ollama:bge-m3.
          model: openai:text-embedding-3-small
          cache_directory: ~/.cache/openapi-mcp-gateway-embeddings
```

The model name, its key and the extra are checked at startup, and a problem fails startup. Keep the default `retrieval: semantic` unless users phrase requests in the spec's own words, and then set `retrieval: hybrid`, so exact word matches still count. [Semantic Search](../concepts/dynamic-exposure.md#semantic-search) explains when operations are embedded, how `retrieval: hybrid` merges rankings, and when search falls back to words.

The container image leaves the `embeddings` extra out, as [Deployment](deployment.md#container-image) notes.

## Related

The concept page explains the meta-tools behind dynamic exposure, and the reference lists the per-operation `resource` keys.

- **Concepts**: [Dynamic Exposure](../concepts/dynamic-exposure.md) for the meta-tools and how search ranks operations and [Search Ranking](../evaluation.md#search-ranking) for how word search scores on literal and paraphrased requests
- **Reference**: [Operation Fields](../reference/configuration.md#operation-fields) for the `resource` keys an override takes
