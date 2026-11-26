---
description: How filters, resources, tool shaping and dynamic exposure keep tool definitions and results from crowding the model's context.
---
# Context Efficiency

This page explains how the gateway keeps tool definitions and tool results from crowding the model's context, through four levers that each trade some reach or effort for space.

## Context Cost

A client loads every tool's name, description and input schema into the model's context when it connects. For an API with hundreds of operations, such as GitHub's 1,000+, that fills the context window before the model does anything. Each result then adds its whole response body.

## Levers

Each lever removes a different part of what the model reads.

| Lever | Removes From Context | Trade-Off | Setup |
| --- | --- | --- | --- |
| Policy Filters | the definitions of operations the model never needs | a filtered operation cannot be reached at all, so a missing one fails the task | [Filtering Operations](../guides/exposure.md#filtering-operations) |
| Resources | read-only GETs from the tool list, read when the client or user decides | a client that ignores resources never reaches them | [Resource Exposure](../guides/exposure.md#resource-exposure) |
| Tool Shaping | hidden parameters from each schema, and envelope fields from each result | written per operation, and kept in step with the spec | [Tool Shaping](../guides/tool-shaping.md) |
| Dynamic Exposure | every definition until the model asks for it, leaving three meta-tools, which search, describe and call operations | up to three calls to reach an operation, and word search misses a request phrased unlike the spec | [Dynamic Exposure](../guides/exposure.md#dynamic-exposure) |

Tool shaping is the only lever that shrinks results. The other three shrink the definitions the client loads. [Dynamic Exposure](dynamic-exposure.md) gives the rules of the meta-tools and of search.

## Combining Levers

The levers combine, and the size of the API decides which to start from.

- **Small API**: a filter keeps the operations the model needs, and shaping trims what remains. Every tool stays loaded, so the model calls it directly.
- **Medium API**: as a rule of thumb, for roughly 200 to 1,000 operations, filter first, and turn on dynamic exposure when the filtered set is still large.
- **Large API**: dynamic exposure suits a spec with 1,000+ operations, or several APIs together. Shaping still applies to each operation it calls, and a filter still keeps operations out of search.
- **Read-Heavy Clients**: resources move reads off the tool list, for a client that surfaces resources. They cannot be combined with dynamic exposure.
- **Paraphrased Requests**: under dynamic exposure, semantic search is meant to narrow the gap that word search leaves, though no benchmark measures it yet. [Searching by Meaning](../guides/exposure.md#searching-by-meaning) covers its setup.

Operation scopes also leave out of the tool list what a client may not hold, as a side effect of access control.

## Measured Effect

The tool calling benchmark measures two of these levers. On Claude Haiku only, dynamic exposure completed every run on 8% of the input tokens of one tool per operation, and the generated configs, which keep only the operations their stated use needs, used over 85% fewer, as [Tool Calling on Claude Haiku](../evaluation.md#tool-calling-on-claude-haiku) shows with its limits.

## Out of Scope

The gateway shapes what it serves, and leaves the rest of the context to the client.

- **Client Loading**: whether a client loads every tool at once or defers them behind its own search is the client's choice. The benchmark's `pydantic-ai-tool-search` variant measures one such client.
- **Conversation History**: the gateway answers each call on its own, so how long past results stay in context is up to the client.

## Related

Three of the levers are set up in the exposure guide, and their fields are listed in the reference.

- **Guides**: [Exposure](../guides/exposure.md) for filters, resources and dynamic exposure
- **Reference**: [`policy`](../reference/configuration.md#policy) for the filter fields, [`exposure`](../reference/configuration.md#exposure) for the style and search fields and [Operation Fields](../reference/configuration.md#operation-fields) for the `tool` keys
