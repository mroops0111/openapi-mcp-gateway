# Tool Calling Benchmark

42 tasks over seven scenarios, which a model carries out through the gateway under several configs. It measures whether the gateway's features help a model finish real work, compared with one tool per operation. Results are on the [Evaluation](https://mroops0111.github.io/openapi-mcp-gateway/evaluation/) page, and [`eval/README.md`](../../eval/README.md#tool-calling) covers how a run is graded and how to rerun it.

## Contents

Each scenario is one realistic use of one or two APIs, stated as a one-sentence intent, with six tasks the intent names.

| Scenario | APIs | Operations | Intent |
| --- | --- | --- | --- |
| `weather` | National Weather Service | 69 | Keep an outdoor-events team informed about the weather where its events take place |
| `notion` | Notion | 13 | Keep a team's notes organized |
| `asana` | Asana | 271 | Plan and track a team's tasks |
| `discord` | Discord | 250 | Moderate a community server |
| `github` | GitHub | 1,281 | Triage issues and review pull requests |
| `stripe` | Stripe | 612 | Handle a shop's billing and refunds |
| `github-asana` | GitHub and Asana | 1,552 | Turn GitHub work into tracked Asana tasks |

Each scenario splits its tasks into 2 **dev** and 4 **test**. Try a change on dev. Only test is reported.

Each scenario is served under five variants.

| Variant | What It Serves | Compared With |
| --- | --- | --- |
| `baseline` | one tool per operation, as other OpenAPI-to-MCP tools do | |
| `pydantic-ai-tool-search` | the baseline's tools, each deferred until Pydantic AI's tool search finds it | the third-party way to keep tools out of the context |
| `dynamic` | the gateway's dynamic exposure | `pydantic-ai-tool-search` |
| `generated` | the config the generate-config skill writes from the intent | `baseline` and `dynamic` |
| `generated-shaped` | the config the skill writes when shaped tools are also asked for | `generated` |

## Layout

Each scenario keeps all of its data in one directory.

```
benchmark.yaml                  spec pins, models, repeats, system prompt, generator
prompts/config_generator.txt    what the generator is told on top of the skill
specs/<spec>.json               a dated spec copy, where the publisher's repository lags
scenarios/<name>/scenario.yaml  the intent and the servers
scenarios/<name>/tasks.yaml     the tasks, their expected calls and upstream responses
scenarios/<name>/variants/      one config fragment per variant, and each generated
                                variant's run beside it as <variant>.provenance.json
```

## Specs

Each spec is pinned in [`benchmark.yaml`](benchmark.yaml) and checked by SHA-256.

| Spec | Source |
| --- | --- |
| GitHub, Stripe, Asana, Discord | a commit of the publisher's repository |
| Notion | a commit of APIs-guru's community OpenAPI directory |
| National Weather Service | a copy of the live spec taken on 2026-11-27 UTC, since the publisher's repository lags the live API |

The gateway and the config generator load each spec with its `security` removed, since no benchmark reaches a real API or checks a credential. Every API here also has a config under [`examples/`](../../examples) that runs against the real API.

## Data Provenance

Claude agents wrote the tasks, and a model running the generate-config skill wrote the generated variants.

- **Tasks**: written by Claude agents configured as `claude-opus-5-5`, which saw the intent and the operation list. Each solution was run through the gateway before any model ran the tasks. A task is frozen from then on, and changes only when its own solution stops passing `check`.
- **Generated Variants**: written by the model named under `config_generator`, which sees the intent and the specs, never the tasks. A generated config is regenerated, never edited, and its run is kept beside it.
- **Other Variants**: written by hand, and none is fitted to the intent.

## Validation

`tool-calling check` calls no model, and CI runs it on every change.

- **Every Task Can Pass**: each task's solution runs under the baseline and passes the grader.
- **Hand-Written Variants Reach Every Task**: a hand-written variant that hides an operation a task needs is a mistake in its config.

A generated variant that leaves a task out of reach is a result to report, not a mistake to fix. `check` lists it, and the report shows it as intent coverage.

## Known Limitations

These limits apply before you cite a number.

- **Model-Written Data**: Claude models wrote the tasks and the generated configs, and may phrase both the way a Claude model expects to read them.
- **Well-Known APIs**: models have likely seen GitHub's and Stripe's documentation, so a model may reach an operation from memory.
- **Stripe Writes Without a Body**: the gateway reads only JSON request bodies, and Stripe's are form-encoded, so Stripe's tasks are writes that need only ids in the path.
- **Reads Graded by Request**: the weather tasks only read, and the grader checks the requests a run sends, not the answer it gives.
- **Replayed Responses**: the upstream is a replay, which answers in `application/json` and builds any response a task does not set from the spec, so it is plausible but not real.

## Versioning

`version` in `benchmark.yaml` names the data. A change to a spec pin, a task, a variant or a prompt makes a new version. Results are cited with their version, and a version is reproduced from the commit that holds it.
