# Search Benchmark

320 English requests over four public APIs, each judged for which operations answer it, for measuring how well `search_operations` ranks the operation a request asks for. The data is a first version, built to give the framework something to run, and is expected to be revised or annotated again.

## Corpus

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

Overlap is the share of the answer's name and summary terms the query repeats, after the tokenizing and stemming search applies. `search check` refuses a paraphrase or indirect query above 0.5. One indirect query, `asana-q077`, was over that line as first written and was reworded.

## Judgements

Two annotators judged every query independently. [`annotations/`](annotations/) holds both annotators' judgements per query, any adjudication, and the instructions each annotator was given.

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

[`annotations/README.md`](annotations/README.md) describes how the data is annotated again, and `search annotations build` rebuilds the queries and judgements from the annotations.

## Splits

Per spec, 10 queries of each style are drawn into **dev** by a fixed seed, and the other 50 are **test**. Tune on dev. Only test is reported or gated.

| Split | Queries |
| --- | --- |
| dev | 120 |
| test | 200 |

## Known Biases

The data has six known biases, each worth weighing before citing a number.

- **One model annotated twice.** Both annotators were Claude agents in one session, configured as `claude-opus-5-5`, on 2026-11-14. Two instances of one model share its mistakes, so their agreement says less about correctness than two independent annotators would. A second annotation from another provider's model is planned once #163 brings in a multi-provider framework.
- **One annotator wrote the queries.** Annotator A wrote each query with an answer in mind, which favours queries with one clear answer over genuinely ambiguous ones.
- **No parameters shown.** Both annotators saw each operation's name, method, path, and summary, and no parameters or full descriptions, so operations that differ only in their parameters may be told apart less well.
- **Well-known APIs.** The annotating model has likely seen GitHub's and Stripe's documentation in training, so its queries may lean on their official vocabulary, and its judgements there may rest on what it remembers as well as on the list. Search itself calls no model, so this shapes the data, not the ranker's scores.
- **English only.** Search does not segment Chinese or other unspaced scripts, so a set in those languages waits on tokenizer work or #166.
- **Strict paraphrases.** The overlap rule keeps paraphrases far from the answer's words, so they are harder than many real requests, which tend to repeat some of an operation's vocabulary.

## Versioning

`version` in `benchmark.yaml` names the data. A change to a spec pin, a query, a judgement, or the split seed makes a new version, and the PR that makes it updates `baseline.json`. Scores are cited with their version, and an older version is reproduced from the commit that held it.
