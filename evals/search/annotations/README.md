# Annotations

One file per spec, one query per line, holding everything the qrels are built from:

```json
{"id": "github-q056", "text": "...", "style": "paraphrase", "annotator_a": {"checks_rerequest_run": 2}, "annotator_b": {"actions_re_run_job_for_workflow_run": 2}, "adjudication": {"relevant": {...}, "note": "..."}}
```

`annotator_a` and `annotator_b` map operation names to grades. `adjudication` appears only where the two share no answer.

## Provenance

Both annotators were Claude agents launched from one Claude Code session on 2026-11-14, configured as `claude-opus-5-5`. Each worked from one file listing every operation of the spec with its `name`, `method`, `path`, and `summary`, and was told not to open anything else or run any ranker. Annotator B never saw annotator A's judgements.

The instructions below are the ones given, with the spec's name and its examples left as placeholders. Each spec's copy named that spec's areas and gave examples from its own operations.

## The Query Author

Annotator A wrote each spec's queries and judged them, with these instructions.

> You are writing test queries for a retrieval benchmark. The system under test is a search over the operations of the {spec} API: a user, or an AI assistant acting for them, types a request in plain English, and the search should return the API operation that fulfils it.
>
> **Your input** is one file listing every operation as `name`, `method`, `path`, `summary`. This is the only thing you may look at. Do not open any other file, do not look at any source code, and do not run any search engine or script that ranks operations. The benchmark is only fair if the queries are written without knowing how the ranker behaves.
>
> **Write 80 queries**, in English, each the way a real user would ask an assistant to do something. Spread them across the whole API, with no more than 6 queries in one area, and prefer operations a real user would plausibly want.
>
> Three styles, as close to a third each as possible, 27 literal, 27 paraphrase, and 26 indirect:
>
> - **literal** uses the operation's own wording, as the summary says it.
> - **paraphrase** is the same intent in different words. Do not reuse the summary's verb, and replace its other content words with synonyms or natural phrasing wherever a person plausibly would. Product names with no synonym may stay.
> - **indirect** describes the goal or situation, never the action itself. The reader must infer the operation.
>
> Rules for every query:
>
> - Never include an operation `name`, a path, or an HTTP method.
> - Each query must have one operation that is clearly the best answer, grade 2. If two are genuinely equally good, list both as grade 2.
> - Also list operations that would be an acceptable but lesser answer, grade 1, such as a list operation when a get operation is the better answer, or a broader operation that would also achieve the goal. Most queries have zero to three. Search the file for them and be thorough.
> - Concrete details, such as names, numbers, and ids, are welcome.
> - Vary length and phrasing.
>
> **Output** one JSON object per line, `{"text", "style", "relevant": {name: grade}}`, with every name checked against the file.

## The Independent Assessor

Annotator B judged the same queries without seeing annotator A's judgements, with these instructions.

> You are a relevance assessor for a retrieval benchmark, judging independently, the way TREC assessors do. The system under test searches the operations of the {spec} API for a plain-English request. Your job is to decide, for each request, which operations answer it.
>
> **Your input** is the same operation list, and the requests as `{"id", "text"}`. Only look at those two files. Do not open any other file, including any file of answers someone else wrote, and do not run any search engine or ranking script.
>
> For each request, find every relevant operation and grade it:
>
> - **2** does what the request asks. Usually exactly one operation. Give two only when they are genuinely interchangeable for this request.
> - **1** would also get the user there, but is a lesser answer, such as a list operation when a get-by-id operation is the better answer, a general update operation when a specific operation does exactly this, or a necessary close step of the same goal.
> - Leave everything else out.
>
> Every request must have at least one operation graded 2. Search the file for each request, trying several words and synonyms. Do not grade an operation 1 just because it is in the same area; it has to actually help with this request.
>
> **Output** one JSON object per line, `{"id", "relevant": {name: grade}}`, with every name checked against the file.
