# Agent Crew telemetry contract fixtures

These fixtures document the JSONL contract `agent_crew` issue #202
("feat: expose durable context identity and lifecycle telemetry") specifies,
so `quota_core.context_economics.agent_crew_adapter` can be built and tested
independently of that issue landing.

If/when #202 ships, real Agent Crew output should be parseable by the same
adapter without code changes, as long as it stays within this documented
shape. If Agent Crew's real contract diverges, update these fixtures and the
adapter together, and bump `SCHEMA_VERSION` in
`quota_core/context_economics/schema.py` only if the change is breaking.

## `attribution_sample.jsonl`

One durable attribution record per line, one line per task execution. Fields
match issue #202 section 2 and `RuntimeAttribution` in `schema.py`. Included
cases:

- a 4-task chain resuming the same `context_id` (`session_task_index`
  incrementing 0..4), including a failed task and its retry
  (`retry_of` pointing back at the failed `task_id`),
- a second, independent Codex context (`ctx-77aa`),
- a record with a newer `schema_version` and an undocumented field to verify
  forward-compatible fields are preserved rather than dropped,
- a minimal/older-shaped record missing most optional fields to verify
  tolerant parsing does not raise.

## `lifecycle_events_sample.jsonl`

Append-only lifecycle events for the same scenario: `context_created` ->
`task_started`/`task_completed` pairs, a `context_resumed`, a `task_failed`,
a `context_compacted` (used by the before/after compact analysis), a
`provider_fallback`, and a final line with an unrecognized future
`event_type` to verify the parser skips unknown event types instead of
raising.
