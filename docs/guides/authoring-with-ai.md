---
description: Write a config with the generate-config Claude Code skill, inspect it with a dry run, and measure it on your own tasks with the eval command.
---
# Authoring With AI

A companion Claude Code skill writes a config from a plain-language request. Two commands then check what the config exposes and what it does for a model.

## Generating a Config

`generate-config` is a Claude Code skill that writes a `config.yml` for an API you name. It finds the OpenAPI spec, picks the operations and auth, adds shaping where a raw operation makes a poor tool, and checks that the config boots. This repository is also its plugin marketplace. Install the plugin from inside Claude Code:

```text
/plugin marketplace add mroops0111/openapi-mcp-gateway
/plugin install openapi-mcp-gateway@openapi-mcp-gateway
```

Then invoke the skill by its namespaced name, with your request:

```text
/openapi-mcp-gateway:generate-config connect GitHub to manage issues
```

Claude Code also invokes the skill without the command when you ask for an API to be connected as MCP.

## Inspecting a Config

`--dry-run` validates a config and prints what it would expose, without serving:

```bash
uvx openapi-mcp-gateway serve --config config.yml --dry-run
```

```text
✓ Valid   1 server(s), 13 tool(s), 6 resource(s)
    transport      streamable-http

  petstore
    mount          /petstore
    ...
```

Read the tool names and the `SHAPING` column to confirm the generated config exposes what you asked for. [Dry Run](../reference/cli.md#dry-run) describes the full output and its JSON form.

## Measuring a Config

The `eval` command measures what your config does for a model, on tasks you write for your API. It needs the `eval` extra, and no real API is called. Start with `check`, which calls no model:

```bash
uvx --from 'openapi-mcp-gateway[eval]' openapi-mcp-gateway eval check \
    --task-file tasks.yaml --config config.yml
```

`check` proves each task's solution passes, and lists the tasks your config cannot reach. Then `run` runs the tasks with a model:

```bash
uvx --from 'openapi-mcp-gateway[eval]' openapi-mcp-gateway eval run \
    --task-file tasks.yaml --config config.yml \
    --model anthropic:claude-haiku-5-5
```

`run` runs `check` first, and calls no model if a solution fails. It then prints how many runs it will make, since each one is billed. It compares your config with one tool per operation, the `baseline`, and reports success, tokens, tool calls, extra writes and cost. Two flags widen the comparison.

- **Several Configs**: name each, as `--config shaped=shaped.yml --config plain=plain.yml`. Every config must serve the same servers from the same specs.
- **Other Comparisons**: `--against dynamic` adds dynamic exposure, `--against tool-search` adds Pydantic AI's tool search, and `--against none` runs your configs alone. Repeat the flag for several.

The model needs its provider's API key in the usual variable, such as `ANTHROPIC_API_KEY`. A run keeps only what a model sees from each config, its `exposure`, `policy`, `operations` and `instructions`. The upstream credential and the MCP sign-in are left out, so operation scopes are not measured. [Evaluation](../evaluation.md) shows results on public APIs.

### Tasks File

The tasks file holds the tasks and the settings every run over them shares.

```yaml
# Optional, the benchmark's own prompt by default.
system_prompt: You keep the shop's pet records.
# Optional, by server, operations that only read though they POST.
read_only_operations:
  pets: ['POST /pets/search']
tasks:
  - id: adopted
    prompt: Pet 7 was adopted, take it off the list.
    expected_calls:
      - server: pets
        method: DELETE
        path: '/pets/{petId}'
        path_parameters: {petId: '7'}
    solution:
      - {server: pets, operation: delete_pet, arguments: {petId: 7}}
    upstream_responses:
      - {server: pets, method: GET, path: /pets, body: [{id: 7, name: Rex}]}
```

- **`id` and `prompt`**: the task's name in the report, and what the user asks.
- **`expected_calls`**: the writes a run must make. Each names its `server`, `method` and `path`, a template or a concrete path, and the `path_parameters`, `query` and `body` values it must carry, leaving the rest free. `any_of` lists alternatives of which any one counts, and `all_of` a change split across requests. Reads are never graded.
- **`forbidden_calls`**: optional, requests that fail the task when made.
- **`upstream_responses`**: optional, what a read is answered with. A read none matches gets the spec's example or a sample of its schema, and the report lists the failed runs that read one, which shows where data is missing.
- **`solution`**: optional, calls known to pass, each an operation as the baseline names it with its arguments. `check` and `run` replay them before any model runs.

## Related

The skill writes fields the exposure and shaping guides explain, and the evaluation page reports how its configs perform.

- **Guides**: [Exposure](exposure.md) for the `policy` and `exposure` fields the skill sets and [Tool Shaping](tool-shaping.md) for the `tool` blocks it adds
- **Concepts**: [Evaluation](../evaluation.md) for how generated configs perform on public APIs
- **Reference**: [`eval` Command](../reference/cli.md#eval-command) for every flag of `eval check` and `eval run`
