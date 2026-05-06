"""Normalize Gemini usage payloads into the public snapshot schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quota_core.adapters.projects import finalize_project_aggregates, merge_project_breakdown, normalize_project_name
from quota_core.config import ProviderConfig
from quota_core.snapshot import (
    AggregateBreakdown,
    NormalizedSnapshot,
    RuntimeBreakdown,
    SnapshotQuotaGroup,
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
            quota_groups=_gemini_quota_groups(payload.get("quota_by_model", {})),
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
    for session_file in _session_files(tmp_dir):
        try:
            if session_file.stat().st_size > MAX_LOCAL_SCAN_FILE_BYTES:
                skipped_large_files += 1
                continue
        except OSError:
            continue
        project_name = normalize_project_name(_project_name(tmp_dir, session_file))
        for message in _iter_session_messages(session_file):
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

    by_project = finalize_project_aggregates(projects, total_tokens)
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


def _session_files(tmp_dir: Path) -> list[Path]:
    return sorted([*tmp_dir.rglob("session-*.json"), *tmp_dir.rglob("session-*.jsonl")])


def _load_session_file(path: Path) -> dict[str, Any] | None:
    try:
        record = json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _iter_session_messages(path: Path) -> list[dict[str, Any]]:
    seen: set[str] = set()
    messages: list[dict[str, Any]] = []
    if path.suffix == ".jsonl":
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            key = str(message.get("id") or f"{message.get('timestamp')}:{message.get('type')}:{message.get('content', '')[:80]}")
            if key in seen:
                continue
            seen.add(key)
            messages.append(message)
        return messages
    record = _load_session_file(path)
    if record is None:
        return []
    for message in record.get("messages", []):
        if not isinstance(message, dict):
            continue
        key = str(message.get("id") or f"{message.get('timestamp')}:{message.get('type')}:{message.get('content', '')[:80]}")
        if key in seen:
            continue
        seen.add(key)
        messages.append(message)
    return messages


def _project_name(tmp_dir: Path, session_file: Path) -> str:
    try:
        relative = session_file.relative_to(tmp_dir)
    except ValueError:
        project_dir = session_file.parents[1] if len(session_file.parents) > 1 else session_file.parent
        return _project_root_name(project_dir) or session_file.parent.name or "unknown"
    if not relative.parts:
        return "unknown"
    project_dir = tmp_dir / relative.parts[0]
    return _project_root_name(project_dir) or relative.parts[0]


def _project_root_name(project_dir: Path) -> str | None:
    marker = project_dir / ".project_root"
    try:
        value = marker.read_text(errors="replace").strip()
    except OSError:
        return None
    return value or None


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
    quota_groups: dict[str, SnapshotQuotaGroup] | None = None,
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
        quota_groups=quota_groups or {},
        runtime=runtime,
        cache_state=cache_state,  # type: ignore[arg-type]
        stale=stale,
    )


def _gemini_quota_groups(raw: Any) -> dict[str, SnapshotQuotaGroup]:
    if not isinstance(raw, dict):
        return {}
    groups = {
        "flash": (
            "Flash 그룹",
            (
                "gemini-2.5-flash",
                "gemini-3-flash-preview",
                "gemini-2.5-flash-lite",
                "gemini-3.1-flash-lite-preview",
            ),
        ),
        "pro": (
            "Pro 그룹",
            (
                "gemini-2.5-pro",
                "gemini-3-pro-preview",
                "gemini-3.1-pro-preview",
            ),
        ),
    }
    result: dict[str, SnapshotQuotaGroup] = {}
    for key, (label, models) in groups.items():
        rows = [raw.get(model) for model in models if isinstance(raw.get(model), dict)]
        if not rows:
            continue
        utilization = max(float(row.get("utilization") or 0.0) for row in rows)
        resets_at = next((_optional_int(row.get("resets_at")) for row in rows if _optional_int(row.get("resets_at"))), None)
        token_type = next((str(row.get("token_type") or "") for row in rows if row.get("token_type")), "")
        result[key] = SnapshotQuotaGroup(
            label=label,
            utilization=utilization,
            resets_at=resets_at,
            token_type=token_type,
            models=tuple(model for model in models if model in raw),
        )
    return result


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
