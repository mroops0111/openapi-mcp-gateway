# Benchmarks

Tests check that the gateway does what it specifies. Benchmarks measure whether that helps a model, which has no single right answer and is judged against a baseline. They live in their own package, `openapi-mcp-gateway-benchmarks`, a uv workspace member beside the gateway, so the gateway's wheel and dependencies carry none of them. The code and its tests sit under `src/` and `tests/`, and each benchmark's data sits in a folder named after it.

| Benchmark | Measures | Calls a Model | Runs |
| --- | --- | --- | --- |
| [`search_ranking/`](search_ranking/) | whether `search_operations` ranks the requested operation near the top | no | every PR, in CI |
| [`tool_calling/`](tool_calling/) | whether a model calls the right operations with the right arguments through the gateway, compared with one tool per operation | yes | on demand; its data is checked on every PR |

## Layout

```
benchmarks/
  pyproject.toml                      the workspace member, depending on the gateway, Pydantic AI, and Pydantic Evals
  tests/                              the benchmarks' own tests
benchmarks/src/openapi_mcp_gateway_benchmarks/
  cli.py                              the commands, one group per benchmark, which parse and print only
  spec_pins.py                        specs pinned by URL and SHA-256, downloaded once into a cache
  gateway_connection.py               a gateway built in-process from a server config, and an MCP client to it
  significance.py                     macro means and the paired bootstrap
  local_server.py                     an ASGI app served on a loopback port, for a model's framework to reach
  errors.py                           the error every benchmark's bad data raises
  provenance.py                       the record a model's run keeps beside the data it wrote
  search_ranking/
    dataset.py                        the manifest, queries, and judgements, loaded and validated
    annotations.py                    two annotators' judgements merged into one set, and their agreement
    annotation_workflow.py            the stages that prepare each annotator's files and collect what they write
    automated_annotation.py           the first stages with both annotators run through Pydantic AI
    metrics.py                        Metric and its kinds, nDCG, reciprocal rank, and recall
    scoring.py                        the metrics this benchmark reports
    variants.py                       Variant and its kinds: the gateway's search, Pydantic AI's, a random floor
    runner.py                         every query of every spec under every variant
    report.py                         summaries per variant and the paired comparisons
    markdown.py                       a report as Markdown tables
    gate_scores.py                    the gated scores CI compares against
    overlap.py                        how much of its answer's wording a query repeats
  tool_calling/
    dataset.py                        the manifest, scenarios, tasks, and variants, loaded and validated
    config_generation.py              each scenario's generated variants, written by a model running the skill
    sampling.py                       a fixed example value for any response schema
    replay.py                         the stand-in upstream, answering every operation and recording each request
    grading.py                        whether a run's requests make the expected calls and none it forbids
    harness.py                        a scenario's gateway and replay on loopback ports, and one Pydantic AI run
    solution.py                       a scripted model that makes a task's solution calls
    runner.py                         every task under every variant and model, as Pydantic Evals datasets
    report.py                         summaries per scenario, variant, and model, and comparisons with the baseline
    markdown.py                       a report as Markdown tables
    reach.py                          the tasks each variant serves every operation for, as intent coverage
    validation.py                     every task passing with its solution, and every hand-written variant reaching it
```

Each benchmark's data sits in a folder named after it, laid out the same way, which its README's Layout section lists. A unit of data, a spec or a scenario, keeps all of its files in one directory. Every instruction a model is given sits under `prompts/`, and every file a model wrote keeps the record of that run beside it as `<file>.provenance.json`.

Every record is a pydantic model, from the manifest to the report, so a wrong field fails where it is read.

Two abstractions carry the extension points. A `Metric` scores one ranking, and `scoring.METRICS` lists the ones reported. A search ranking `Variant` ranks a spec's operations, and the manifest picks each variant's class by its `type`, validating its configuration on load. A new metric or a new kind of variant, such as another framework's tool search, is a new subclass, and the runner, the report, and the CI gate stay as they are.

The gateway variant reaches the gateway the way a model does, through MCP, so renames, policy, and paging all count. Its `server` is a fragment of a server config, such as an `exposure` block, so comparing two configurations needs no code.

## Search Ranking

```bash
uv run openapi-mcp-gateway-benchmarks search-ranking run benchmarks/search_ranking        # every variant, test split
uv run openapi-mcp-gateway-benchmarks search-ranking run benchmarks/search_ranking --split dev --variant default
uv run openapi-mcp-gateway-benchmarks search-ranking check benchmarks/search_ranking      # what CI runs, calling no model
```

`run` ranks every query of a split under every variant. `check` checks the data and compares the gated variant with its gate scores. `generate` annotates the data again with models, and `search-ranking annotations` holds the stages around it, which the [README](search_ranking/README.md#data-provenance) walks through.

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
uv run openapi-mcp-gateway-benchmarks search-ranking check benchmarks/search_ranking --update
```

CI fails when nDCG@10 or Recall@20 of the `default` variant falls more than 0.01 below its gate scores, over all specs or for any one of them. With 50 test queries a spec, one query changing rank can move a spec's Recall@20 by 0.02, so the gate catches any lost answer.

## Tool Calling

```bash
uv run openapi-mcp-gateway-benchmarks tool-calling run benchmarks/tool_calling --scenario github --model claude-haiku
uv run openapi-mcp-gateway-benchmarks tool-calling run benchmarks/tool_calling --output report.json
uv run openapi-mcp-gateway-benchmarks tool-calling check benchmarks/tool_calling         # what CI runs, calling no model
```

`run` serves each scenario's gateway and a replay upstream on loopback ports, connects a Pydantic AI agent to the gateway over MCP, and runs every test task under every variant and model as many times as the manifest says, through Pydantic Evals. `check` proves every task can pass. `generate` writes the generated variants with a model running the skill, which the [README](tool_calling/README.md#data-provenance) walks through. `run` and `generate` need each provider's API key in its usual variable, such as `ANTHROPIC_API_KEY`.

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
