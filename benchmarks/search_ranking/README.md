# Search Ranking Benchmark

320 English requests over four public APIs, each judged for which operations answer it, for measuring how well `search_operations` ranks the operation a request asks for. The data is a first version, built to give the framework something to run, and is expected to be revised or annotated again.

## Layout

Each spec keeps all of its data in one directory, and every instruction a model is given is kept as data too.

```
benchmark.yaml                              spec pins, variants, and the CI gate
gate_scores.json                            the gated scores CI compares against
prompts/query_author.txt                    the query author's instructions, as a template
prompts/independent_assessor.txt            the independent assessor's instructions, as a template
specs/<spec>/annotations.jsonl              each query's two annotations and any adjudication
specs/<spec>/annotations.provenance.json    the annotators' runs, when models were run through Pydantic AI
specs/<spec>/queries.jsonl                  queries, in BEIR's format
specs/<spec>/qrels/{dev,test}.tsv           graded judgements, in BEIR's format, which names them qrels
```

## Specs

Four specs, each pinned to one commit and checked by SHA-256 in [`benchmark.yaml`](benchmark.yaml). The documents are the operations as `search_operations` serves them under a plain dynamic config, named as the model sees them.

| Spec | Operations | OpenAPI |
| --- | --- | --- |
| GitHub | 1,281 | 3.0.3 |
| Stripe | 612 | 3.0.0 |
| Asana | 271 | 3.0.0 |
| Discord | 250 | 3.1.0 |

Discord's spec has no summaries or descriptions, so its operations are searchable only by name and path. That is kept on purpose, since many real specs are like it.

## Queries

80 per spec, in English, as a user would ask an assistant. Each spec has 27 literal, 27 paraphrase, and 26 indirect requests.

| Style | Meaning | Example | Mean Overlap |
| --- | --- | --- | --- |
| **literal** | the operation's own wording | "Send a message to the #announcements channel" | 0.85 |
| **paraphrase** | the same intent in other words | "Wipe the last 50 spam posts in #general in one go" | 0.07 |
| **indirect** | the goal, not the action | "I posted the wrong release date and the text is now incorrect" | 0.10 |

Overlap is the share of the answer's name and summary terms the query repeats, after the tokenizing and stemming search applies.

## Judgements

Two annotators judged every query independently. Each spec's `annotations.jsonl` holds both annotators' judgements per query and any adjudication, one query per line:

```json
{"id": "github-q056", "text": "...", "style": "paraphrase", "annotator_a": {"checks_rerequest_run": 2}, "annotator_b": {"actions_re_run_job_for_workflow_run": 2}, "adjudication": {"relevant": {...}, "note": "..."}}
```

`annotator_a` is the query author and `annotator_b` the independent assessor, each mapping operation names to grades. The queries and qrels are built from these files.

- **Grades**: `2` for the operation that does what was asked, `1` for one that would also get there, such as a list operation where a get operation fits better. Anything unjudged is `0`.
- **Merging**: an operation is graded `2` only when both annotators gave it `2`, and `1` when either judged it relevant at all.
- **Adjudication**: a query whose annotators share no answer is settled by a third look, recorded with a note.

| Spec | Shared Answer | Adjudicated | Cohen's Kappa |
| --- | --- | --- | --- |
| GitHub | 79 / 80 | 1 | 0.45 |
| Stripe | 80 / 80 | 0 | 0.56 |
| Asana | 80 / 80 | 0 | 0.72 |
| Discord | 80 / 80 | 0 | 0.51 |

Kappa is computed over the grade of every operation either annotator judged, so it leaves out the operations both left at `0` and runs lower than a kappa over the whole corpus would. The annotators agree on the answer itself almost everywhere, and disagree over which lesser operations count.

## Variants

Each variant is a way of ranking a spec's operations, declared in the manifest, whose `type` picks its kind.

| Variant | What It Ranks With | Compared With |
| --- | --- | --- |
| `default` | the gateway's `search_operations` under its default weights | |
| `equal-weights` | the same search with every field weighted alike | `default` |
| `openai-embeddings` | the same search ranking by meaning through OpenAI's `text-embedding-3-small` | `default` |
| `openai-embeddings-hybrid` | that embedding's ranking merged with the default's | `openai-embeddings` |
| `pydantic-ai-keywords` | Pydantic AI's keyword tool search, over the same operations | `default` |
| `random` | a fixed shuffle, as a floor | `default` |

The two OpenAI variants need `OPENAI_API_KEY`, and the others call no model. A gateway variant's `server` is a fragment of a server config, such as an `exposure` block, so comparing two configurations needs no code. `default` is the gated variant, and every other is compared with it.

## Splits

Per spec, 10 queries of each style are drawn into **dev** by a fixed seed, and the other 50 are **test**. Tune on dev. Only test is reported or gated.

| Split | Queries |
| --- | --- |
| dev | 120 |
| test | 200 |

## Data Provenance

Claude agents annotated version 1 by hand, and `search-ranking generate` can annotate the data again with models run through Pydantic AI.

