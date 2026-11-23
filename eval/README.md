# Benchmarks

Tests check that the gateway does what it specifies. Benchmarks measure whether that helps a model, which has no single right answer and is judged against a baseline. They live in their own package, `openapi-mcp-gateway-eval`, a uv workspace member beside the gateway, so the gateway's wheel and dependencies carry none of them. The code and its tests sit under `src/` and `tests/`, and each benchmark's data sits in a folder named after it under [`benchmarks/`](../benchmarks/) at the repository root.

| Benchmark | Measures | Calls a Model | Runs |
| --- | --- | --- | --- |
| [`search_ranking/`](../benchmarks/search_ranking/) | whether `search_operations` ranks the requested operation near the top | no | every PR, in CI |
| [`tool_calling/`](../benchmarks/tool_calling/) | whether a model calls the right operations with the right arguments through the gateway, compared with one tool per operation | yes | on demand; its data is checked on every PR |

The same tool calling harness also runs a user's own config on their own tasks, which [Your Own Config](#your-own-config) covers.

`omgw benchmark` runs the gateway's own benchmarks from the repository root. Each command reads its benchmark's folder unless `--benchmark-directory` names another, and `omgw --help` leaves `benchmark` out, since only a maintainer runs it.

## Your Own Config

`omgw eval` measures what your config gains a model on tasks you write for your API. It comes with the gateway's `eval` extra.

```bash
uvx --from 'openapi-mcp-gateway[eval]' omgw eval check --task-file tasks.yaml --config config.yml
uvx --from 'openapi-mcp-gateway[eval]' omgw eval run --task-file tasks.yaml --config config.yml --model anthropic:claude-haiku-5-5
```

`run` runs every task under your config and under one tool per operation, the baseline, and prints the tool calling benchmark's tables. No real API is called. A replay server answers each request from your tasks, else from the spec's example or a sample of its schema, and the grader reads the writes it received. A model needs its provider's API key in the usual variable, such as `ANTHROPIC_API_KEY`.

