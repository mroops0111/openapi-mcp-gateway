# Annotations

Everything the queries and judgements are built from, one file per spec, one query per line:

```json
{"id": "github-q056", "text": "...", "style": "paraphrase", "annotator_a": {"checks_rerequest_run": 2}, "annotator_b": {"actions_re_run_job_for_workflow_run": 2}, "adjudication": {"relevant": {...}, "note": "..."}}
```

`annotator_a` is the query author and `annotator_b` the independent assessor, each mapping operation names to grades. `adjudication` appears only where the two share no answer.

## Procedure

Annotating the data again takes eight steps. Three are done by hand, the two annotators' work and any adjudication, and the other five are commands, so the same specs and the same templates always give the annotators the same files, and the same annotator output always gives the same data. Only what the annotators write can differ between runs.

The commands below write their working files into a directory of your choosing, here `work/`, outside the repository.

1. **Prepare authoring.** `search annotations prepare-authoring benchmarks/search work` writes each spec's `operations.tsv`, listing every operation as the gateway serves it, and `query_author_instructions.txt`, rendered from [`query_author.txt`](query_author.txt).
2. **Author the queries.** Give one annotator each spec's instructions, with no access to anything but its operation list. It writes `query_author.jsonl`.
3. **Prepare assessment.** `search annotations prepare-assessment benchmarks/search work` gives each query its id, by its line in the author's file, and writes `queries.jsonl` without the author's judgements and `independent_assessor_instructions.txt`, rendered from [`independent_assessor.txt`](independent_assessor.txt).
4. **Assess the queries.** Give a second annotator each spec's assessor instructions, with no access to the author's judgements. It writes `independent_assessor.jsonl`.
5. **Collect.** `search annotations collect benchmarks/search work` checks that both annotators named only operations the spec serves, graded only 1 and 2, and gave every query an answer, then writes `annotations/<spec>.jsonl` here.
6. **Build.** `search annotations build benchmarks/search` merges the annotations into the queries and judgements, and reports the agreement. It stops at any query whose annotators share no answer.
7. **Adjudicate.** For each query the build stopped at, a third look adds an `adjudication` with its judgements and a note, and the build runs again.
8. **Check.** `search check benchmarks/search --update` refuses any paraphrase or indirect query that repeats its answer's words. Reword each one it names in `annotations/<spec>.jsonl`, build again, and rerun until it passes, which also writes the new baseline.

New annotations make a new benchmark version. Raise `version` in `benchmark.yaml`, and record in the dataset card which model played each annotator and on what date.

## Automated Annotation

`search annotations annotate` runs steps 1 to 4 with both annotators run through Pydantic AI, so a fresh annotation is one command followed by steps 5 to 8:

```bash
uv run python -m openapi_mcp_gateway.benchmarks search annotations annotate benchmarks/search work \
  --author-model anthropic:claude-sonnet-5-5 --assessor-model openai:<model>
```

Each annotator gets the same template as a hand-run one, with its operation list in the prompt and a tool that searches the list for a word, never the ranker. It answers in structured output, and is sent back to fix an answer that names an operation the spec does not serve, or that skips a query or a style. Every raw exchange is kept as `<role>.messages.json` beside the answers, and `provenance.json` records each annotator's model, finishing time, Pydantic AI version, and token usage, since a model's output cannot be reproduced.

It needs the `benchmarks` extra and each provider's API key in its usual variable.

## Annotators

The two annotators should be independent, which a run of this procedure can make stronger than version 1 did.

- **Different models.** Two instances of one model share its mistakes. Run the assessor on another provider's model where one is available.
- **No ranker.** Neither annotator may run search or see its output, so the queries say nothing about how the ranker behaves.
- **No answers.** The assessor never sees the author's file, only the queries the third stage wrote.

## Version 1

Both annotators were Claude agents launched from one Claude Code session on 2026-11-14, configured as `claude-opus-5-5`. They worked from the same operation lists the first stage writes today, byte for byte. Their instructions were the templates here with two differences. Each spec's copy named that spec's areas and gave examples from its own operations, and the files were named by hand. Running stages 3 and 5 on their raw output reproduces this directory exactly, except `asana-q077`, which stage 8 had reworded, and the one adjudication, which stage 7 added.
