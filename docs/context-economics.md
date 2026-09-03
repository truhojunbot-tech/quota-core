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

## Failure classification (issue #60)

`RuntimeAttribution` and `TaskEconomicsRecord` both carry `failure_reason`
(the runtime-specific reason string, e.g. `"dispatcher_timeout"`),
`failure_category`, `retryable`, and `terminal_source`, alongside the
existing `outcome`/`raw_outcome`. `failure_category` is one of:

- `context_or_policy` -- evidence the failure was caused by stale/overlong
  context or a bad compact/resume decision. Not reachable from any reason
  string Agent Crew's real contract currently emits (see below).
- `provider_or_transport` -- upstream provider/transport transient errors
  (rate limits, capacity exhaustion, streaming backpressure).
- `runtime_or_dispatcher` -- the orchestrator's own operational failures
  (dispatcher timeout, no result ever submitted).
- `work_product_or_test` -- a reason that *positively* identifies a
  test/lint/assertion failure on the agent's own merits. A bare/generic
  nonzero exit code (`exit_1`, `exit_code_137`, ...) does **not** qualify --
  it is evidence-free (a crash, an OOM kill, and a genuine failing test all
  produce the same bare exit code) and classifies as `unknown` instead
  (round-1 review of issue #60 caught an earlier version of this that
  treated any `exit_*` reason as work-product evidence).
- `cancelled` -- the task was cancelled, not a failure on the merits.
- `unknown` -- a failed task with no recognizable reason, or only an
  evidence-free one like a bare exit code. Never fabricated into a more
  specific category.

