# Tool Calling Benchmark

42 tasks over seven scenarios, each run by a model through the gateway under several configs, for measuring whether the gateway's features help a model finish real work compared with one tool per operation. The data is a first version, built to give the harness something to run, and is expected to be revised.

## Layout

Each scenario keeps all of its data in one directory, and every instruction a model is given is kept as data too.

```
benchmark.yaml                                       spec pins, models, the repeat count, the system prompt, and the generator
prompts/config_generator.txt                         what the config generator is told on top of the skill
scenarios/<name>/scenario.yaml                       the intent and the servers
scenarios/<name>/tasks.yaml                          tasks with expected calls, a solution, and upstream responses
scenarios/<name>/variants/<variant>.yaml             one gateway config fragment per server
scenarios/<name>/variants/<variant>.provenance.json  the generator's run, beside each generated variant
```

## Specs

Six specs, each pinned to one commit and checked by SHA-256 in [`benchmark.yaml`](benchmark.yaml).

| Spec | Operations | OpenAPI |
| --- | --- | --- |
| GitHub | 1,281 | 3.0.3 |
| Stripe | 612 | 3.0.0 |
| Asana | 271 | 3.0.0 |
| Discord | 250 | 3.1.0 |
| Swagger Petstore | 19 | 3.0.4 |
| Notion | 13 | 3.0.3 |

Notion stands in for the TMDB spec #163 planned, since the TMDB spec in `examples/` has two operations and no fuller one can be pinned.

## Scenarios

Each scenario is one realistic use of one or two APIs, stated as a one-sentence intent in its `scenario.yaml`.

| Scenario | APIs | Operations | Intent |
| --- | --- | --- | --- |
| `petstore` | Swagger Petstore | 19 | Run a small pet shop |
| `notion` | Notion | 13 | Keep a team's notes organized |
| `asana` | Asana | 271 | Plan and track a team's tasks |
| `discord` | Discord | 250 | Moderate a community server |
| `github` | GitHub | 1,281 | Triage issues and review pull requests |
| `stripe` | Stripe | 612 | Handle a shop's billing |
| `github-asana` | GitHub and Asana | 1,552 | Turn GitHub work into tracked Asana tasks |

## Tasks

Each scenario has 6 tasks in `tasks.yaml`, 42 in all. 39 of the 42 need two or more calls, mostly finding something by name before changing it.

Every task is one the intent's own words name, such as cancelling an order for "handle customers' orders". A request the intent leaves out would only fail wherever a config follows the intent, which the config itself already shows.

Every task carries three things, and some a fourth:

- **Expected Calls**: the requests the upstream must receive, by method, path template, and the values the user's request decides. A step that the API does by more than one endpoint lists them under `any_of`, and any one counts. A way that takes several requests lists them under `all_of`.
- **A Solution**: the calls an ideal model would make, with arguments as each tool's schema wants them.
- **Responses**: what the upstream answers for the requests a task needs particular data from, with a decoy beside the answer. Every route a model could find the data by, such as a list, a search, or a single read, answers with the same data, and a response may name the query values it answers, as a typeahead answers projects and users apart.
- **Forbidden Calls**: requests that fail the task, only where the request says what to leave alone.

A run passes when every expected call reaches the upstream and no forbidden call does.

Any other write is counted as an extra write without failing the run. A harmless one, such as setting a field after creating a record, says little about whether the task got done, while the count still shows a variant whose model changes more than asked. Some operations only read though their method writes, such as Notion's database query, a `POST`. The manifest lists them under `read_only_operations`, and the grader counts them as reads.

A model may fix a tool call the gateway refused up to `tool_retries` times, three by default, before the run fails.

## Upstream

No real API is called. A replay server stands in for every upstream, answering each request from the task's upstream responses, else from the spec's own example, else from a fixed sample of the response schema, so a model may call any operation, not only the expected ones. It records every request for the grader.

## Report

`tool-calling run` reports each scenario, variant, and model with its success rate, its tokens and the share of them read from the prompt cache, and what its runs cost, each model request priced on its own from Pydantic AI's price data. Each run keeps the requests the upstream received and where each answer came from, so a run that failed after reading data its task never set shows as a gap in the data rather than the model's miss.

