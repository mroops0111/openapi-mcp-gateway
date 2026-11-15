# Benchmarks

Tests check that the gateway does what it specifies. Benchmarks measure whether that helps a model, which has no single right answer and is judged against a baseline. The code lives in the package, under `openapi_mcp_gateway.benchmarks`, and the data lives here, so the same commands run this repository's benchmarks and, later, a user's own.

| Benchmark | Measures | Calls a Model | Runs |
| --- | --- | --- | --- |
| [`search/`](search/) | whether `search_operations` ranks the requested operation near the top | no | every PR, in CI |
| [`agent/`](agent/) | whether a model finishes real tasks through the gateway, compared with one tool per operation | yes | on demand; its data is validated on every PR |

## Layout

```
src/openapi_mcp_gateway/benchmarks/   code, shipped in the package
  cli.py                              the commands, one group per benchmark, which parse and print only
  spec_pins.py                        specs pinned by URL and SHA-256, downloaded once into a cache
  gateway_connection.py               a gateway built in-process from a server config, and an MCP client to it
  significance.py                     macro means and the paired bootstrap
  local_server.py                     an ASGI app served on a loopback port, for a model's framework to reach
  errors.py                           the error every benchmark's bad data raises
  search/
    dataset.py                        the manifest, queries, and judgements, loaded and validated
    annotations.py                    two annotators' judgements merged into one set, and their agreement
    annotation_workflow.py            the stages that prepare each annotator's files and collect what they write
    automated_annotation.py           the same stages with both annotators run through Pydantic AI
    metrics.py                        Metric and its kinds, nDCG, reciprocal rank, and recall
    scoring.py                        the metrics this benchmark reports
    systems.py                        System and its kinds: the gateway's search, Pydantic AI's, a random floor
    runner.py                         every query of every spec under every system
    report.py                         summaries per system and the paired comparisons
    markdown.py                       a report as Markdown tables
    baseline.py                       the gated numbers CI compares against
    overlap.py                        how much of its answer's wording a query repeats
  agent/
    dataset.py                        the manifest, scenarios, tasks, and variants, loaded and validated
    sampling.py                       a fixed example value for any response schema
    replay.py                         the stand-in upstream, answering every operation and recording each request
    grading.py                        whether a run's requests make the expected calls and write nothing else
    harness.py                        a scenario's gateway and replay on loopback ports, and one Pydantic AI run
    solution.py                       a scripted model that makes a task's solution calls
    runner.py                         every task under every variant and model, as Pydantic Evals datasets
    report.py                         summaries per scenario, variant, and model, and comparisons with the baseline
    markdown.py                       a report as Markdown tables
    validation.py                     every task passing with its solution, and every variant reaching it

benchmarks/                           data, not shipped
  search/
    benchmark.yaml                    spec pins, systems, and the CI gate
    README.md                         what the data holds, how it was made, and its known biases
    baseline.json                     the gated scores CI compares against
    annotations/<spec>.jsonl          each query's two annotations and any adjudication
    annotations/README.md             how the data is annotated again, stage by stage
    annotations/*.txt                 the instructions each annotator is given, as templates
    <spec>/queries.jsonl              queries, in BEIR's format
    <spec>/qrels/{dev,test}.tsv       graded judgements, in BEIR's format, which names them qrels and its splits dev and test
  agent/
    benchmark.yaml                    spec pins, models, repetitions, and the system prompt
    README.md                         what the data holds, how it was made, and its known biases
    scenarios/<name>/scenario.yaml    the intent and the servers
    scenarios/<name>/tasks.yaml       tasks with expected calls, a solution, and responses
    scenarios/<name>/variants/*.yaml  one gateway config fragment per server, per variant
```

Every record is a pydantic model, from the manifest to the report, so a wrong field fails where it is read.

Two abstractions carry the extension points. A `Metric` scores one ranking, and `scoring.METRICS` lists the ones reported. A `System` ranks a spec's operations, and the manifest picks each system's class by its `type`, validating its configuration on load. A new metric or a new kind of system, such as another framework's tool search, is a new subclass, and the runner, the report, and the CI gate stay as they are.

The gateway system reaches the gateway the way a model does, through MCP, so renames, policy, and paging all count. Its `server` is a fragment of a server config, such as an `exposure` block, so comparing two configurations needs no code.

## Search