- **Version 1** was annotated by two Claude agents launched from one Claude Code session on 2026-11-14, configured as `claude-opus-5-5`. They worked from the same operation lists the first stage writes today, byte for byte. Their instructions said what the templates in [`prompts/`](prompts/) say, under other headings, with two differences. Each spec's copy named that spec's areas and gave examples from its own operations, and the files were named by hand. Running stages 3 and 5 below on their raw output reproduces the annotations exactly, except `asana-q077`, which stage 8 had reworded, and the one adjudication, which stage 7 added. A hand-run annotation has no provenance file.
- **A generated annotation** runs stages 1 to 4 with both annotators run through Pydantic AI. Each annotator gets the same template as a hand-run one, with its operation list in the prompt and a tool that searches the list for a word, never the ranker. It answers in structured output, and is sent back to fix an answer that names an operation the spec does not serve, or that skips a query or a style. Collect keeps the annotators' runs beside the annotations as `annotations.provenance.json`, with each model, the hash of its instructions, the finishing time, and the token usage. Every raw exchange stays in the work directory.

```bash
uv run openapi-mcp-gateway-benchmarks search-ranking generate benchmarks/search_ranking work \
  --author-model anthropic:claude-sonnet-5-5 --assessor-model openai:<model>
```

Annotating the data again takes eight stages. Three are done by hand or by `generate`, the two annotators' work and any adjudication. The other five are commands, so the same specs and the same templates always give the annotators the same files, and the same annotator output always gives the same data. The commands write their working files into a directory of your choosing, here `work/`, outside the repository.

1. **Prepare authoring.** `search-ranking annotations prepare-authoring benchmarks/search_ranking work` writes each spec's `operations.tsv`, listing every operation as the gateway serves it, and `query_author_instructions.txt`, rendered from [`prompts/query_author.txt`](prompts/query_author.txt).
2. **Author the queries.** Give one annotator each spec's instructions, with no access to anything but its operation list. It writes `query_author.jsonl`.
3. **Prepare assessment.** `search-ranking annotations prepare-assessment benchmarks/search_ranking work` gives each query its id, by its line in the author's file, and writes `queries.jsonl` without the author's judgements and `independent_assessor_instructions.txt`, rendered from [`prompts/independent_assessor.txt`](prompts/independent_assessor.txt).
4. **Assess the queries.** Give a second annotator each spec's assessor instructions, with no access to the author's judgements. It writes `independent_assessor.jsonl`.
5. **Collect.** `search-ranking annotations collect benchmarks/search_ranking work` checks that both annotators named only operations the spec serves, graded only 1 and 2, and gave every query an answer, then writes each spec's `annotations.jsonl`.
6. **Build.** `search-ranking annotations build benchmarks/search_ranking` merges the annotations into the queries and judgements, and reports the agreement. It stops at any query whose annotators share no answer.
7. **Adjudicate.** For each query the build stopped at, a third look adds an `adjudication` with its judgements and a note, and the build runs again.
8. **Check.** `search-ranking check benchmarks/search_ranking --update` refuses any paraphrase or indirect query that repeats its answer's words. Reword each one it names in its spec's `annotations.jsonl`, build again, and rerun until it passes, which also writes the new gate scores.

The two annotators should be independent, which a new annotation can make stronger than version 1 did.

- **Different models.** Two instances of one model share its mistakes. Run the assessor on another provider's model where one is available.
- **No ranker.** Neither annotator may run search or see its output, so the queries say nothing about how the ranker behaves.
- **No answers.** The assessor never sees the author's file, only the queries the third stage wrote.

## Validation

`search-ranking check` calls no model, and CI runs it on every change.

- **The data is consistent.** The files load, every query is judged in exactly one split, and every judged operation is one the spec serves.
- **Every style label holds.** A paraphrase or indirect query that repeats more than half of its answer's terms is refused, since a label written by hand can be wrong in the direction that flatters a lexical ranker. One indirect query, `asana-q077`, was over that line as first written and was reworded.
- **The gated variant has not regressed.** It ranks the test split under `default` and fails when a gated metric falls below `gate_scores.json` by more than the manifest's tolerance.

## Known Biases

The data has six known biases, each worth weighing before citing a number.

- **One model annotated twice.** Both annotators were Claude agents in one session, configured as `claude-opus-5-5`, on 2026-11-14. Two instances of one model share its mistakes, so their agreement says less about correctness than two independent annotators would. A second annotation from another provider's model is planned, which `generate` now makes one command.
- **One annotator wrote the queries.** Annotator A wrote each query with an answer in mind, which favours queries with one clear answer over genuinely ambiguous ones.
- **No parameters shown.** Both annotators saw each operation's name, method, path, and summary, and no parameters or full descriptions, so operations that differ only in their parameters may be told apart less well.
- **Well-known APIs.** The annotating model has likely seen GitHub's and Stripe's documentation in training, so its queries may lean on their official vocabulary, and its judgements there may rest on what it remembers as well as on the list. Search itself calls no model, so this shapes the data, not the ranker's scores.
- **English only.** Search does not segment Chinese or other unspaced scripts, so a set in those languages waits on tokenizer work or #166.
- **Strict paraphrases.** The overlap rule keeps paraphrases far from the answer's words, so they are harder than many real requests, which tend to repeat some of an operation's vocabulary.

## Versioning

`version` in `benchmark.yaml` names the data. A change to a spec pin, a query, a judgement, a prompt, or the split seed makes a new version, and the PR that makes it updates `gate_scores.json`. Scores are cited with their version, and an older version is reproduced from the commit that held it.