## Variants

Each file in a scenario's `variants/` is a gateway server config fragment per server. No variant is written by hand for the intent. The first three need no intent, and the generator writes the last two.

| Variant | What It Adds | Compared With |
| --- | --- | --- |
| `baseline` | nothing: one tool per operation, the approach other OpenAPI-to-MCP tools share | |
| `pydantic-ai-tool-search` | the baseline's tools, each deferred until Pydantic AI's tool search finds it | the third-party way to keep many tools out of the context |
| `dynamic` | the gateway's dynamic exposure alone, so a model searches the operations and calls one | `pydantic-ai-tool-search` |
| `generated` | what the generate-config skill writes from the intent alone | `baseline`, and `dynamic` |
| `generated-shaped` | what the skill writes when the user also asks for shaped tools | `generated` |

`pydantic-ai-tool-search` sets `tool_search: true` instead of a server fragment. The model then searches with its provider's native tool search where there is one, else with Pydantic AI's keyword search. Every variant is compared with `baseline` on the report's measures, and the column names the comparison each one is there for.

## Splits

Each scenario has 2 **dev** tasks and 4 **test** tasks. Try a change on dev. Only test is reported. Loading refuses scenarios whose tasks split differently, so each scenario weighs the same in a mean over them.

| Split | Tasks |
| --- | --- |
| dev | 14 |
| test | 28 |

## Data Provenance

Claude agents wrote every task by hand, and `tool-calling generate-config` writes every generated variant with a model running the skill.

- **Task writers** were Claude agents launched from one Claude Code session on 2026-11-15, configured as `claude-opus-5-5`. They saw the intent and the operation list, and wrote the tasks. Each checked its tasks by running every solution through the real gateway against the stand-in upstream. Hand-written tasks have no provenance file.
- **The config generator** is the model the manifest names under `config_generator`, running the generate-config skill in a workspace holding the scenario's pinned specs. It is told [`prompts/config_generator.txt`](prompts/config_generator.txt) on top of the skill, and is sent back until its config loads. It sees the intent, and never the tasks. Each generated variant keeps its run beside it as `<variant>.provenance.json`, with the model, the hash of its instructions, the skill's hash, the request, the finishing time, and the token usage. Every raw exchange stays in the work directory. A generated config is regenerated, never edited.

```bash
uv run omgw benchmark tool-calling generate-config --scenario github
```

## Validation

`tool-calling check` calls no model, and CI runs it on every change.

- **Every task can pass.** Each task's solution runs under the baseline through the gateway and the replay upstream, and has to pass the grader with no failing tool call.
- **Every hand-written variant reaches what each task needs.** Every task is one the intent names, so a hand-written variant that hides an operation a task expects is a mistake in its config.

A generated variant may leave a task out of reach too, and that is the generator's miss to measure, never one to fix by hand. `check` lists those tasks without failing, and `run` reports them as intent coverage beside the success rates.

## Known Biases

The data has five known biases, each worth weighing before citing a number.

- **Model-written data.** Claude models wrote the tasks and the generated configs, and may phrase both the way a Claude model expects to read them.
- **Well-known APIs.** Models have likely seen GitHub's and Stripe's documentation in training, so a model may reach an operation from memory rather than through the tools. A scenario over an API models are unlikely to know is planned.
- **Stripe writes carry no body.** The gateway reads only JSON request bodies, and Stripe's are form-encoded, so no Stripe tool accepts body fields. Stripe's tasks are therefore writes that need only ids in the path, such as refunding a charge or cancelling a subscription. A refund through `POST /v1/refunds`, which the grader accepts, needs the charge in its body, so a variant that serves only that endpoint cannot refund.
- **Notion's broken parameter.** Notion's spec declares a header parameter with no name on one operation, which the gateway now skips with a warning.
- **Synthetic responses.** A response built from a schema is plausible but not real, so a task that depends on data a real API would return carries that data in its upstream responses.

## Versioning

`version` in `benchmark.yaml` names the data. A change to a spec pin, a task, a variant, a prompt, or the system prompt makes a new version. Results are cited with their version, and an older version is reproduced from the commit that held it.