```bash
uv run python -m openapi_mcp_gateway.benchmarks search run benchmarks/search        # every system, test split
uv run python -m openapi_mcp_gateway.benchmarks search run benchmarks/search --split dev --system default
uv run python -m openapi_mcp_gateway.benchmarks search check benchmarks/search      # what CI runs
```

`check` first checks the data, that its files agree and that no paraphrase or indirect query repeats its answer's words, and then compares the gated system with the baseline. `search annotations` holds the four stages that make the data anew, which [`search/annotations/README.md`](search/annotations/README.md) walks through.

The first run downloads about 30 MB of pinned specs into `~/.cache/openapi-mcp-gateway`, or the directory `OPENAPI_MCP_GATEWAY_CACHE` names. A run takes a few seconds after that.

### Metrics

Each run reports four metrics, all from 0 to 1.

- **nDCG@10** is the headline, as in BEIR, since it uses the graded judgements.
- **Recall@20** says whether the answer is on the first page of `search_operations`, which holds 20.
- **MRR@10** and **Recall@1** count only the answer, grade `2`.

Every metric is averaged per spec and then over specs, so GitHub's size does not outweigh the others. A run compares the gated system with every other by a paired bootstrap over test queries, stratified by spec, reporting a 95% interval.

### Changing Search

Tune on dev, then run test once. A change that moves the ranking on purpose updates the baseline in the same PR, so the diff shows the new numbers:

```bash
uv run python -m openapi_mcp_gateway.benchmarks search check benchmarks/search --update
```

CI fails when nDCG@10 or Recall@20 of the `default` system falls more than 0.01 below the baseline, over all specs or for any one of them. With 50 test queries a spec, one query changing rank can move a spec's Recall@20 by 0.02, so the gate catches any lost answer.

### Results

Version 1, test split, openapi-mcp-gateway 0.8.0:

| System | nDCG@10 | Recall@20 | MRR@10 | Recall@1 |
| --- | --- | --- | --- | --- |
| default | 0.377 | 0.540 | 0.358 | 0.300 |
| equal weights | 0.367 | 0.555 | 0.342 | 0.275 |
| Pydantic AI keyword search | 0.230 | 0.445 | 0.206 | 0.140 |
| random | 0.003 | 0.040 | 0.001 | 0.000 |

| Style | nDCG@10 | Recall@20 |
| --- | --- | --- |
| literal | 0.891 | 1.000 |
| paraphrase | 0.117 | 0.324 |
| indirect | 0.107 | 0.281 |

A literal request always finds its operation on the first page. A paraphrased or indirect one does so about three times in ten, so synonyms remain search's main weakness, which #166 takes up. The default weights beat equal weights by 0.010 nDCG@10, an interval of -0.008 to +0.027, which this set cannot tell apart from no difference. They beat Pydantic AI's keyword tool search, which counts the query words a tool's name and description share without stemming or weights, by 0.147 nDCG@10, an interval of +0.101 to +0.192.

## Agent

```bash
uv run python -m openapi_mcp_gateway.benchmarks agent validate benchmarks/agent   # what CI runs, calling no model
uv run python -m openapi_mcp_gateway.benchmarks agent run benchmarks/agent --scenario github --model claude-haiku
uv run python -m openapi_mcp_gateway.benchmarks agent run benchmarks/agent --output report.json
```

`run` needs the `benchmarks` extra and each provider's API key in its usual variable, such as `ANTHROPIC_API_KEY`. It serves each scenario's gateway and a replay upstream on loopback ports, connects a Pydantic AI agent to the gateway over MCP, and runs every final task under every variant and model as many times as the manifest says, through Pydantic Evals.

### Measures

Each scenario, variant, and model reports five measures.

- **Success Rate**: the share of runs that made the expected calls and wrote nothing else, overall and split into tasks inside and outside the intent, with its spread across repetitions.
- **Input and output tokens per task**, as the provider reports them.
- **Tool calls per task**, every call the model made, searches and failures included.
- **Tool errors per task**, calls the gateway or the upstream refused, such as arguments that do not fit the schema.

Each variant is compared with the baseline per model on success rate and input tokens, by a paired bootstrap over tasks stratified by scenario. A run that fails for reasons outside the model and the variant, such as a rate limit that outlasts its retries, is counted as errored and left out of the means.

### Cost

The baseline is what costs. One tool per operation sends every tool's definition with every request, about 340,000 tokens for GitHub and 590,000 for GitHub with Asana, where dynamic exposure sends three. The manifest caches the tool definitions on Anthropic models, which leaves the counted tokens as they are and cuts what a rerun of them costs.
