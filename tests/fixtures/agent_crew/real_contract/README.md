# Synthetic producer-contract fixtures

These rows are wholly synthetic, but preserve producer field names and edge
cases: ISO-8601 `ts`, `completed` and colon-delimited failure outcomes,
multiple attribution snapshots per task, provider derived from `agent`, and a
retry linked to its failed predecessor. Identifiers and timestamps are invented.
The fixture intentionally omits `runtime`/`provider` and retains extra fields
such as `worktree_path`, `repo_url`, `created_at`, and `status` so the tolerant
adapter and task-level reconciliation remain exercised.
