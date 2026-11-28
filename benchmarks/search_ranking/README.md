# Search Ranking Benchmark

320 English requests over four public APIs, each judged for which operations answer it. It measures how well `search_operations` ranks the operation a request asks for. Results are on the [Evaluation](https://mroops0111.github.io/openapi-mcp-gateway/evaluation/#search-ranking) page, and [`eval/README.md`](../../eval/README.md#search-ranking) covers the metrics, the CI gate and how to rerun it.

## Contents

Each spec has 80 requests, written as a user would ask an assistant, in three styles.

| Spec | Operations | Requests |
| --- | --- | --- |
| GitHub | 1,281 | 80 |
| Stripe | 612 | 80 |
| Asana | 271 | 80 |
| Discord | 250 | 80 |

| Style | Meaning | Example | Mean Overlap |
| --- | --- | --- | --- |
| **literal** | the operation's own wording | "Send a message to the #announcements channel" | 0.85 |
| **paraphrase** | the same intent in other words | "Wipe the last 50 spam posts in #general in one go" | 0.07 |
| **indirect** | the goal, not the action | "I posted the wrong release date and the text is now incorrect" | 0.10 |

Overlap is the share of the answer's name and summary terms a request repeats, after the tokenizing and stemming search applies. Each spec has 27 literal, 27 paraphrase and 26 indirect requests.

Per spec, 10 requests of each style are drawn into **dev** by a fixed seed, and the other 50 are **test**. Tune on dev. Only test is reported or gated.

Each request is ranked under six variants.

| Variant | What It Ranks With | Compared With |
| --- | --- | --- |
| `default` | the gateway's `search_operations` under its default weights | |
| `equal-weights` | the same search with every field weighted alike | `default` |
| `openai-embeddings` | the same search ranking by meaning through OpenAI's `text-embedding-3-small` | `default` |
| `openai-embeddings-hybrid` | that embedding's ranking merged with the default's | `openai-embeddings` |
| `pydantic-ai-keywords` | Pydantic AI's keyword tool search over the same operations | `default` |
| `random` | a fixed shuffle, as a floor | `default` |

## Layout

Each spec keeps all of its data in one directory.

```
benchmark.yaml                            spec pins, variants, the CI gate
gate_scores.json                          the gated scores CI compares against
prompts/                                  each annotation role's instructions
specs/<spec>/annotations.jsonl            each request's two annotations and any adjudication
specs/<spec>/annotations.provenance.json  the annotators' runs, when models wrote them
specs/<spec>/queries.jsonl                the requests, in BEIR's format
specs/<spec>/qrels/{dev,test}.tsv         the graded judgements, in BEIR's format
```

## Specs

Each spec is pinned to a commit of its publisher's repository in [`benchmark.yaml`](benchmark.yaml) and checked by SHA-256. The documents are the operations as `search_operations` serves them under a plain dynamic config, named as the model sees them. Discord's spec has no summaries or descriptions, so its operations are searchable only by name and path, as in many real specs.

## Judgements

Two annotators judged every request independently, and `annotations.jsonl` keeps both judgements and any adjudication.

- **Grades**: `2` for the operation that does what was asked, `1` for one that would also get there, such as a list where a get fits better. Anything unjudged is `0`.
- **Merging**: an operation is graded `2` only when both annotators gave it `2`, and `1` when either judged it relevant.
- **Adjudication**: a request whose annotators share no answer is settled by a third look, recorded with a note.

| Spec | Shared Answer | Adjudicated | Cohen's Kappa |
| --- | --- | --- | --- |
| GitHub | 79 / 80 | 1 | 0.45 |
| Stripe | 80 / 80 | 0 | 0.56 |
| Asana | 80 / 80 | 0 | 0.72 |
| Discord | 80 / 80 | 0 | 0.51 |

Kappa is computed over every operation either annotator judged, so it runs lower than a kappa over the whole corpus would. The annotators agree on the answer almost everywhere, and differ over which lesser operations count.

## Data Provenance

Two Claude agents configured as `claude-opus-5-5` wrote the requests and judgements. One wrote each request with its judgement, and the other judged it from its text alone. Each saw the operation list, never the ranker.

`search-ranking generate-annotations` makes the data anew with models through Pydantic AI, in four roles, each from its template in [`prompts/`](prompts/).

1. **Query Author**: writes the requests with its own judgements.
2. **Independent Assessor**: judges each request from its id and text alone.
3. **Adjudicator**: settles each request the two share no answer for, and notes why.
4. **Reviser**: rewords a paraphrase or indirect request that repeats more than half of its answer's terms.

Running the assessor and the adjudicator on other providers' models keeps the annotators independent.

```bash
uv run omgw benchmark search-ranking generate-annotations \
  --author-model anthropic:claude-sonnet-5-5 \
  --assessor-model openai:<model> --adjudicator-model google:<model>
```

## Validation

`search-ranking check` calls no model, and CI runs it on every change.

- **Consistent Data**: the files load, every request is judged in exactly one split, and every judged operation is one the spec serves.
- **Honest Style Labels**: a paraphrase or indirect request that repeats more than half of its answer's terms is refused, since such a label flatters a word-based ranker.
- **No Regression**: the `default` variant's test scores stay within the manifest's tolerance of `gate_scores.json`.

## Known Limitations

These limits apply before you cite a number.

- **One Model Annotated Twice**: both annotators were instances of one model, which share its mistakes, so their agreement says less about correctness than independent annotators would.
- **One Annotator Wrote the Requests**: each request was written with an answer in mind, which favors requests with one clear answer.
- **No Parameters Shown**: the annotators saw each operation's name, method, path and summary only, so operations that differ only in their parameters may be told apart less well.
- **Well-Known APIs**: the annotating model has likely seen GitHub's and Stripe's documentation, which may shape the requests' wording. Search calls no model, so this shapes the data, not the ranker's scores.
- **English Only**: search does not segment Chinese or other unspaced scripts, so the set holds English requests only.
- **Strict Paraphrases**: the overlap rule keeps paraphrases far from the answer's words, so they are harder than many real requests.

## Versioning

`version` in `benchmark.yaml` names the data. A change to a spec pin, a request, a judgement, a prompt or the split seed makes a new version, and updates `gate_scores.json` with it. Scores are cited with their version, and a version is reproduced from the commit that holds it.