`classify_failure_category(outcome, raw_outcome)` derives this from a raw
`"failed:<reason>"`-style outcome; `infer_retryable(reason)` derives a
best-effort `True`/`False`/`None` from the *specific reason tag* (not the
coarser category), mirroring agent_crew's own dispatcher
(`_detect_transient_error_in_log`) verbatim: its explicitly-retryable tag
group (`claude_429`, `claude_throttle`, `gemini_capacity`,
`gemini_resource_exhausted`, `codex_capacity`, `agy_timeout`,
`agy_subscriber_lag`) and its explicitly-non-retryable group
(`gemini_quota_exhausted`, `gemini_ineligible_tier`, `agy_quota_exhausted`).
Deriving from the coarser `failure_category` instead (an earlier version of
this) got the single most common real failure reason wrong:
`agy_quota_exhausted` is 321 of 413 (78%) of every failure reason observed
locally, and the dispatcher's own comment calls it "clear reason; no point
in immediate retry" even though it shares a `provider_or_transport` category
with genuinely-retryable tags. A reason containing `"max_retries"` (the
dispatcher's own retry loop already gave up) is always non-retryable
regardless of which tag it otherwise matches.
`attribution_from_dict` populates all of this automatically from `outcome`
when a source dict does not supply the fields explicitly; explicit values
-- including an explicit `null` -- always win over the derived ones (a
present-but-invalid value, e.g. an unrecognized `failure_category` string,
is treated the same as absent and still gets derived). Older telemetry with
only a bare `outcome="failed"` (no reason) still parses -- it classifies as
`unknown`, not dropped.

This exists so context-age and resume/compact/fresh comparisons don't treat
a provider outage or dispatcher bug as evidence about context handling --
see the real `agent_crew#205`/`#206` AGY "subscriber fell behind" incident
in `tests/fixtures/agent_crew/agy_incident/README.md` and
`tests/test_failure_classification.py`.

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

**`tasks.db` `error_info`/`status` enrichment** (round-1 and round-2 review
of issue #60): `attribution.jsonl`'s own failure reason is, in real local
data, almost always an uninformative bare `exit_1`/`dispatcher_timeout` --
worse, the majority of real tasks that Agent Crew's own dispatcher marked
`status="failed"` (with a specific `error_info` reason) never got a
terminal row written to `attribution.jsonl` at all, so their `outcome` stays
`None` ("still in progress") and they are invisible to
`attribution.jsonl`-only classification. `tasks.db`'s `error_info` column
(`{"reason": "<tag>"}`, keyed by the same `task_id`) and its `status` column
are the richer source: `read_task_error_reasons(db_path)` reads
`error_info` standalone, and `enrich_with_task_error_reasons(attributions,
db_path)` joins both onto an existing attribution list, doing three things:
(1) backfilling `outcome="failed"` (plus derived
`failure_reason`/`failure_category`/`retryable`) for a task `tasks.db`
marks `status="failed"` but `attribution.jsonl` never terminated -- the
highest-yield case in real local data (321 of 413 real observed `tasks.db`
error rows are exactly this shape); (2) the symmetric case (round-2 review):
backfilling `outcome="success"` for a task `tasks.db` marks
`status="completed"` but `attribution.jsonl` never terminated either --
without this, only failures could ever be backfilled, which skewed the
reported failure rate on real local data from an ~11% jsonl-only
understatement to a ~75% overstatement (worse than the original bug); and
(3) preferring the richer `tasks.db` reason over an existing bare
`attribution.jsonl` reason when a task already has `outcome="failed"` in
both. `attribution.jsonl`'s own explicit non-`None` outcome (`success`,
`unknown`, or an already-set `failed`) is never overridden by this
function on its own.

`read_attribution_jsonl_with_task_errors(attribution_path, tasks_db_path)`
is the one-call convenience wrapper -- `read_attribution_jsonl`, then
`reconcile_attribution_by_task`, then `enrich_with_task_error_reasons`, in
that order. **Reconciliation runs before enrichment, not after** (round-3
review): reconciliation's own tiebreak (a task's terminal row always beats
an in-progress row; latest timestamp only breaks ties within the same tier)
only guarantees "`attribution.jsonl`'s own terminal call always wins" while
"terminal" still means "`attribution.jsonl` itself reported an outcome". If
enrichment ran first, every one of a task's rows -- including its
still-in-progress ones -- could pick up an outcome from `tasks.db` before
reconciliation ever saw them, collapsing the tiebreak to "latest timestamp
wins" and letting a `tasks.db`-backfilled row outrank a real, explicit
terminal row from `attribution.jsonl` whenever it happened to carry a later
timestamp. Reconciling the raw stream first means the tiebreak only ever
treats an `attribution.jsonl`-native terminal outcome as terminal; the
`tasks.db` backfill is then applied per task, strictly after, and only to a
task whose winning row still has `outcome=None` -- so it can add
information but can never outrank an explicit terminal call regardless of
timestamps. All of this enrichment is optional, best-effort -- a
missing/unreadable `tasks.db` degrades silently back to
`attribution.jsonl`-only behavior, never a hard dependency.

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

`stratified_failure_rates(records)` (issue #60) breaks a "not succeeded"
rate down by `failure_category`: `provider_or_runtime_operational`,
`work_product_or_test`, `policy_relevant`, `cancelled`, `unknown` -- each
with its own count and rate, alongside the raw `raw_failure_rate`.
`work_product_or_test` is its own bucket, not folded into `policy_relevant`
(round-1 review of issue #60: folding it in meant `policy_relevant` silently
included ordinary test/lint failures unrelated to context/compact policy --
`policy_relevant` now contains only `context_or_policy`-caused failures, the
signal context-age/compact comparisons should actually read).
`context_age_vs_failure_rate` and `compare_context_policies` both merge this
breakdown into their existing output (backward compatible -- the
pre-existing `failure_rate`/`count`/`success_rate` keys are unchanged) and
add a `"warning"` key (via `unknown_cause_warning()`) when a large share of
the failures observed in a group have an unrecognized cause --
`UNKNOWN_FAILURE_CAUSE_WARNING` when *all* of them are unknown,
`PARTIAL_UNKNOWN_FAILURE_CAUSE_WARNING` when at least
`PARTIAL_UNKNOWN_FAILURE_CAUSE_RATE_THRESHOLD` (50%) are (added in round-1
review -- the original warning was all-or-nothing, so a group that was e.g.
90% unknown reported a confident-looking `policy_relevant_failure_rate` with
no warning at all). `compact_analysis.before_after_compact`'s before/after
windows expose the same breakdown for the same reason.

## Context Pack economics (`context_pack_analytics.py`, quota-core#62)

Consumes Agent Crew #239's real Context Pack producer contract: a
`"context_pack_built"` `ContextLifecycleEvent` emitted alongside the other
lifecycle events in the same `context_events.jsonl` stream, carrying
`ContextPack.telemetry()`'s fields (`context_pack_id`/`hash`/
`schema_version`, `mode`, `role`, `candidate_count`, `selected_count`,
`total_tokens`, `tokens_by_category` -- `mandatory`/`authoritative`/
`episodic`/`procedural` -- `stale_count`, `conflict_count`, `latency_ms`,
`degraded`, `degraded_reason`, `budget`) plus dispatch-time identifiers
(`task_id`, `project`, `agent`, `context_id`, `context_generation`).

