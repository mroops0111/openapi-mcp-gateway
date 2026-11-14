# Benchmarks

Tests check that the gateway does what it specifies. Benchmarks measure whether that helps a model, which has no single right answer and is judged against a baseline. The code lives in the package, under `openapi_mcp_gateway.evals`, and the data lives here, so the same commands run this repository's benchmarks and, later, a user's own.

| Benchmark | Measures | Calls a model | Runs |
| --- | --- | --- | --- |
| [`search/`](search/) | whether `search_operations` ranks the requested operation near the top | no | every PR, in CI |

## Layout

```
src/openapi_mcp_gateway/evals/     code, shipped in the package
  cli.py                           the commands, one group per benchmark
  corpus.py                        specs pinned by URL and SHA-256, downloaded once into a cache
  target.py                        a gateway built in-process from a server config, and an MCP client to it
  stats.py                         macro means and the paired bootstrap
  search/
    dataset.py                     benchmark.yaml, queries, and qrels, loaded and validated
    build.py                       two annotators' judgements merged into qrels
    retriever.py                   rankings from search_operations, and the random floor
    metrics.py                     nDCG, MRR, and recall over graded judgements
    overlap.py                     how much of its answer's wording a query repeats
    runner.py                      every query under every variant
    report.py                      summaries, comparisons, Markdown, and the CI baseline

evals/                             data, not shipped
  search/v1/
    benchmark.yaml                 spec pins, variants, and the CI gate
    DATASET.md                     how the data was made, and its known biases
    baseline.json                  the gated scores CI compares against
    annotations/<spec>/            each annotator's raw judgements, and the adjudications
    <spec>/queries.jsonl           queries, in BEIR's format
    <spec>/qrels/{dev,test}.tsv    graded judgements, in BEIR's format
```

A benchmark reaches the gateway the way a model does, through MCP, so renames, policy, and paging all count. A variant is a fragment of a server config, such as an `exposure` block, so comparing two configurations needs no code.

## Search

```bash
uv run python -m openapi_mcp_gateway.evals search run evals/search/v1        # every variant, test split
uv run python -m openapi_mcp_gateway.evals search run evals/search/v1 --split dev --variant default
uv run python -m openapi_mcp_gateway.evals search check evals/search/v1      # what CI runs
uv run python -m openapi_mcp_gateway.evals search validate evals/search/v1   # file checks and style overlap
uv run python -m openapi_mcp_gateway.evals search build evals/search/v1      # rebuild qrels from annotations
```

The first run downloads about 30 MB of pinned specs into `~/.cache/openapi-mcp-gateway`, or the directory `OPENAPI_MCP_GATEWAY_CACHE` names. A run takes a few seconds after that.

### Metrics

- **nDCG@10** is the headline, as in BEIR, since it uses the graded judgements.
- **Recall@20** says whether the answer is on the first page of `search_operations`, which holds 20.
- **MRR@10** and **Recall@1** count only the answer, grade `2`, and are kept for comparison with #146.

Every metric is averaged per spec and then over specs, so GitHub's size does not outweigh the others. Each run also scores a random ranking as the floor, and compares the gated variant with every other by a paired bootstrap over test queries, stratified by spec, reporting a 95% interval.

### Changing Search

Tune on dev, then run test once. A change that moves the ranking on purpose updates the baseline in the same PR, so the diff shows the new numbers:

```bash
uv run python -m openapi_mcp_gateway.evals search check evals/search/v1 --update
```

CI fails when nDCG@10 or Recall@20 of the `default` variant falls more than 0.01 below the baseline, over all specs or for any one of them. With 50 test queries a spec, one query changing rank can move a spec's Recall@20 by 0.02, so the gate catches any lost answer.

### Results

v1, test split, openapi-mcp-gateway 0.8.0:

| Variant | nDCG@10 | Recall@20 | MRR@10 | Recall@1 |
| --- | --- | --- | --- | --- |
| default | 0.367 | 0.532 | 0.350 | 0.295 |
| equal weights | 0.356 | 0.537 | 0.327 | 0.255 |
| random | 0.006 | 0.040 | 0.001 | 0.000 |

| Style | nDCG@10 | Recall@20 |
| --- | --- | --- |
| literal | 0.882 | 1.000 |
| paraphrase | 0.103 | 0.316 |
| indirect | 0.102 | 0.266 |

A literal request always finds its operation on the first page. A paraphrased or indirect one does so about three times in ten, so synonyms remain search's main weakness. The default weights beat equal weights by 0.012 nDCG@10, an interval of -0.007 to +0.031, which this set cannot tell apart from no difference.
