# Task token/cache telemetry (quota-core#78)

Agent Crew #317/#318 writes provider-neutral, task-level token and cache
telemetry into `task_attribution` on real task completion, read from the
provider's own session transcript. This document is quota-core's side of that
contract.

## ⛔ PRODUCTION SAMPLE PENDING

The wire shape has shipped and is confirmed against real data: the eight
columns exist in real `tasks.db` files, and the eight keys appear in real
`attribution.jsonl` rows. **But no organic row with a measured value has been
captured yet** — every real row observed so far carries `null` in all eight,
because the producer fills them only on a task completion that had a readable
provider transcript.

Nothing built on these fields may be presented as measured cache-locality or
resume-vs-fresh economics until a real post-deploy row with values is ingested
end-to-end. The fixtures under `tests/fixtures/agent_crew/task_token_telemetry/`
carry the real row *shape* with constructed *values*, and say so.

## The contract

Eight nullable fields, matching the producer's own `TaskTelemetry` dataclass:

| field | meaning |
|---|---|
| `uncached_input_tokens` | input the provider processed fresh |
| `cache_write_tokens` | input written into the provider's cache |
| `cache_read_tokens` | input read back from cache |
| `output_tokens` | output produced |
| `reasoning_tokens` | the reasoning **subset** of `output_tokens` |
| `context_window_tokens` | the provider's context window at completion |
| `stable_prefix_hash` | identity of the stable prompt prefix |
| `context_pack_hash` | identity of the Context Pack assembled |

`None` means the provider did not supply the fact. The producer states this
explicitly and updates only the columns it actually measured, so a `null` is
never an overwritten measurement. A measured `0` is a measurement and stays
distinguishable from `None` everywhere in this path.

## No universal total

`TaskTokenTelemetry` deliberately exposes **no total**, and
`task_token_telemetry_summary()` reports each component with its own
denominator and never crosses them. Two reasons:

1. `reasoning_tokens` is a **subset** of `output_tokens` where both exist, so
   adding them double-counts.
2. A provider exposing only some components would otherwise produce a "total"
   that silently means something different from another provider's.

Compose whatever total a specific pricing model needs at the point of use,
where the provider is known. The adapter contains no pricing assumption and no
provider-specific policy.

## Two different context-size numbers — not one to reconcile

Agent Crew emits two numbers that both look like "context size". They are
**different measurements of different things at different moments**, and an
earlier version of this change wrongly treated them as one quantity to
reconcile — which discarded both whenever they differed.

| number | what it is | when |
|---|---|---|
| `context_tokens` (quota-core#70) | the provider's real context window, read from the transcript | at **dispatch**, before this task ran |
| `task_telemetry.context_window_tokens` (#78) | cache-read + cache-write + uncached-input, each already summed across **every invocation** in the task span | at task **completion** |

The producer builds the second in `telemetry_claude._extract_span`. For a task
that made one invocation it happens to resemble a window; for a task that made
ten it is roughly ten windows' worth, because each invocation's cache read is
counted again. **A multi-invocation task disagreeing is the normal case, not a
contradiction.**

It is also *derived*: where all three components are present it equals their sum
exactly, so it carries no information they do not already carry. It is kept
because a provider may report it when a component is missing.

`context_window_observations(record)` therefore reports both side by side,
labelled, and:

- never chooses between them,
- never sums or averages them,
- never calls a difference a conflict,
- always reports `comparable: False`, so a caller reaching for a comparison
  finds the answer rather than inventing one.

It offers `invocation_amplification` (span input total ÷ dispatch window) only
when both are known and the window is non-zero. That is a ratio of two measured
numbers — a rough floor on how many times the context was re-sent — not an
inference about provider behaviour.

## Hashes are dimensions, not truth

`stable_prefix_hash` and `context_pack_hash` are carried so a caller can group
and compare by them — `task_telemetry_by_stable_prefix()` does exactly that. A
hash is an identity, not a claim: two dispatches sharing a `stable_prefix_hash`
were assembled from the same stable prefix, which is **not** by itself evidence
that the provider cached it. Nothing here infers cache behaviour from a hash;
the cache components are the measurement, and the hash is only the grouping.

Note `context_pack_hash` also reaches quota-core via `ContextPackAttribution`
(quota-core#62), from the Context Pack's own telemetry. Both name the same
pack; they are separate observation paths and should agree.

## Persistence

`task_economics_to_dict()` nests the components under `task_telemetry`,
matching the existing `tokens` group, and carries both hashes flat alongside
`test_scope_hash`. `attribution_to_dict()` flattens the same fields under the
producer's own column names instead, because that function mirrors a producer
row and must round-trip as one.

Every key is written even when `null`. A field that does not survive this
boundary does not exist to any report — quota-core PR #71 joined fields in
memory and dropped them here, so in-process tests passed while the written
artifact was wrong.
