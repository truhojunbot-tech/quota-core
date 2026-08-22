# Context Economics

## Purpose

`quota_core.context_economics` answers a different question than
`quota_core.snapshot`. The snapshot schema answers *"how much quota is left
in this window?"*. This subpackage answers:

> How efficiently is this AI runtime using context, and what does the data
> suggest?

It is provider-neutral (Claude/Codex/Gemini) and runtime-neutral -- it works
from raw provider telemetry alone, and gets richer when an orchestrator
(such as Agent Crew) also exposes task/context attribution. It never imports
an orchestrator's code.

## Schema (`quota_core/context_economics/schema.py`)

`SCHEMA_VERSION` starts at `1`. Bump it only when a field is removed or its
meaning changes; adding an optional field is not a breaking change. All
`*_from_dict` parsers are tolerant: unknown top-level keys are preserved
under `extra` instead of dropped, and a missing/invalid enum-like field
(`context_policy`, unrecognized `event_type`) degrades to a safe default
(`"unknown"`) or is skipped, rather than raising.

- **`TokenComponents`** -- `fresh_input`, `output`, `cache_read`,
  `cache_creation`, `tool_tokens`, `provider_total`. Each field is `None`
  when the provider does not expose it -- never fabricated as `0`. Use
  `token_components_total()` for a best-effort total (provider-reported
  total, else sum of known parts; `None` if nothing is known).
- **`RuntimeAttribution`** -- one task execution's runtime/context
  attribution: `runtime`, `task_id`, `project`, `task_type`, `role`,
  `agent`, `provider`, `model`, `context_id`, `provider_session_id`,
  `context_policy` (`resume | compact | fresh | unknown`),
  `context_generation`, `session_task_index`, `previous_task_id`,
  `retry_of`, `fallback_of`, `started_at`, `completed_at`, `updated_at`,
  `outcome` (normalized to `success | failed | unknown | None`, see
  `normalize_outcome()`), `raw_outcome` (the runtime's original string,
  kept for diagnostics -- e.g. Agent Crew's real `"failed:dispatcher_timeout"`
  normalizes to `outcome="failed"` with `raw_outcome` preserved verbatim).
  Timestamps accept unix epoch (int/float) or an ISO-8601 string via
  `parse_flexible_timestamp()` -- Agent Crew's real lifecycle events use a
  `ts` ISO-8601 field, not a `timestamp` epoch int.
- **`ContextLifecycleEvent`** -- one append-only lifecycle event:
  `context_created`, `context_resumed`, `context_compacted`,
  `context_reset`, `context_recovered`, `provider_fallback`,
  `task_started`, `task_completed`, `task_failed`.
- **`TaskEconomicsRecord`** -- attribution joined with usage: adds `tokens`
  (`TokenComponents`), `attribution_confidence` (`high | medium | low`), and
  `attribution_notes` explaining how/why the join was made.

## Agent Crew adapter (`agent_crew_adapter.py`)

Reads Agent Crew's durable attribution JSONL and lifecycle-event JSONL (per
`agent_crew` issue #202) from a file path. It never imports `agent_crew` --
if Agent Crew is not installed at all, this adapter still works against the
documented contract in `tests/fixtures/agent_crew/` (see that directory's
`README.md`). A missing file, malformed line, or unrecognized event type
degrades gracefully (skipped/empty) instead of raising.

**Reconciliation**: real Agent Crew `attribution.jsonl` is snapshot/event-like
-- a task gets a dispatch row, zero or more progress-update rows, and a
terminal row, all sharing the same `task_id` (quota-core issue #58). Call
`reconcile_attribution_by_task(attributions)` after `read_attribution_jsonl()`
to collapse this to one row per `task_id` (the terminal row when one exists,
else the most recently updated in-progress row) before correlating or
running analytics -- reading every raw line as an independent task execution
will double/triple-count real data. See
`tests/fixtures/agent_crew/real_contract/README.md` for the golden fixtures
(sanitized, derived from real production output) this was validated against.

## Token components per provider (`token_components.py`)

`claude_token_components()`, `codex_token_components()`,
`gemini_token_components()` translate each provider's native usage shape.
Claude currently exposes a full fresh/output/cache-read/cache-creation
breakdown; Codex and Gemini typically expose only a subset -- the unexposed
components stay `None` rather than being estimated.

## Correlation (`correlate.py`)

`correlate_task_economics(attributions, usage_records)` joins attribution
with `ProviderUsageRecord`s using, in order:

1. **high** -- same `provider_session_id` *and* the usage record(s) overlap
   the task's `[started_at, completed_at]` window (±30s slop for clock/log
   skew).
2. **medium** -- same `provider_session_id`, but no task time window to
   confirm overlap, or no usage record actually overlapped it.
3. **low** -- no `provider_session_id` match; falls back to
   project + time-range heuristic. Also used when nothing matches at all
   (tokens stay unknown in that case).

Confidence and a human-readable note are always attached to the result --
callers must not treat a `low`-confidence join as equivalent to a `high`
one. Each usage record is attributed to **at most one** task: when a
record's window overlaps more than one candidate task in the same session,
it is assigned exclusively to whichever task's own window it is closest to,
so one unit of usage is never double-counted across several tasks.

## Known limitations / follow-ups

- **Not yet wired to quota-core's own live provider readers.** The schema,
  adapter, and correlation/analytics primitives are validated against real
  Agent Crew `attribution.jsonl`/`context_events.jsonl` output (issue #58 --
  see `tests/fixtures/agent_crew/real_contract/`), but there is still no glue
  that constructs `ProviderUsageRecord`s automatically from
  `quota_core.adapters.claude`/`codex`/`gemini`'s live session readers.
  Left for a follow-up once there's a concrete consumer that needs it.
- **`before_after_compact` assumes `context_id` continuity across a
  compact/reset** -- see that function's docstring.

## Analytics (`analytics.py`)

Exposes the individual components required by issue #56 (fresh input/task,
cache creation/task, cache read/task, failed+retry token waste, tokens per
successful task, tokens per outcome, context age vs token usage, context age
vs failure rate, resume/compact/fresh comparison). There is intentionally no
single composite `efficiency_score` -- compose one from these primitives if
you need it, so the underlying components stay visible.

## Compact before/after analysis (`compact_analysis.py`)

`before_after_compact(records, events, n=5)` compares up to `n` tasks
immediately before vs after each `context_compacted`/`context_reset` event
in the same `context_id`, reporting task count, average tokens, success
rate, and average duration for each side. This is the primitive
`quota-ops` issue #7's compact telemetry is meant to feed, to evaluate
whether proactive compaction actually helped rather than assuming it did.
