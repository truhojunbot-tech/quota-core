# Real Agent Crew contract fixtures (quota-core issue #58)

Unlike `tests/fixtures/agent_crew/attribution_sample.jsonl` and
`lifecycle_events_sample.jsonl` (synthetic, written before Agent Crew's real
telemetry landed -- see that directory's `README.md`), the two files here
are **sanitized golden fixtures derived from real `agent_crew` production
output** (project name, filesystem paths, and repo URL replaced with
generic placeholders; task/context IDs and timestamps otherwise faithful to
what was actually observed).

They exist to catch the exact contract mismatches confirmed in issue #58,
which the synthetic fixtures didn't have because they were authored to match
a *documented* contract that turned out to differ from what Agent Crew
actually ships:

1. **`context_events.jsonl` uses `ts`** (an ISO-8601 string, e.g.
   `"2026-08-21T23:56:19.497696"`), not the synthetic fixture's `timestamp`
   (unix epoch int).
2. **Terminal success is `outcome: "completed"`**, not `"success"`.
   `attribution.jsonl` also uses `"failed:<reason>"` (colon-delimited) for
   failures, e.g. `"failed:dispatcher_timeout"`.
3. **`attribution.jsonl` is snapshot/event-like, not one-row-per-task.**
   `test-a4f50eb4` here has three rows (dispatch, a progress update, and the
   terminal `"completed"` row) all sharing the same `task_id` -- a naive
   reader that treats every line as a distinct task execution would
   triple-count it. `reconcile_attribution_by_task()` collapses this to one
   row per `task_id`.
4. **Real attribution rows don't have a `runtime` or `provider` field** --
   only `agent` (`claude`/`codex`/`gemini`). They also carry fields the
   original schema didn't anticipate (`worktree_path`, `codex_logs_path`,
   `repo_url`, `git_branch`, `created_at`, `updated_at`, `status`) which the
   tolerant parser preserves under `extra` rather than dropping.

`impl-08da84a7` / `impl-retry-1a2b3c4d` is a constructed (not literally
observed) retry pair -- real production data available when this fixture
was built had no `retry_of`-linked example yet, but the shape matches the
real schema exactly (see issue #58: "at least one retry ... when
practical").
