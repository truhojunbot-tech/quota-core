# Tester treatment / lock-wait fixtures (quota-core issue #68)

Production-shaped fixtures for Agent Crew #278/#279's tester-treatment and
scheduler-lock-wait contract, built from the real column/field names and
call order in `agent_crew/server.py` and `agent_crew/queue.py` (not
invented): `record_test_economics()` writes `effective_test_scope`,
`test_scope_source`, `test_scope_hash`, `lock_wait_seconds`,
`lock_defer_count` into the same `task_attribution` row `attribution.jsonl`
mirrors, and `record_context_event()` emits `test_scope_resolved` /
`test_stage_deferred` into the same `context_events.jsonl` stream as every
other lifecycle event (project/path/repo values are generic placeholders;
task IDs, field names, and sequencing match the real contract).

## `attribution.jsonl`

Four task_ids, five rows:

- `impl-9c1a2b3d` -- a non-test (`implementer`) task. `record_test_economics`
  is only ever called for `task_type == "test"`, so all five new fields are
  explicit `null` here, not absent -- this is what every non-test row looks
  like post-#279, not a degraded case.
- `test-targeted-4e5f6a7b` -- a test task dispatched with no lock
  contention: `effective_test_scope="targeted"`, `lock_wait_seconds=0.0`,
  `lock_defer_count=0`. Two rows (dispatch + terminal `"completed"`), same
  shape #58's `real_contract` fixture already established for the
  non-test-economics fields.
- `test-fullsuite-8c9d0e1f` -- a test task that was deferred once (another
  test stage held its worktree's lock) before dispatch:
  `effective_test_scope="full_suite"`, `lock_wait_seconds=275.0` (the
  measured wait), `lock_defer_count=1`. There is no separate attribution row
  for the deferred attempt itself -- per #279, a deferred attempt writes none
  at all; the wait is folded into the one row the eventually-dispatched
  attempt produces. Its `context_id` matches `impl-9c1a2b3d`'s (same
  worktree/context, later in the same resumed session), demonstrating the
  two are joinable by `context_id` as well as `task_id`.
- `test-historical-2a3b4c5d` -- a pre-#279 `attribution.jsonl` line (this
  repo's existing `real_contract` fixture row, unchanged): the five new keys
  are **absent from the JSON entirely**, not `null`. This is the real
  shape of a line written to the append-only file before a dispatcher
  deploy added the columns -- distinct from the `null` case above, and both
  must parse to `RuntimeAttribution(effective_test_scope=None, ...)`
  identically.

## `context_events.jsonl`

Covers both new lifecycle event types:

- `test_scope_resolved` for both dispatched test tasks, carrying the same
  five-field payload as their attribution row (the producer writes both from
  the same resolved `_scope` value -- see `server.py`'s dispatch path).
- `test_stage_deferred` for `test-fullsuite-8c9d0e1f`, timestamped *before*
  its `task_started`/`test_scope_resolved` pair and sharing the same
  `task_id` -- the requeue-and-retry Agent Crew uses for a lock-contended
  test stage keeps the task_id stable across the deferral, which is what
  lets a consumer join a deferral history to its eventual dispatch (or
  reconcile one across a dispatcher restart) without a separate task record.