`schema.ContextPackAttribution` is the normalized record; `schema.
context_pack_attribution_from_event(event)` extracts one from a lifecycle
event (`None` for any other event type), and `agent_crew_adapter.
context_pack_attributions_from_events(events)` filters a full lifecycle
stream down to just these. As with the rest of this package, quota-core
does not import `agent_crew` -- only the documented JSONL wire shape.

Three read-only analysis functions:

- `context_composition(attributions)` -- aggregate token composition
  (`mandatory`/`authoritative`/`episodic`/`procedural` totals and shares),
  mean pack size.
- `context_pack_efficiency(attributions)` -- candidate→selected compression
  ratio, latency p50/p95, degraded/stale/conflict rates.
- `retrieval_mode_comparison(attributions)` -- the above two, broken down
  per `mode` (e.g. `lexical`/`semantic`/`hybrid`/`unknown`), each carrying
  its own `sample_size` so a comparison never hides how thin one side is.

Every rate/mean is computed over only the attributions that actually report
the relevant field, with its own `*_known_count` denominator reported
alongside it -- a field no producer version has started emitting yet stays
absent from the aggregate, never a fabricated zero that would silently drag
a rate down.

### Known limitations

- **No real production telemetry exists yet, for two independent reasons.**
  `AGENT_CREW_CONTEXT_PACK` (the feature's own opt-in flag) is not set
  anywhere on the fleet this was developed against, AND the real producer
  call site (`server.py`'s dispatcher) currently raises `TypeError` on
  every attempt to record this event (`role` is passed both explicitly and
  via `**ContextPack.telemetry()`, which also contains a `role` key) --
  reported as `agent_crew#258`, not fixed here since it is that repo's
  code. Either gap alone would already mean zero real events; both apply
  today. This module's fixtures are therefore derived directly from
  reading `agent_crew`'s real source (`context_pack.py`'s `telemetry()`
  method and `server.py`'s call site) rather than from a real captured
  golden fixture like issue #58's `tests/fixtures/agent_crew/
  real_contract/` -- re-derive against real data once both gaps close.
- **"Context Pack value indicators" and "compact/retrieval interaction"
  are not implemented here.** Both need a real paired sample (tasks run
  with vs without a pack, or compact events correlated with pack telemetry
  for the same task) to be meaningful; building them against fixtures
  today would only prove the arithmetic works, not that the comparison
  means anything. Left for a follow-up issue once real paired data exists.
- **No cross-check against `TaskEconomicsRecord`'s `failure_category`.**
  Distinguishing a context-policy-relevant retrieval failure from an
  unrelated provider/runtime failure (issue #62's "stratify provider/
  runtime incidents from context-policy-relevant failures" acceptance
  criterion) requires joining Context Pack attributions with the existing
  failure-classification pipeline by `task_id` -- not done in this module,
  which only aggregates the pack telemetry itself.

## Compact before/after analysis (`compact_analysis.py`)

`before_after_compact(records, events, n=5)` compares up to `n` tasks
immediately before vs after each `context_compacted`/`context_reset` event
in the same `context_id`, reporting task count, average tokens, success
rate, and average duration for each side. This is the primitive
`quota-ops` issue #7's compact telemetry is meant to feed, to evaluate
whether proactive compaction actually helped rather than assuming it did.
