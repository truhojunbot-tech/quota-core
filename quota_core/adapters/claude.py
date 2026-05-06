"""Normalize Claude quota payloads into the public snapshot schema."""

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
    SnapshotWindow,
)

WINDOW_SECONDS = {
    "five_hour": 5 * 3600,
    "seven_day": 7 * 24 * 3600,
}
MAX_LOCAL_SCAN_FILE_BYTES = 50 * 1024 * 1024


def normalize_claude_quota(payload: dict[str, Any]) -> NormalizedSnapshot:
    """Convert the existing Claude monitor payload into a normalized snapshot."""

    sampled_at = int(payload.get("fetched_at") or 0)
    error = payload.get("error")
    if error:
        return NormalizedSnapshot(source="claude", sampled_at=sampled_at, errors=(str(error),))

    windows: dict[str, SnapshotWindow] = {}
    for window_name, window_seconds in WINDOW_SECONDS.items():
        raw_window = payload.get(window_name)
        if not isinstance(raw_window, dict):
            continue
        resets_at = _optional_int(raw_window.get("resets_at"))
        window_start = resets_at - window_seconds if resets_at is not None else None
        window_end = min(sampled_at, resets_at) if sampled_at and resets_at is not None else sampled_at or resets_at
        total_tokens = int(raw_window.get("tokens_used") or 0)
        runtime_total = int(raw_window.get("runtime_tokens_used") or 0)
        runtime_requests = int(raw_window.get("runtime_requests") or 0)
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
                requests=runtime_requests,
                by_project=runtime_by_project,
                by_model=_model_aggregates(runtime_by_project, runtime_total),
            ),
            cache_state="live",
            stale=False,
        )
    return NormalizedSnapshot(source="claude", sampled_at=sampled_at, windows=windows)


def scan_claude_local(config: ProviderConfig, sampled_at: int) -> NormalizedSnapshot:
    """Scan Claude local JSONL usage from public config."""

    projects_dir_raw = config.paths.get("projects_dir")
    if not projects_dir_raw:
        return NormalizedSnapshot(
            source="claude",
            sampled_at=sampled_at,
            warnings=("claude projects_dir is not configured",),
        )

    projects_dir = Path(projects_dir_raw).expanduser()
    if not projects_dir.exists():
        return NormalizedSnapshot(
            source="claude",
            sampled_at=sampled_at,
            warnings=("claude projects_dir does not exist",),
        )

    total_tokens = 0
    requests = 0
    projects: dict[str, AggregateBreakdown] = {}
    skipped_large_files = 0

    for jsonl_file in sorted(projects_dir.rglob("*.jsonl")):
        project_name = normalize_project_name(jsonl_file.parent.name or "unknown")
        try:
            if jsonl_file.stat().st_size > MAX_LOCAL_SCAN_FILE_BYTES:
                skipped_large_files += 1
                continue
        except OSError:
            continue
        try:
            line_iter = jsonl_file.open(errors="replace")
        except OSError:
            continue
        with line_iter as lines:
            for line in lines:
                record = _parse_json_line(line)
                if record is None:
                    continue
                usage = _extract_usage(record)
                if not usage:
                    continue
                tokens = _usage_tokens(usage)
                if tokens <= 0:
                    continue
                model = _record_model(record)
                total_tokens += tokens
                requests += 1
                current = projects.get(project_name, AggregateBreakdown())
                models = dict(current.models)
                model_requests = dict(current.model_requests)
                if model:
                    models[model] = models.get(model, 0) + tokens
                    model_requests[model] = model_requests.get(model, 0) + 1
                projects[project_name] = AggregateBreakdown(
                    total_tokens=current.total_tokens + tokens,
                    requests=current.requests + 1,
                    models=models,
                    model_requests=model_requests,
                )

    warnings = ()
    if skipped_large_files:
        warnings = (f"claude skipped {skipped_large_files} oversized local files",)

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
    return NormalizedSnapshot(source="claude", sampled_at=sampled_at, windows={"local_all": window}, warnings=warnings)


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
        models = _int_map(value.get("models", {}))
        model_requests = _int_map(value.get("model_requests", {}))
        merge_project_breakdown(
            aggregates,
            project,
            tokens=tokens,
            requests=requests,
            models=models,
            model_requests=model_requests,
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


def _parse_json_line(line: str) -> dict[str, Any] | None:
    if not line.strip() or "usage" not in line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _extract_usage(record: dict[str, Any]) -> dict[str, Any] | None:
    usage = record.get("message", {}).get("usage") if isinstance(record.get("message"), dict) else None
    if usage is None:
        usage = record.get("usage")
    return usage if isinstance(usage, dict) else None


def _usage_tokens(usage: dict[str, Any]) -> int:
    return sum(
        int(usage.get(key) or 0)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    )


def _record_model(record: dict[str, Any]) -> str:
    raw_model = None
    message = record.get("message")
    if isinstance(message, dict):
        raw_model = message.get("model")
    raw_model = raw_model or record.get("model") or "unknown"
    return str(raw_model).split("/")[-1]


