# Tool Calling Benchmark

42 tasks over seven scenarios, each run by a model through the gateway under several configs, for measuring whether the gateway's features help a model finish real work compared with one tool per operation. The data is a first version, built to give the harness something to run, and is expected to be revised.

## Scenarios

Each scenario is one realistic use of one or two APIs, stated as a one-sentence intent in its `scenario.yaml`. Every spec is pinned to one commit and checked by SHA-256 in [`benchmark.yaml`](benchmark.yaml).

| Scenario | APIs | Operations | Intent |
| --- | --- | --- | --- |
| `petstore` | Swagger Petstore | 19 | Run a small pet shop |
| `notion` | Notion | 13 | Keep a team's notes organized |
| `asana` | Asana | 271 | Plan and track a team's tasks |
| `discord` | Discord | 250 | Moderate a community server |
| `github` | GitHub | 1,281 | Triage issues and review pull requests |
| `stripe` | Stripe | 612 | Handle a shop's billing |
| `github-asana` | GitHub and Asana | 1,552 | Turn GitHub work into tracked Asana tasks |

Notion stands in for the TMDB spec #163 planned, since the TMDB spec in `examples/` has two operations and no fuller one can be pinned.

## Tasks

Each scenario has 6 tasks in `tasks.yaml`, 42 in all, 2 for practice and 4 for the final runs. Loading refuses scenarios whose tasks split differently, so each scenario weighs the same in a mean over them. 39 of the 42 need two or more calls, mostly finding something by name before changing it.

Every task is one the intent's own words name, such as cancelling an order for "handle customers' orders". A request the intent leaves out would only fail wherever a config follows the intent, which the config itself already shows.

Every task carries three things, and some a fourth:

- **Expected Calls**: the requests the upstream must receive, by method, path template, and the values the user's request decides. A step that the API does by more than one endpoint lists them under `any_of`, and any one counts.
- **A Solution**: the calls an ideal model would make, with arguments as each tool's schema wants them.
- **Responses**: what the upstream answers for the requests a task needs particular data from, with a decoy beside the answer.
- **Forbidden Calls**: requests that fail the task, only where the request says what to leave alone.

A run passes when every expected call reaches the upstream and no forbidden call does.

Any other write is counted as an extra write without failing the run. A harmless one, such as setting a field after creating a record, says little about whether the task got done, while the count still shows a variant whose model changes more than asked. Some operations only read though their method writes, such as Notion's database query, a `POST`. The manifest lists them under `read_only_operations`, and the grader counts them as reads.

A model may fix a tool call the gateway refused up to `tool_retries` times, three by default, before the run fails.

## Variants

Each file in a scenario's `variants/` is a gateway server config fragment per server.

| Variant | What It Adds |
| --- | --- |
| `baseline` | nothing: one tool per operation, the approach other OpenAPI-to-MCP tools share |
| `policy` | `policy.allow` narrowed to the intent |
| `policy-dynamic` | the same policy behind dynamic exposure, for the specs with more than 50 operations |
| `tailored` | the policy, dynamic exposure where it applies, and tool shaping for the operations central to the intent |
| `generic` | what a user would write knowing nothing of the intent: dynamic exposure for a large spec, and rules that suit any use |

`policy`, `policy-dynamic`, and `tailored` add one feature at a time, so each step's gain shows.

## Data Provenance

Two kinds of annotator wrote each scenario, both Claude agents launched from one Claude Code session on 2026-11-15, configured as `claude-opus-5-5`.

- **Config writers** saw the intent, the operation list with each tool's input schema, and the README's config reference, and wrote the variants. They never saw the tasks.
- **Task writers** saw the intent and the operation list, and wrote the tasks. They never saw the variants.

Each task writer checked its tasks by running every solution through the real gateway against the stand-in upstream, and `agent validate` repeats that check in CI.

## Upstream

No real API is called. A replay server stands in for every upstream, answering each request from the task's responses, else from the spec's own example, else from a fixed sample of the response schema, so a model may call any operation, not only the expected ones. It records every request for the grader.

## Validation

`tool-calling validate` calls no model, and proves two things.

- **Every task can pass.** Each task's solution runs under the baseline through the gateway and the replay upstream, and has to pass the grader with no failing tool call.
- **Every hand-written variant reaches what each task needs.** Every task is one the intent names, so a hand-written variant that hides an operation a task expects is a mistake in its config.

A generated variant may leave a task out of reach too, and that is the generator's miss to measure, never one to fix by hand. `validate` lists those tasks without failing, and `run` reports them as intent coverage beside the success rates.

## Known Biases

The data has five known biases, each worth weighing before citing a number.

- **Model-written data.** The configs and tasks were written by instances of one model, which may phrase tasks the way that model expects to read them.
- **Well-known APIs.** Models have likely seen GitHub's and Stripe's documentation in training, so a model may reach an operation from memory rather than through the tools. A scenario over an API models are unlikely to know is planned.
- **Stripe writes carry no body.** The gateway reads only JSON request bodies, and Stripe's are form-encoded, so no Stripe tool accepts body fields. Stripe's tasks are therefore writes that need only ids in the path, such as refunding a charge or cancelling a subscription.
- **Notion's broken parameter.** Notion's spec declares a header parameter with no name on one operation, which the gateway now skips with a warning.
- **Synthetic responses.** A response built from a schema is plausible but not real, so a task that depends on data a real API would return carries that data in its responses.

## Versioning

`version` in `benchmark.yaml` names the data. A change to a spec pin, a task, a variant, or the system prompt makes a new version. Results are cited with their version.
