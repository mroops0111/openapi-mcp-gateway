---
description: How the gateway is measured with a tool calling benchmark and a search ranking benchmark, the results on Claude Haiku, and their limits.
---
# Evaluation

Tests check that the gateway does what it specifies. Evaluation measures whether that helps a model, against the usual approach of one tool per operation.

## Benchmarks

The gateway has two benchmarks, one for each feature whose value depends on a model.

| Benchmark | Question | Size | Calls a Model | Runs |
| --- | --- | --- | --- | --- |
| [Tool Calling](https://github.com/mroops0111/openapi-mcp-gateway/tree/master/benchmarks/tool_calling) | Does a model finish real work through the gateway, and at what cost? | 42 tasks over 7 scenarios | yes | on demand, with its data checked on every change |
| [Search Ranking](https://github.com/mroops0111/openapi-mcp-gateway/tree/master/benchmarks/search_ranking) | Does `search_operations` rank the requested operation near the top? | 320 requests over 4 APIs | no | on every change, in CI |

Both keep their specs, tasks and configs as data in the repository, and [eval/README.md](https://github.com/mroops0111/openapi-mcp-gateway/blob/master/eval/README.md) holds the commands to rerun them.

## Run Method

A tool calling run puts a model in front of the gateway and grades what reaches the upstream, never what the model says.

- **No Real API**: a replay server stands in for every upstream and records each request.
- **Graded by Requests**: a run passes when every expected write reaches the upstream and no forbidden one does. No model grades another.
- **Unseen Tasks**: the generated configs are written by the generate-config skill from each scenario's one-sentence intent, and the skill never sees the tasks.

## Tool Calling on Claude Haiku

These results come from 28 held-out tasks on Claude Haiku, 3 runs per task. Every scenario runs the same five variants. Three are the gateway, and two are the setups it is compared with.

| Variant | Side | What the Model Gets |
| --- | --- | --- |
| `baseline` | comparison | one tool per operation, as other OpenAPI-to-MCP tools serve a spec |
| `pydantic-ai-tool-search` | comparison | the baseline's tools, deferred until Pydantic AI's tool search finds them |
| `dynamic` | gateway | the gateway's [dynamic exposure](concepts/dynamic-exposure.md), three meta-tools that search, describe and call every operation |
| `generated` | gateway | the config the generate-config skill writes from the scenario's one-sentence intent |
| `generated-shaped` | gateway | the config the skill writes when also asked to shape the tools |

![Mean input tokens per run on Claude Haiku, with the share of runs passed. For comparison, the baseline used 940k tokens and passed 86%, and Pydantic AI's tool search used 272k and passed 71%. The gateway's generated configs used 130k and passed 92%, dynamic exposure used 76k and passed 100%, and generated-shaped configs used 68k and passed 86%.](assets/input-tokens-light.svg#only-light) ![Mean input tokens per run on Claude Haiku, as described above.](assets/input-tokens-dark.svg#only-dark)

The first table gives the share of runs passed by scenario size, and the second gives each variant's input tokens and cost. Bold marks the best value in a column where one variant stands out.

| Variant | Small | Medium | Large | GitHub With Asana | All |
| --- | --- | --- | --- | --- | --- |
| `baseline` | 100% | 100% | 100% | 0% | 86% |
| `pydantic-ai-tool-search` | 100% | 46% | 100% | 8% | 71% |
| `dynamic` | 100% | 100% | 100% | **100%** | **100%** |
| `generated` | 96% | 100% | 88% | 75% | 92% |
| `generated-shaped` | 100% | 100% | 88% | 25% | 86% |

| Variant | Input Tokens per Run | Cost of 84 Runs |
| --- | --- | --- |
| `baseline` | 940,145 | $4.94 |
| `pydantic-ai-tool-search` | 271,522 | $3.02 |
| `dynamic` | 76,348 | $0.22 |
| `generated` | 130,028 | $0.20 |
| `generated-shaped` | **67,565** | **$0.12** |

The baseline's GitHub with Asana runs exceed the context window at their first request and count as zero tokens, so its true token mean is higher. Each finding below holds on Claude Haiku only.

- **Dynamic Exposure**: it passed every run on 8% of the baseline's input tokens.
- **The Baseline**: it cannot run GitHub with Asana at all. Its tool definitions come to more than a million tokens, past the model's context window.
- **Pydantic AI's Tool Search**: it saves fewer tokens than dynamic exposure, and loses runs on the medium scenarios and on GitHub with Asana.
- **Generated Configs**: they cut input tokens by more than 85%, and `generated-shaped` loses most of its failed runs on GitHub with Asana.

??? note "Run Details"

    These details matter when you rerun or check the results.

    - **Versions**: run on 2026-11-24 with a 0.8.0 development build of openapi-mcp-gateway and Pydantic AI 2.69.0, on `anthropic:claude-haiku-5-5`. The generated configs were written by `anthropic:claude-sonnet-5-5`.
    - **Scenario Sizes**: small is Petstore and Notion, medium is Asana and Discord, and large is GitHub and Stripe. GitHub with Asana serves both APIs together, 1,552 operations in all.
    - **Task Split**: each scenario splits its tasks into 2 dev and 4 test, and only the test tasks are reported. CI replays each task's known solution through the real gateway, so every task can pass.
    - **Replay Answers**: the replay server answers from the task's data, else from the spec's example, else from a sample built from the response schema, so a model may call any operation.
    - **Cost**: cost also counts output tokens, and Anthropic's prompt caching of the tool definitions lowers the price of repeated input, so cost does not track input tokens.
    - **Failed Runs**: on GitHub with Asana, `generated-shaped` hides the milestone list one task reads, and on two other tasks the model made no write or passed a GitHub login as the Asana assignee.
    - **Pinned Specs**: each benchmark pins its specs by SHA-256, so a result can be reproduced from the commit that produced it.

## Search Ranking

These results come from 200 held-out requests. A third use the operation's own words, and the rest paraphrase it or state only the goal. Bold marks the best value in each column.

| Ranker | nDCG@10 | Answer on the First Page | MRR@10 | Answer First |
| --- | --- | --- | --- | --- |
| `search_operations`, Default Weights | **0.377** | 54% | **0.358** | **30%** |
| `search_operations`, Equal Weights | 0.367 | **55.5%** | 0.342 | 27.5% |
| Pydantic AI's Keyword Tool Search | 0.230 | 44.5% | 0.206 | 14% |
| Random Order | 0.003 | 4% | 0.001 | 0% |

Every literal request finds its operation on the first page, while a paraphrased or indirect one does so about three times in ten. Dynamic exposure still passed every tool calling run, and `search_operations` takes up to 10 phrasings in one call. Semantic search, set up in [Searching by Meaning](guides/exposure.md#searching-by-meaning), is meant to narrow the gap, and its results are not published yet.

??? note "Run Details"

    These details matter when you rerun or check the results.

    - **Version**: run with a 0.8.0 development build of openapi-mcp-gateway. Search calls no model, so no model is named.
    - **Metrics**: nDCG@10 scores the first ten results from 0 to 1, giving more credit the higher the requested operation, and any other that would serve, ranks. MRR@10 is one over the rank of the requested operation, or 0 when it falls outside the first ten.
    - **Intervals**: the default weights beat Pydantic AI's keyword search by 0.147 nDCG@10, with a 95% interval of +0.101 to +0.192. Against equal weights the difference is 0.010, an interval of -0.008 to +0.027, which this set cannot tell apart from none.

    By the style of request, under the default weights:

    | Request Style | nDCG@10 | Answer on the First Page |
    | --- | --- | --- |
    | Literal | 0.891 | 100% |
    | Paraphrase | 0.117 | 32.4% |
    | Indirect | 0.107 | 28.1% |

## Limits of These Results

The benchmarks are a first version, and these limits apply before you cite a number.

- **One Model**: only Claude Haiku has run the tool calling benchmark. OpenAI's and Google's small models are configured but not yet run.
- **Self-Authored Data**: Claude wrote the tasks, and Claude running the generate-config skill wrote the generated configs. Both may be phrased the way a Claude model expects to read them.
- **One Annotator Model**: two instances of one Claude model judged the search requests, so their agreement says less than two independent annotators would.
- **Well-Known APIs**: models have likely seen GitHub's and Stripe's documentation in training, so a model may reach an operation from memory.
- **Synthetic Upstreams**: replayed responses are plausible, not real, and Stripe's form-encoded writes reach the model with no body fields.

## Measuring Your Own Config

The `eval` command runs the same tool calling harness on tasks you write for your own API, as [Measuring a Config](guides/authoring-with-ai.md#measuring-a-config) explains.

## Measuring Your Own Config

The `eval` command runs the same tool calling harness on tasks you write for your own API, as [Measuring a Config](guides/authoring-with-ai.md#measuring-a-config) explains.

## Related

These pages cover the generated configs the benchmarks measure and how to measure your own.

- **Guides**: [Authoring With AI](guides/authoring-with-ai.md#measuring-a-config) for running `eval` on your own tasks and [Tool Shaping](guides/tool-shaping.md) for the `tool` blocks the `generated-shaped` configs add
