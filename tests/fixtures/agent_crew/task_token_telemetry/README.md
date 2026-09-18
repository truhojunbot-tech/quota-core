# Task token/cache telemetry fixtures (quota-core issue #78)

Production-shaped fixtures for Agent Crew #317/#318's task-level token and
cache telemetry, which the producer writes into `task_attribution` on real
task completion by reading the provider's own session transcript.

## Provenance

The row shape here is **copied from a real post-deploy `attribution.jsonl`
row**, not invented: the full 44-key set, key order, and the empty-string vs
`null` conventions were taken from an actual `status="completed"` row observed
on this fleet after #317 shipped. Identifying values (task ids, project, repo
url, worktree path, session ids) were replaced with neutral ones; no field was
added, renamed, or dropped.

The eight fields under test are exactly the columns the producer's own
`TaskTelemetry` contract declares (`agent_crew/telemetry.py`):

    uncached_input_tokens  cache_write_tokens  cache_read_tokens
    output_tokens          reasoning_tokens    context_window_tokens
    stable_prefix_hash     context_pack_hash

## ⛔ PRODUCTION SAMPLE PENDING

**The measured VALUES in rows 1-3 are constructed, not captured.** The wire
shape has shipped -- the eight columns exist in real `tasks.db` files and the
eight keys appear in real `attribution.jsonl` rows -- but as of this change
every organic row carries `null` in all eight (row 4 is that shape, reproduced
verbatim). The producer fills them only on real task completion via a provider
transcript read, and no such completion has been captured yet.

Nothing computed from these fixtures may be presented as measured
cache-locality or resume-vs-fresh economics. Re-derive against a captured
organic row before drawing any conclusion. See `docs/task-token-telemetry.md`.

## Rows

1. `impl-measured-resume` -- fully measured resume dispatch: large cache read,
   small fresh input, reasoning split present.
2. `impl-measured-fresh` -- measured *fresh* dispatch that wrote the cache
   rather than reading it, with a genuine measured **zero** cache read and a
   measured zero reasoning count. These must never collapse into "unknown".
3. `review-partial-provider` -- a provider exposing no reasoning split and no
   window measurement: those two stay `null` while the others are measured.
   Partial observation is per-component, not all-or-nothing.
4. `test-all-null` -- post-#317 row whose task reported nothing: all eight keys
   **present and explicitly null**. This is the shape of every organic row
   observed so far.
5. `impl-pre-317-historical` -- pre-#317 row where the eight keys are **absent
   entirely** rather than null. Must parse identically to row 4.
