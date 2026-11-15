# Benchmarks

Tests check that the gateway does what it specifies. Benchmarks measure whether that helps a model, which has no single right answer and is judged against a baseline. The code lives in the package, under `openapi_mcp_gateway.benchmarks`, and the data lives here, so the same commands run this repository's benchmarks and, later, a user's own.

| Benchmark | Measures | Calls a Model | Runs |
| --- | --- | --- | --- |
| [`search/`](search/) | whether `search_operations` ranks the requested operation near the top | no | every PR, in CI |

## Layout

```
src/openapi_mcp_gateway/benchmarks/   code, shipped in the package
  cli.py                              the commands, one group per benchmark, which parse and print only
  spec_pins.py                        specs pinned by URL and SHA-256, downloaded once into a cache
  gateway_connection.py               a gateway built in-process from a server config, and an MCP client to it
  significance.py                     macro means and the paired bootstrap
  search/
    dataset.py                        the manifest, queries, and judgements, loaded and validated
    annotations.py                    two annotators' judgements merged into one set, and their agreement
    annotation_workflow.py            the stages that prepare each annotator's files and collect what they write
    metrics.py                        Metric and its kinds, nDCG, reciprocal rank, and recall
    scoring.py                        the metrics this benchmark reports
    systems.py                        System and its kinds, the gateway's search and a random floor
    runner.py                         every query of every spec under every system
    report.py                         summaries per system and the paired comparisons
    markdown.py                       a report as Markdown tables
    baseline.py                       the gated numbers CI compares against
    overlap.py                        how much of its answer's wording a query repeats

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
| random | 0.003 | 0.040 | 0.001 | 0.000 |

| Style | nDCG@10 | Recall@20 |
| --- | --- | --- |
| literal | 0.891 | 1.000 |
| paraphrase | 0.117 | 0.324 |
| indirect | 0.107 | 0.281 |

A literal request always finds its operation on the first page. A paraphrased or indirect one does so about three times in ten, so synonyms remain search's main weakness, which #166 takes up. The default weights beat equal weights by 0.010 nDCG@10, an interval of -0.008 to +0.027, which this set cannot tell apart from no difference.
