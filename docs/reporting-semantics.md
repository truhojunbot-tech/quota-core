# Reporting Semantics And Dashboard Policy

## Purpose

This document defines how normalized snapshot fields should be interpreted and rendered in reports, dashboards, and operational checks.

The snapshot schema defines the data shape. This document defines the product semantics: which fields are authoritative, which values are comparable, and how stale or partial data must be displayed.

## Data Source Classes

Quota reports combine several data classes that are intentionally not interchangeable.

| Class | Meaning | Examples | UI requirement |
| --- | --- | --- | --- |
| Quota window | Provider quota period with utilization and reset semantics | Claude 5-hour, Codex 7-day, Gemini current quota | Show utilization only when telemetry is not stale or clearly mark it delayed |
| Local history | Local observed usage without provider reset semantics | `local_all`, today, 7-day local history, month history | Label as local/history, not quota pressure |
| Runtime slice | Bot/runtime-only portion of a provider window | nested `runtime` totals | Display as share of provider window, separate from provider quota utilization |
| Session analytics | Transcript-derived Claude session analysis | `Claude Sessions` panel | Show coverage/reconciliation when compared with quota scanner totals |
| Usage timeline | Historical DB summary over a date range | 30-day Codex/Gemini/Claude trends | Label as historical usage and not the current quota window |

Dashboard code must not silently compare these classes as if they had the same denominator or freshness.

## Provider Window Totals

Provider adapters own raw provider payload interpretation. Dashboard code consumes normalized snapshots and must not repair raw provider payloads.

### Codex

Codex raw quota payloads can expose both `tokens_used` and `total_tokens`.

Rules:
- Prefer raw `total_tokens` when present because it is the observed window total and can include runtime-only exec tokens.
- Fall back to `tokens_used` only when `total_tokens` is missing or zero.
- Include projects that exist only in `runtime_by_project` in the main `by_project` breakdown so project shares sum against the observed total.
- Keep the nested `runtime.by_project` breakdown as the runtime-only view.

Example:
- `tokens_used = 80,915`
- `runtime_tokens_used = 246,942`
- `total_tokens = 327,857`

The dashboard window total is `327,857`, and a runtime-only project must appear in the main project list with `75.3%` share.

### Claude And Gemini

Claude and Gemini adapters should use the shared project/model aggregation helpers for raw project maps. Provider-specific adapters should not duplicate project normalization, model rollups, or share recomputation.

Shared ownership:
- `quota_core.adapters.projects.project_aggregates_from_raw()`
- `quota_core.adapters.projects.project_aggregates_with_runtime_extras()`
- `quota_core.adapters.projects.model_aggregates_from_projects()`

## Freshness And Display Policy

`cache_state` and `stale` affect what the dashboard may claim.

| State | Meaning | Utilization display | Reset display |
| --- | --- | --- | --- |
| `live` | Fresh enough to represent current provider state | Show percent normally | Show reset countdown normally |
| `cached` | Recent cached data, usable with caution | Show percent with cached badge/warning | Show countdown, but keep data-state warning visible |
| `stale` | Rate-limit/quota telemetry is too old or unreliable | Mark as delayed; do not show plain percent as authoritative | Show `reset 확인 지연`, not a normal countdown |
| `unknown` | Freshness cannot be established | Avoid strong operational claims | Avoid strong reset claims when possible |

Specific stale rules:
- If `stale` and `total_tokens > 0` and `utilization <= 0`, render utilization as `집계 지연`.
- If `stale` and utilization is nonzero, render it with a delay marker such as `1% · 지연`.
- If `stale`, render reset text as `reset 확인 지연` even when `resets_at` is in the future.
- Never render stale future `resets_at` as a normal `n시간 후 리셋` countdown.
- Never render expired stale `resets_at` as `리셋됨`; that suggests a confirmed provider reset.

## Runtime Display Policy

Runtime percentages and quota utilization are different metrics.

Runtime card left-side percentage:
- means `runtime.total_tokens / window.total_tokens`
- answers “how much of this provider window came from runtime-tagged bots?”

Runtime card right-side quota context:
- means provider quota utilization/freshness
- answers “how reliable is the provider quota pressure reading?”

Rules:
- If runtime has no tokens and no requests, show `runtime 없음`, not `0.0%`.
- If runtime share exists but the provider window is stale, show runtime share normally and show quota context as `quota 집계 지연`.
- Do not render a row that looks like `75.3% / 0.0% of quota`; it mixes runtime share with stale quota telemetry.

## History And Timeline Display Policy

Usage timelines are historical summaries, not current quota windows.

Rules:
- Label usage timelines with their date range.
- Mark usage timelines as historical, for example `30일 사용량 히스토리 · 현재 quota 창 아님`.
- Do not compare a 30-day history total directly with a current 5-hour or 7-day quota window total without explaining the denominator.
- Codex `codex_daily` and `codex_exec_log` history are not the same source semantics as Codex rate-limit telemetry.

## Claude Session Coverage Policy

Claude session analytics can have narrower coverage than the quota scanner.

Reasons include:
- local transcript roots only
- skipped oversized transcript files
- unparseable timestamps
- remote/session sources not present locally

Rules:
- If session analytics is compared with quota scanner totals, show a coverage/reconciliation card.
- Label project rankings as local/session-derived when they are not provider-wide totals.
- Prefer `Local Session Projects` over `Top Projects` when the list is based on local transcript coverage.
- Surface the outside/missing token amount when quota scanner totals exceed session analytics totals.

## Public/Private Boundary

Public `quota_core` owns reusable semantics and display policy for normalized snapshots.

Private ops may own:
- real provider polling
- private cache schedules
- operational alert thresholds
- generated dashboard artifacts
- environment-specific serving ports

Private ops must not become the authoritative implementation of provider-common aggregation or dashboard display policy.

## Dashboard Acceptance Checklist

A dashboard-facing change is not complete until these checks pass for the intended environment.

Required code checks:
- public test suite passes
- private overlay tests pass when private ops is in use
- provider-common logic is centralized instead of duplicated per adapter
- dashboard display labels come from shared formatter/helper APIs

Required artifact checks:
- private mirror is synced when private ops serves the dashboard
- snapshot artifact is regenerated
- HTML artifact is regenerated from that snapshot
- `quota-core verify-dashboard --snapshot <snapshot.json> --html <dashboard.html>` passes
- the actual served URL is checked, not only a temporary file or alternate server

Required visual checks:
- open the actual dashboard URL in a browser when possible
- inspect overview, runtime, provider quota, session analytics, timeline, and data-state sections
- verify key strings in the served HTML for stale/cached/live cases
- verify stale quota windows do not look like live quota countdowns
- verify history totals are labeled as historical and not current quota windows

For private deployments, the actual served dashboard URL and port must come from private ops configuration. Do not hard-code an environment-specific port in public core.