# Normalized Snapshot Schema

## Purpose

This schema is the public API between provider adapters, dashboard components, alerts, reports, and users of `quota_core`.

Provider-specific raw data must be normalized into this shape before it reaches dashboard or reporting code.

For data ingestion flow, see [data-ingestion-architecture.md](data-ingestion-architecture.md). For field interpretation and UI/reporting policy, see [reporting-semantics.md](reporting-semantics.md). This schema defines shape; the reporting semantics document defines freshness, history, runtime, and coverage meaning.

## Top-Level Shape

```json
{
  "source": "claude",
  "sampled_at": 1770000000,
  "windows": {},
  "errors": [],
  "warnings": []
}
```

Fields:
- `source`: provider name, for example `claude`, `codex`, `gemini`, or `ollama`
- `sampled_at`: unix timestamp when the snapshot was created
- `windows`: named quota/session windows
- `errors`: fatal or provider-level errors surfaced to UI/reporting
- `warnings`: non-fatal provider warnings surfaced to UI/reporting

## Window Shape

```json
{
  "window_start": 1769982000,
  "window_end": 1770000000,
  "resets_at": 1770000000,
  "utilization": 0.42,
  "total_tokens": 123456,
  "requests": 72,
  "by_project": {},
  "by_model": {},
  "runtime": {},
  "cache_state": "live",
  "stale": false
}
```

Fields:
- `window_start`: unix timestamp, nullable when unknown
- `window_end`: unix timestamp, nullable when unknown
- `resets_at`: unix timestamp, nullable when provider has no reset concept
- `utilization`: provider quota utilization from `0.0` to `1.0` when known
- `total_tokens`: total tokens observed in this window
- `requests`: request count observed in this window
- `by_project`: project aggregate map
- `by_model`: model aggregate map
- `runtime`: runtime-only aggregate
- `cache_state`: `live`, `cached`, `stale`, or `unknown`
- `stale`: boolean shortcut for stale UI/reporting states

Freshness matters. A stale window can still carry tokens, utilization, and reset timestamps, but dashboard code must render those values according to [reporting-semantics.md](reporting-semantics.md) instead of treating them as live provider state.

When any quota window is `cached`, `stale`, or `unknown`, the snapshot must also preserve why the provider is not live. Use `warnings`, `errors`, a history key ending in `_error`, or structured `history.quota_telemetry` with fields such as `source_event_ts`, `source_age_seconds`, `last_cli_activity_ts`, and `rate_limit_source`.

## Aggregate Shape

```json
{
  "total_tokens": 1234,
  "requests": 12,
  "share_pct": 73.0,
  "models": {
    "claude-sonnet-4-6": 900
  },
  "model_requests": {
    "claude-sonnet-4-6": 10
  }
}
```

## Runtime Shape

```json
{
  "total_tokens": 1000,
  "requests": 4,
  "by_project": {},
  "by_model": {}
}
```

Runtime is nested under each window so dashboard and reporting can compare provider-wide usage with bot/runtime-only usage without re-scanning raw logs.

Runtime percentages are a share of provider-window tokens, not provider quota utilization. Dashboards must label those concepts separately.

## Local Scanner Windows

Config-driven public local scanners emit a `local_all` window when they can read local provider history but cannot infer provider reset windows or live quota utilization. In that case:
- `window_start` is `null`
- `window_end` is the snapshot timestamp
- `resets_at` is `null`
- `utilization` is `0.0`
- `cache_state` is `live`

Provider-specific live quota windows can be added later without changing the top-level schema.

## GUI Rule

Dashboard components may render only this normalized schema. They must not inspect raw provider logs, SQLite databases, credentials, private bot registries, or internal paths.

## Validation

The public Python validation entrypoint is:

```python
from quota_core.snapshot import validate_snapshot_dict

errors = validate_snapshot_dict(snapshot)
```

An empty `errors` tuple means the snapshot matches the public shape.
