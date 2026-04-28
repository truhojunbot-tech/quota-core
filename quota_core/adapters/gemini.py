"""Normalize Gemini usage payloads into the public snapshot schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quota_core.config import ProviderConfig
from quota_core.snapshot import (
    AggregateBreakdown,
    NormalizedSnapshot,
    RuntimeBreakdown,
    SnapshotWindow,
)

WINDOW_SECONDS = {
    "minute": 60,
    "current_quota": 24 * 3600,
}
MAX_LOCAL_SCAN_FILE_BYTES = 50 * 1024 * 1024


def normalize_gemini_usage(payload: dict[str, Any]) -> NormalizedSnapshot:
    """Convert the existing Gemini monitor payload into a normalized snapshot."""

    sampled_at = int(payload.get("fetched_at") or 0)
    error = _quota_error(payload.get("quota"))
    warnings = (error,) if error else ()
    windows: dict[str, SnapshotWindow] = {}

    current_quota = payload.get("current_quota")
    if isinstance(current_quota, dict):
        windows["current_quota"] = _window_from_bucket(
            raw=current_quota,
            sampled_at=sampled_at,
            cache_state=_quota_cache_state(payload.get("quota"), sampled_at),
            runtime_raw=payload.get("runtime_current_quota"),
        )

    for name in ("minute", "today", "seven_day", "this_month"):
        raw = payload.get(name)
        if isinstance(raw, dict):
            windows[name] = _window_from_bucket(raw=raw, sampled_at=sampled_at, cache_state="unknown")

    return NormalizedSnapshot(source="gemini", sampled_at=sampled_at, windows=windows, warnings=warnings)


def scan_gemini_local(config: ProviderConfig, sampled_at: int) -> NormalizedSnapshot:
    """Scan Gemini local session usage from public config."""

    tmp_dir_raw = config.paths.get("tmp_dir")
    if not tmp_dir_raw:
        return NormalizedSnapshot(
            source="gemini",
            sampled_at=sampled_at,
            warnings=("gemini tmp_dir is not configured",),
        )

    tmp_dir = Path(tmp_dir_raw).expanduser()
    if not tmp_dir.exists():
        return NormalizedSnapshot(
            source="gemini",
            sampled_at=sampled_at,
            warnings=("gemini tmp_dir does not exist",),
        )

    total_tokens = 0
    requests = 0
    projects: dict[str, AggregateBreakdown] = {}
    skipped_large_files = 0
    for session_file in sorted(tmp_dir.rglob("session-*.json")):
        try:
            if session_file.stat().st_size > MAX_LOCAL_SCAN_FILE_BYTES:
                skipped_large_files += 1
                continue
        except OSError:
            continue
        record = _load_session_file(session_file)
        if record is None:
            continue
        project_name = _project_name(tmp_dir, session_file)
        for message in record.get("messages", []):
            if not isinstance(message, dict) or message.get("type") != "gemini":
                continue
            tokens, model = _message_tokens(message)
            if tokens <= 0:
                continue
            total_tokens += tokens
            requests += 1
            current = projects.get(project_name, AggregateBreakdown())
            models = dict(current.models)
            model_requests = dict(current.model_requests)
            model_name = model.split("/")[-1]
            models[model_name] = models.get(model_name, 0) + tokens
            model_requests[model_name] = model_requests.get(model_name, 0) + 1
            projects[project_name] = AggregateBreakdown(
                total_tokens=current.total_tokens + tokens,
                requests=current.requests + 1,
                models=models,
                model_requests=model_requests,
            )

    by_project = {
        project: AggregateBreakdown(
            total_tokens=aggregate.total_tokens,
            requests=aggregate.requests,
            share_pct=round(aggregate.total_tokens / total_tokens * 100, 1) if total_tokens else 0.0,
            models=aggregate.models,
            model_requests=aggregate.model_requests,
        )
        for project, aggregate in sorted(projects.items(), key=lambda item: -item[1].total_tokens)
    }
    warnings = ()
    if skipped_large_files:
        warnings = (f"gemini skipped {skipped_large_files} oversized local files",)

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
    return NormalizedSnapshot(source="gemini", sampled_at=sampled_at, windows={"local_all": window}, warnings=warnings)


def _load_session_file(path: Path) -> dict[str, Any] | None:
    try:
        record = json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _project_name(tmp_dir: Path, session_file: Path) -> str:
    try:
        relative = session_file.relative_to(tmp_dir)
    except ValueError:
        return session_file.parent.name or "unknown"
    return relative.parts[0] if relative.parts else "unknown"


def _message_tokens(message: dict[str, Any]) -> tuple[int, str]:
    raw_tokens = message.get("tokens")
    if not isinstance(raw_tokens, dict):
        return 0, str(message.get("model") or "unknown")
    total = raw_tokens.get("total")
    if total is None:
        total = sum(_coerce_int(raw_tokens.get(key)) for key in ("input", "output", "cached", "thoughts", "tool"))
    return _coerce_int(total), str(message.get("model") or "unknown")


def _window_from_bucket(
    raw: dict[str, Any],
    sampled_at: int,
    cache_state: str,
    runtime_raw: Any = None,
) -> SnapshotWindow:
    total_tokens = int(raw.get("total") or raw.get("total_tokens") or 0)
    requests = int(raw.get("requests") or 0)
    resets_at = _optional_int(raw.get("resets_at"))
    window_seconds = _optional_int(raw.get("window_seconds"))
    window_end = resets_at or sampled_at or None
    window_start = window_end - window_seconds if window_end is not None and window_seconds else None
    by_project = _project_aggregates(raw.get("by_project", {}), total_tokens)
    runtime = _runtime_breakdown(runtime_raw, total_tokens)
    stale = cache_state == "stale"
    return SnapshotWindow(
        window_start=window_start,
        window_end=window_end,
        resets_at=resets_at,
        utilization=float(raw.get("utilization") or 0.0),
        total_tokens=total_tokens,
        requests=requests,
        by_project=by_project,
        by_model=_model_aggregates(by_project, total_tokens),
        runtime=runtime,
        cache_state=cache_state,  # type: ignore[arg-type]
        stale=stale,
    )


def _runtime_breakdown(raw: Any, provider_total_tokens: int) -> RuntimeBreakdown:
    if not isinstance(raw, dict):
        return RuntimeBreakdown()
    total_tokens = int(raw.get("total") or raw.get("total_tokens") or 0)
    by_project = _project_aggregates(raw.get("by_project", {}), total_tokens or provider_total_tokens)
    return RuntimeBreakdown(
        total_tokens=total_tokens,
        requests=int(raw.get("requests") or 0),
        by_project=by_project,
        by_model=_model_aggregates(by_project, total_tokens),
    )


def _project_aggregates(raw: Any, total_tokens: int) -> dict[str, AggregateBreakdown]:
    if not isinstance(raw, dict):
        return {}

    aggregates: dict[str, AggregateBreakdown] = {}
    for project, value in raw.items():
        if not isinstance(value, dict):
            continue
        tokens = int(value.get("total") or value.get("tokens") or value.get("total_tokens") or 0)
        requests = int(value.get("requests") or 0)
        share_pct = value.get("share_pct")
        if share_pct is None:
            share_pct = round(tokens / total_tokens * 100, 1) if total_tokens else 0.0
        aggregates[str(project)] = AggregateBreakdown(
            total_tokens=tokens,
            requests=requests,
            share_pct=float(share_pct or 0.0),
            models=_int_map(value.get("models", {})),
            model_requests=_int_map(value.get("model_requests", {})),
        )
    return dict(sorted(aggregates.items(), key=lambda item: -item[1].total_tokens))


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


def _quota_cache_state(raw_quota: Any, sampled_at: int) -> str:
    if not isinstance(raw_quota, dict):
        return "unknown"
    if raw_quota.get("error"):
        return "stale"
    fetched_at = _optional_int(raw_quota.get("fetched_at"))
    if fetched_at is None or sampled_at <= 0:
        return "cached"
    age = max(0, sampled_at - fetched_at)
    if age <= 30:
        return "live"
    if age <= 300:
        return "cached"
    return "stale"


def _quota_error(raw_quota: Any) -> str | None:
    if isinstance(raw_quota, dict) and raw_quota.get("error"):
        return str(raw_quota["error"])
    return None


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
