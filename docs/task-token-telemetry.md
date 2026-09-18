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

## Reconciling with the #70 window observation

The provider's context window is now measured in **two** places:

- quota-core#70 — the `provider_context_observed` / `provider_context_capped`
  lifecycle event, joined onto `TaskEconomicsRecord.context_tokens`;
- quota-core#78 — `context_window_tokens` on the task-attribution row.

They measure the same physical quantity, so summing them double-counts one
window. The two are kept in **separate fields** and reconciled explicitly by
`reconcile_context_window(record)`:

| both | result |
|---|---|
| only lifecycle | that value, `source="lifecycle"` |
| only task attribution | that value, `source="task_attribution"` |
| both, equal | that value, `source="agree"` — stronger evidence than either alone |
| both, different | **no value chosen**, `source="conflict"`, both raw values returned |
| neither | `None`, `source="unknown"` |

A disagreement means the two observation paths saw different states, most
plausibly at different moments in the dispatch. Choosing one would assert a
resolution the data does not support, so the function chooses neither and makes
the disagreement visible instead. It never sums, averages, or prefers the
larger.

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
