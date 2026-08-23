# Changelog

## 0.1.16 - 2026-08-23

Adds failure cause/classification to `quota_core.context_economics` so raw
task failure is no longer treated as automatic context-policy evidence
(issue #60). Motivated by the real `agent_crew#205`/`#206` incident: 31
tester failures over ~21 hours caused by AGY's transient "subscriber fell
behind updates" streaming/backpressure error that agent_crew's dispatcher
had not yet classified as retriable.

- `RuntimeAttribution`/`TaskEconomicsRecord` gain `failure_reason`,
  `failure_category`, `retryable`, and `terminal_source`, alongside the
  existing `outcome`/`raw_outcome`.
- New `classify_failure_category()` maps a raw `"failed:<reason>"` outcome
  into a small public category set (`context_or_policy`,
  `provider_or_transport`, `runtime_or_dispatcher`, `work_product_or_test`,
  `cancelled`, `unknown`), grounded in reason strings actually observed in
  `~/.agent_crew/*/tasks.db` `error_info` / `attribution.jsonl` `outcome`
  (`dispatcher_timeout`, `exit_1`, `agy_quota_exhausted`,
  `no_result_submitted`) and agent_crew's own dispatcher transient-error
  tags. Never fabricates a specific category from insufficient evidence --
  defaults to `unknown`.
- New `infer_retryable()` mirrors which categories agent_crew's dispatcher
  currently auto-retries (only `provider_or_transport`).
- `attribution_from_dict` derives all of the above from `outcome`
  automatically; an explicit value in the source dict always wins.
- New `stratified_failure_rates()`, merged into `context_age_vs_failure_rate`,
  `compare_context_policies`, and `compact_analysis.before_after_compact`'s
  before/after windows: exposes `provider_or_runtime_operational`,
  `policy_relevant`, `cancelled`, and `unknown` failure counts/rates
  alongside the existing raw rate, plus a `"warning"` key when every
  observed failure's cause is unrecognized. Existing keys
  (`failure_rate`/`count`/`success_rate`) are unchanged.
- Regression fixture `tests/fixtures/agent_crew/agy_incident/` models the
  AGY incident and proves it classifies as `provider_or_transport` (not
  `context_or_policy`) and is excluded from `policy_relevant_failure_rate`.
- Backward compatible: older telemetry with only a bare `outcome="failed"`
  (no reason) still parses and classifies as `unknown`, not dropped.
- Full suite: 139/139 passing.

## 0.1.15 - 2026-08-22

Fixes `quota_core.context_economics`'s Agent Crew adapter to conform to the
real `agent_crew` telemetry contract instead of the pre-#202 synthetic
fixtures it was built against (issue #58, P0):

- Lifecycle events use Agent Crew's real `ts` (ISO-8601) field, not the
  synthetic `timestamp` (epoch int) -- both are now accepted via
  `parse_flexible_timestamp()`.
- Terminal success is Agent Crew's real `outcome="completed"` (and
  colon-delimited failures like `"failed:dispatcher_timeout"`), normalized
  via the new `normalize_outcome()` into `success | failed | unknown`, with
  the original string preserved as `raw_outcome` for diagnostics.
- New `reconcile_attribution_by_task()`: real `attribution.jsonl` is
  snapshot/event-like (dispatch + progress-update + terminal rows sharing
  one `task_id`), not one row per task -- this collapses it to the terminal
  (or latest) row per task instead of triple-counting real data.
- `provider` is derived from `agent` (claude/codex/gemini) when the real
  contract's row doesn't include a separate `provider` field, instead of
  staying incorrectly unknown.
- Added `tests/fixtures/agent_crew/real_contract/`: sanitized golden
  fixtures derived from real production `agent_crew` output, plus an
  end-to-end contract test (fixture -> adapter -> reconciliation ->
  correlation -> `TaskEconomicsRecord`) and regression tests for each of the
  four confirmed mismatches.
- Existing public snapshot/session/context-economics APIs remain backward
  compatible (full suite: 105/105 passing).

## 0.1.14 - 2026-08-21

Adds `quota_core.context_economics`: a provider-neutral context-economics /
session-efficiency measurement layer (issue #56).

- New public, versioned schema (`TokenComponents`, `RuntimeAttribution`,
  `ContextLifecycleEvent`, `TaskEconomicsRecord`) that keeps fresh input,
  output, cache-read, and cache-creation tokens as separate economic
  categories instead of collapsing them into one total.
- Tolerant Agent Crew attribution/lifecycle-event JSONL adapter
  (`agent_crew_adapter.py`) built against a documented fixture contract
  (`tests/fixtures/agent_crew/`) so this does not block on Agent Crew
  issue #202 landing, and never imports `agent_crew`.
- Per-provider token component extraction for Claude/Codex/Gemini
  (`token_components.py`), preserving partial observability instead of
  fabricating a breakdown for providers that only report a total.
- Attribution/usage correlation with explicit confidence
  (`high`/`medium`/`low`) and human-readable notes (`correlate.py`).
- Context-efficiency analytics and resume/compact/fresh comparison
  (`analytics.py`), and before-vs-after compact/reset analysis primitives
  (`compact_analysis.py`).
- `docs/context-economics.md` documents the contract and versioning
  expectations.
- Fully additive: existing `quota_core.snapshot`/dashboard/session public
  APIs are unchanged, and the new subpackage is not imported eagerly from
  `quota_core/__init__.py`.

## 0.1.13 - 2026-04-29

Original dashboard structure restore.

- Restores the dark provider-column `전체 현황` quota board from the original dashboard.
- Shows each provider's quota windows with usage bars, reset timing, and Korean pace labels.
- Restores the dark `자동 런타임 LLM 사용량` board as a separate runtime-only section.
- Adds browser auto-refresh so regenerated dashboards update without manual reload.
- Rebuilds the public dashboard around the original GUI order: overview, runtime, provider quota panels, Gemini usage, details, and data state.

## 0.1.12 - 2026-04-29

Overall status restore.

- Restores the dashboard-wide Overall status block above the legacy quota report.
- Keeps provider quota details in the report section below so the summary and report have separate roles.

## 0.1.11 - 2026-04-29

Legacy dashboard layout restore.

- Restores the first screen to the original report-style provider sections: Claude Max, Codex, and Gemini quota lines with app lists.
- Keeps runtime usage in a separate report block instead of scattering it through provider windows.

## 0.1.10 - 2026-04-29

Gemini operations report correction.

- Keeps Gemini local 7-day history out of the first-screen Operations report quota pair.
- Leaves Gemini 7-day local usage available in provider details where it is labeled as local history.

## 0.1.9 - 2026-04-29

Visible operations report layout.

- Added a first-screen Operations report section that mirrors the original provider-first report structure.
- Shows each provider's short/current quota and 7-day windows with app lists in the overview, not only in lower detail sections.

## 0.1.8 - 2026-04-29

Dashboard structure refactor.

- Added a dashboard view model that maps snapshots into the original operations report structure before rendering.
- Moved provider window pairing, primary-window selection, reset ordering, and data-state summaries out of HTML components.
- Added regression coverage for Claude/Codex-style 5-hour + 7-day and Gemini-style current quota + 7-day grouping.

## 0.1.7 - 2026-04-29

Gemini quota window alignment.

- Pairs `current_quota` with `seven_day` in provider cards and detail sections when a provider has no 5-hour window.
- Keeps per-window app lists side by side for Gemini-style quota snapshots.

## 0.1.6 - 2026-04-29

Dashboard provider-detail split view.

- Added a side-by-side 5-hour and 7-day layout in provider detail sections.
- Lists each quota window's top apps inside its own detail column.

## 0.1.5 - 2026-04-29

Dashboard provider-card progress bars.

- Added separate 5-hour and 7-day progress bars in provider summary cards.
- Removed the single combined provider-card bar when quota windows are available.

## 0.1.4 - 2026-04-29

Dashboard short-window visibility fix.

- Restored 5-hour quota visibility in provider summary cards instead of showing only the highest-pressure window.
- Provider cards now show both 5-hour and 7-day rows when both quota windows are available.

## 0.1.3 - 2026-04-29

Dashboard information architecture update.

- Added a command-center summary for highest pressure, next reset, and live/cached data state.
- Added a quota matrix that compares provider windows by utilization, tokens, reset, pace, top project, and cache state.
- Added a reset schedule and per-window context for range, sampled time, top project, and top model.
- Added regression coverage for the operational quota context sections.

## 0.1.2 - 2026-04-29

Operations dashboard correction.

- Separated local-history snapshots from live quota windows in the dashboard UI.
- Added attention and operations briefing sections for quota pressure, warnings, and project concentration.
- Made provider summary cards choose the highest-pressure quota window instead of always preferring the shortest window.
- Added regression coverage so local-only scans do not render as misleading `0.0%` quota utilization.

## 0.1.1 - 2026-04-29

Dashboard usability update.

- Reworked the dashboard into an operations-focused layout with provider summary cards.
- Added compact token/request metrics, share bars, and top-project truncation.
- Shortened local Claude project labels for easier scanning while preserving full names in tooltips.
- Added responsive dashboard layout rules for smaller screens.

## 0.1.0 - 2026-04-29

Initial public release preparation.

- Added public `quota-core` package metadata and CLI entrypoint.
- Added config-driven local scanners for Claude, Codex, and Gemini.
- Added normalized snapshot schema and dashboard HTML rendering.
- Added runtime tagging helpers for private overlays that pass explicit config.
- Added public/private boundary documentation and regression tests.
- Hardened local scanner warnings and bounded local file reads.