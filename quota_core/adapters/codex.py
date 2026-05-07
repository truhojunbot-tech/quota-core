"""Normalize Codex quota payloads into the public snapshot schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from quota_core.adapters.projects import finalize_project_aggregates, merge_project_breakdown, normalize_project_name
from quota_core.config import ProviderConfig
from quota_core.snapshot import (
    AggregateBreakdown,
    NormalizedSnapshot,
    RuntimeBreakdown,
    SnapshotWindow,
)

WINDOW_SECONDS = {
    "five_hour": 5 * 3600,
    "seven_day": 7 * 24 * 3600,
}


def normalize_codex_quota(payload: dict[str, Any]) -> NormalizedSnapshot:
    """Convert the existing Codex monitor payload into a normalized snapshot."""

    sampled_at = int(payload.get("fetched_at") or 0)
    error = payload.get("error")
    if error:
        return NormalizedSnapshot(source="codex", sampled_at=sampled_at, errors=(str(error),))

    windows: dict[str, SnapshotWindow] = {}
    cache_state = _cache_state(payload)
    for window_name, window_seconds in WINDOW_SECONDS.items():
        raw_window = payload.get(window_name)
        if not isinstance(raw_window, dict):
            continue
        resets_at = _optional_int(raw_window.get("resets_at"))
        window_start = resets_at - window_seconds if resets_at is not None else None
        window_end = min(sampled_at, resets_at) if sampled_at and resets_at is not None else sampled_at or resets_at
        total_tokens = int(raw_window.get("tokens_used") or 0)
        runtime_total = int(raw_window.get("runtime_tokens_used") or 0)
        by_project = _project_aggregates(raw_window.get("by_project", {}), total_tokens)
        runtime_by_project = _project_aggregates(raw_window.get("runtime_by_project", {}), runtime_total)
        windows[window_name] = SnapshotWindow(
            window_start=window_start,
            window_end=window_end,
            resets_at=resets_at,
            utilization=float(raw_window.get("utilization") or 0.0),
            total_tokens=total_tokens,
            requests=sum(item.requests for item in by_project.values()),
            by_project=by_project,
            by_model=_model_aggregates(by_project, total_tokens),
            runtime=RuntimeBreakdown(
                total_tokens=runtime_total,
                requests=sum(item.requests for item in runtime_by_project.values()),
                by_project=runtime_by_project,
                by_model=_model_aggregates(runtime_by_project, runtime_total),
            ),
            cache_state=cache_state,
            stale=cache_state == "stale",
        )
    warnings = ()
    if cache_state == "stale":
        warnings = ("codex rate-limit telemetry is stale",)
    elif cache_state == "cached":
        warnings = ("codex rate-limit telemetry is cached",)
    return NormalizedSnapshot(source="codex", sampled_at=sampled_at, windows=windows, warnings=warnings)

def _cache_state(payload: dict[str, Any]) -> str:
    state = payload.get("cache_state")
    if state in {"live", "cached", "stale", "unknown"}:
        return str(state)
    return "stale" if payload.get("stale") else "live"


def scan_codex_local(config: ProviderConfig, sampled_at: int) -> NormalizedSnapshot:
    """Scan Codex local SQLite usage from public config."""

    state_db_raw = config.paths.get("state_db")
    if not state_db_raw:
        return NormalizedSnapshot(
            source="codex",
            sampled_at=sampled_at,
            warnings=("codex state_db is not configured",),
        )

    state_db = Path(state_db_raw).expanduser()
    if not state_db.exists():
        return NormalizedSnapshot(
            source="codex",
            sampled_at=sampled_at,
            warnings=("codex state_db does not exist",),
        )

    try:
        rows = _thread_rows(state_db)
    except sqlite3.Error as exc:
        return NormalizedSnapshot(
            source="codex",
            sampled_at=sampled_at,
            errors=(f"codex state_db scan failed: {exc}",),
        )

    total_tokens = 0
    requests = 0
    projects: dict[str, AggregateBreakdown] = {}
    for cwd, model, tokens_used in rows:
        tokens = _coerce_int(tokens_used)
        if tokens <= 0:
            continue
        project_name = normalize_project_name(str(cwd or "unknown"))
        model_name = str(model or "unknown").split("/")[-1]
        total_tokens += tokens
        requests += 1
        current = projects.get(project_name, AggregateBreakdown())
        models = dict(current.models)
        model_requests = dict(current.model_requests)
        models[model_name] = models.get(model_name, 0) + tokens
        model_requests[model_name] = model_requests.get(model_name, 0) + 1
        projects[project_name] = AggregateBreakdown(
            total_tokens=current.total_tokens + tokens,
            requests=current.requests + 1,
            models=models,
            model_requests=model_requests,
        )

    by_project = finalize_project_aggregates(projects, total_tokens)
    window = SnapshotWindow(
        window_start=None,
        window_end=sampled_at,
        resets_at=None,
        utilization=0.0,
        total_tokens=total_tokens,
        requests=requests,
        by_project=by_project,
        by_model=_model_aggregates(by_project, total_tokens),
        cache_state="live",
        stale=False,
    )
    return NormalizedSnapshot(source="codex", sampled_at=sampled_at, windows={"local_all": window})


def _thread_rows(state_db: Path) -> list[tuple[Any, Any, Any]]:
    uri = state_db.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, timeout=10, uri=True) as conn:
        return list(conn.execute("SELECT cwd, model, tokens_used FROM threads"))


def _project_aggregates(raw: Any, total_tokens: int) -> dict[str, AggregateBreakdown]:
    if not isinstance(raw, dict):
        return {}

    aggregates: dict[str, AggregateBreakdown] = {}
    for project, value in raw.items():
        if not isinstance(value, dict):
            continue
        tokens = int(value.get("tokens") or value.get("total_tokens") or 0)
        requests = int(value.get("requests") or 0)
        share_pct = value.get("share_pct")
        if share_pct is None:
            share_pct = round(tokens / total_tokens * 100, 1) if total_tokens else 0.0
        merge_project_breakdown(
            aggregates,
            project,
            tokens=tokens,
            requests=requests,
            models=_int_map(value.get("models", {})),
            model_requests=_int_map(value.get("model_requests", {})),
        )
    return finalize_project_aggregates(aggregates, total_tokens)


def _model_aggregates(projects: dict[str, AggregateBreakdown], total_tokens: int) -> dict[str, AggregateBreakdown]:
    model_tokens: dict[str, int] = {}
    model_requests: dict[str, int] = {}
    for project in projects.values():
        for model, tokens in project.models.items():
            model_tokens[model] = model_tokens.get(model, 0) + int(tokens)
        for model, requests in project.model_requests.items():
            model_requests[model] = model_requests.get(model, 0) + int(requests)
    return {
        model: AggregateBreakdown(
            total_tokens=tokens,
            requests=model_requests.get(model, 0),
            share_pct=round(tokens / total_tokens * 100, 1) if total_tokens else 0.0,
        )
        for model, tokens in sorted(model_tokens.items(), key=lambda item: -item[1])
    }


def _int_map(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        try:
            result[str(key)] = int(value or 0)
        except (TypeError, ValueError):
            continue
    return result


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