- **`--task-file`**: your tasks, which [Tasks](#tasks) lays out.
- **`--config`**: a gateway config, as you serve it. Name several to compare them, as `--config search=search.yml --config static=static.yml`. Every config must serve the same servers from the same specs.
- **`--against`**: what your configs are compared with. It is `baseline` by default, `dynamic` for the gateway's dynamic exposure, or `tool-search` for Pydantic AI's own tool search. Repeat it for several, or give `none` to run your configs alone.
- **`--model`**: a model as Pydantic AI names it. Repeat it for several.
- **`--repeat`**: runs per task, 1 by default. Before it starts, `run` prints how many runs it makes, since each one is a model's bill.

A run keeps only what a model sees from each config, its `exposure`, `policy`, `operations`, and `instructions`. The upstream, its credential, and the MCP sign-in are left out, so operation scopes, which hide tools from some users, are not measured.

`check` calls no model. It proves each task with a solution passes, and lists the tasks each config serves no operation for. `run` makes the same check first and runs no model when a solution fails.

### Tasks

The tasks file holds the tasks and what every run over them shares.

```yaml
system_prompt: You keep the shop's pet records.  # optional, the benchmark's own prompt by default
read_only_operations:                             # optional, by server, operations that only read though they POST
  pets: ['POST /pets/search']
tasks:
  - id: adopted
    prompt: Pet 7 was adopted, take it off the list.
    expected_calls:
      - {server: pets, method: DELETE, path: '/pets/{petId}', path_parameters: {petId: '7'}}
    solution:
      - {server: pets, operation: delete_pet, arguments: {petId: 7}}
    upstream_responses:
      - {server: pets, method: GET, path: /pets, body: [{id: 7, name: Rex}]}
```

- **`id` and `prompt`**: the task's name in the report, and what the user asks.
- **`expected_calls`**: the writes a run must make. Each names its `server`, `method`, and `path`, a template or a concrete path, and the `path_parameters`, `query`, and `body` values it must carry, leaving the rest free. `any_of` lists ways of which any one counts, and `all_of` a change split across requests. Reads are never graded.
- **`forbidden_calls`**: optional, requests that fail the task when made.
- **`upstream_responses`**: optional, what a read is answered with. A read none matches gets the spec's sample, and the report lists the failed runs that read one, which is where data is missing.
- **`solution`**: optional, calls known to pass, each an operation as the baseline names it with its arguments, which `check` and `run` replay before any model runs.

## Layout

```
eval/
  pyproject.toml                      the workspace member, depending on the gateway, Pydantic AI, and Pydantic Evals
  tests/                              the benchmarks' own tests
eval/src/openapi_mcp_gateway_eval/
  cli/
    __init__.py                       omgw eval for a user's own configs, and omgw benchmark, joining one group per benchmark
    common.py                         the arguments and error handling every command shares
    user_configs.py                   run and check for a user's own configs and tasks, which parse and print only
    search_ranking.py                 the search ranking commands, which parse and print only
    tool_calling.py                   the tool calling commands, which parse and print only
  spec_pins.py                        specs pinned by URL and SHA-256, downloaded once into a cache
  gateway_connection.py               a gateway built in-process from a server config, and an MCP client to it
  significance.py                     macro means and the paired bootstrap
  errors.py                           the error every benchmark's bad data raises
  provenance.py                       the record a model's run keeps beside the data it wrote
  search_ranking/
    dataset.py                        the manifest, queries, and judgements, loaded and validated
    metrics.py                        Metric and its kinds, nDCG, reciprocal rank, and recall
    scoring.py                        the metrics this benchmark reports
    variants.py                       Variant and its kinds: the gateway's search, Pydantic AI's, a random floor
    runner.py                         every query of every spec under every variant
    report.py                         summaries per variant and the paired comparisons
    markdown.py                       a report as Markdown tables
    gate_scores.py                    the gated scores CI compares against
    overlap.py                        how much of its answer's wording a query repeats
    annotations/
      merge.py                        two annotators' judgements merged into one set, and their agreement
      annotators.py                   Annotator and its roles, each a Pydantic AI agent with a context of its own
      pipeline.py                     every role in turn on each spec, with the specs side by side
  tool_calling/
    dataset.py                        the manifest, scenarios, tasks, and variants, loaded and validated
    config_generation.py              each scenario's generated variants, written by a model running the skill
    sampling.py                       a fixed example value for any response schema
    replay.py                         the stand-in upstream, answering every operation and recording each request
    grading.py                        whether a run's requests make the expected calls and none it forbids
    local_server.py                   an ASGI app served on a loopback port, for a model's framework to reach
    harness.py                        a scenario's gateway and replay on loopback ports, and one Pydantic AI run
    runner.py                         every task under every variant and model, as Pydantic Evals datasets
    report.py                         summaries per scenario, variant, and model, and comparisons with the baseline
    markdown.py                       a report as Markdown tables
    reach.py                          the tasks each variant serves every operation for, as intent coverage
    validation.py                     every task passing with a scripted model making its solution calls,
                                      and every hand-written variant reaching it
    user_configs.py                   a user's configs and tasks file as a one-scenario benchmark
```

Each benchmark's data sits in a folder named after it under `benchmarks/`, laid out the same way, which its README's Layout section lists. A unit of data, a spec or a scenario, keeps all of its files in one directory. Every instruction a model is given sits under `prompts/`, and every file a model wrote keeps the record of that run beside it as `<file>.provenance.json`.

Every record is a pydantic model, from the manifest to the report, so a wrong field fails where it is read.

Two abstractions carry the extension points. A `Metric` scores one ranking, and `scoring.METRICS` lists the ones reported. A search ranking `Variant` ranks a spec's operations, and the manifest picks each variant's class by its `type`, validating its configuration on load. A new metric or a new kind of variant, such as another framework's tool search, is a new subclass, and the runner, the report, and the CI gate stay as they are.

The gateway variant reaches the gateway the way a model does, through MCP, so renames, policy, and paging all count. Its `server` is a fragment of a server config, such as an `exposure` block, so comparing two configurations needs no code.

## Search Ranking

```bash
uv run omgw benchmark search-ranking run                     # every variant, test split
uv run omgw benchmark search-ranking run --split dev --variant default
uv run omgw benchmark search-ranking check                   # what CI runs, calling no model
```

`run` ranks every query of a split under every variant. `check` checks the data and compares the gated variant with its gate scores. `generate-annotations` makes the queries and judgements anew with models, which the [README](../benchmarks/search_ranking/README.md#data-provenance) walks through.

The first run downloads about 30 MB of pinned specs into `~/.cache/openapi-mcp-gateway`, or the directory `OPENAPI_MCP_GATEWAY_CACHE` names. A run takes a few seconds after that.

### Metrics

Each run reports four metrics, all from 0 to 1.

- **nDCG@10**: the headline, as in BEIR, since it uses the graded judgements.
- **Recall@20**: whether the answer is on the first page of `search_operations`, which holds 20.
- **MRR@10 and Recall@1**: count only the answer, grade `2`.

Every metric is averaged per spec and then over specs, so GitHub's size does not outweigh the others. A run compares the gated variant with every other by a paired bootstrap over test queries, stratified by spec, reporting a 95% interval.

### Results

Version 1, test split, openapi-mcp-gateway 0.8.0:

| Variant | nDCG@10 | Recall@20 | MRR@10 | Recall@1 |
| --- | --- | --- | --- | --- |
| `default` | 0.377 | 0.540 | 0.358 | 0.300 |
| `equal-weights` | 0.367 | 0.555 | 0.342 | 0.275 |
| `pydantic-ai-keywords` | 0.230 | 0.445 | 0.206 | 0.140 |
| `random` | 0.003 | 0.040 | 0.001 | 0.000 |

| Style | nDCG@10 | Recall@20 |
| --- | --- | --- |
| literal | 0.891 | 1.000 |
| paraphrase | 0.117 | 0.324 |
| indirect | 0.107 | 0.281 |

A literal request always finds its operation on the first page. A paraphrased or indirect one does so about three times in ten, so synonyms remain search's main weakness, which #166 takes up. The default weights beat equal weights by 0.010 nDCG@10, an interval of -0.008 to +0.027, which this set cannot tell apart from no difference. They beat Pydantic AI's keyword tool search, which counts the query words a tool's name and description share without stemming or weights, by 0.147 nDCG@10, an interval of +0.101 to +0.192.

### Changing Search

Tune on dev, then run test once. A change that moves the ranking on purpose updates the gate scores in the same PR, so the diff shows the new numbers:

```bash
uv run omgw benchmark search-ranking check --update
```

CI fails when nDCG@10 or Recall@20 of the `default` variant falls more than 0.01 below its gate scores, over all specs or for any one of them. With 50 test queries a spec, one query changing rank can move a spec's Recall@20 by 0.02, so the gate catches any lost answer.

## Tool Calling

```bash
uv run omgw benchmark tool-calling run --scenario github --model claude-haiku
uv run omgw benchmark tool-calling run --output report.json
uv run omgw benchmark tool-calling check                     # what CI runs, calling no model
```

`run` serves each scenario's gateway and a replay upstream on loopback ports, connects a Pydantic AI agent to the gateway over MCP, and runs every test task under every variant and model as many times as the manifest says, through Pydantic Evals. `check` proves every task can pass. `generate-config` writes the generated variants with a model running the skill, which the [README](../benchmarks/tool_calling/README.md#data-provenance) walks through. `run` and `generate-config` need each provider's API key in its usual variable, such as `ANTHROPIC_API_KEY`.

### Metrics

Each scenario, variant, and model reports six metrics.

- **Success Rate**: the share of runs that made the expected calls and none the task forbids, with its spread across repetitions.
- **Input and Output Tokens**: per task, as the provider reports them.
- **Tool Calls**: per task, every call the model made, searches and failures included.
- **Tool Errors**: per task, calls the gateway or the upstream refused, such as arguments that do not fit the schema.
- **Extra Writes**: per task, writes no expected call accounts for, which change more upstream than the user asked.

Each variant is compared with the baseline per model on success rate, input tokens, and extra writes, by a paired bootstrap over tasks stratified by scenario. A run that fails for reasons outside the model and the variant, such as a rate limit that outlasts its retries, is counted as errored and left out of the means.

### Results

No results are published yet. The first come with version 1's final generated configs, and are cited with that version.

### Cost

The baseline is what costs. One tool per operation sends every tool's definition with every request, about 340,000 tokens for GitHub and 590,000 for GitHub with Asana, where dynamic exposure sends three. The manifest caches the tool definitions on Anthropic models, which leaves the counted tokens as they are and cuts what a rerun of them costs.
