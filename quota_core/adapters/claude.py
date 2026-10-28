"""Normalize Claude quota payloads into the public snapshot schema."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from quota_core.adapters.projects import (
    finalize_project_aggregates,
    merge_project_breakdown,
    model_aggregates_from_projects,
    normalize_project_name,
    project_aggregates_from_raw,
)
from quota_core.config import ProviderConfig
from quota_core.session.claude import dedupe_key
from quota_core.snapshot import (
    AggregateBreakdown,
    NormalizedSnapshot,
    RuntimeBreakdown,
    SnapshotWindow,
)

WINDOW_SECONDS = {
    "five_hour": 5 * 3600,
    "seven_day": 7 * 24 * 3600,
    "current_session": 5 * 3600,
    "current_week": 7 * 24 * 3600,
    "current_week_sonnet": 7 * 24 * 3600,
}
MAX_LOCAL_SCAN_FILE_BYTES = 50 * 1024 * 1024
VALID_CACHE_STATES = {"live", "cached", "stale", "unknown"}
LIVE_MAX_AGE_SECONDS = 30
CACHED_MAX_AGE_SECONDS = 300


def normalize_claude_quota(payload: dict[str, Any], now: int | None = None) -> NormalizedSnapshot:
    """Convert the existing Claude monitor payload into a normalized snapshot.

    ``now`` (default: the current time) dates the payload: without an explicit
    cache state, its freshness comes from the age of ``fetched_at``.
    """

    sampled_at = int(payload.get("fetched_at") or 0)
    now = int(time.time()) if now is None else now
    error = payload.get("error")
    if error:
        return NormalizedSnapshot(source="claude", sampled_at=sampled_at, errors=(str(error),))

    windows: dict[str, SnapshotWindow] = {}
    for window_name, default_window_seconds in WINDOW_SECONDS.items():
        raw_window = payload.get(window_name)
        if not isinstance(raw_window, dict):
            continue
        window_seconds = int(raw_window.get("window_seconds") or default_window_seconds)
        resets_at = _optional_int(raw_window.get("resets_at"))
        window_start = resets_at - window_seconds if resets_at is not None else None
        window_end = min(sampled_at, resets_at) if sampled_at and resets_at is not None else sampled_at or resets_at
        total_tokens = int(raw_window.get("tokens_used") or 0)
        runtime_total = int(raw_window.get("runtime_tokens_used") or 0)
        runtime_requests = int(raw_window.get("runtime_requests") or 0)
        by_project = project_aggregates_from_raw(raw_window.get("by_project", {}), total_tokens)
        runtime_by_project = project_aggregates_from_raw(raw_window.get("runtime_by_project", {}), runtime_total)
        cache_state = _cache_state(raw_window, payload, sampled_at, now)
        windows[window_name] = SnapshotWindow(
            window_start=window_start,
            window_end=window_end,
            resets_at=resets_at,
            utilization=_optional_utilization(raw_window.get("utilization")),
            total_tokens=total_tokens,
            requests=sum(item.requests for item in by_project.values()),
            by_project=by_project,
            by_model=model_aggregates_from_projects(by_project, total_tokens),
            runtime=RuntimeBreakdown(
                total_tokens=runtime_total,
                requests=runtime_requests,
                by_project=runtime_by_project,
                by_model=model_aggregates_from_projects(runtime_by_project, runtime_total),
            ),
            cache_state=cache_state,  # type: ignore[arg-type]
            stale=cache_state == "stale",
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

    skipped_large_files = 0
    malformed_usage_records = 0
    # Claude Code writes one line per content block of a streamed response,
    # each repeating the request's usage with cumulative (growing) output
    # counts; resumed sessions copy whole records. Keep one entry per request,
    # holding the component-wise maximum, i.e. its final usage.
    by_key: dict[str, tuple[str, str | None, dict[str, int]]] = {}

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
                components = _usage_components(usage)
                if components is None:
                    malformed_usage_records += 1
                    continue
                if sum(components.values()) <= 0:
                    continue
                key = dedupe_key(record, components)
                seen = by_key.get(key)
                if seen is None:
                    by_key[key] = (project_name, _record_model(record), components)
                    continue
                seen_project, seen_model, seen_components = seen
                merged = {name: max(value, seen_components.get(name, 0)) for name, value in components.items()}
                by_key[key] = (seen_project, seen_model or _record_model(record), merged)

    total_tokens = 0
    requests = 0
    projects: dict[str, AggregateBreakdown] = {}
    for project_name, model, components in by_key.values():
        tokens = sum(components.values())
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

    warnings: tuple[str, ...] = ()
    if skipped_large_files:
        warnings += (f"claude skipped {skipped_large_files} oversized local files",)
    if malformed_usage_records:
        warnings += (f"claude skipped {malformed_usage_records} records with malformed usage",)

    by_project = finalize_project_aggregates(projects, total_tokens)
    window = SnapshotWindow(
        window_start=None,
        window_end=sampled_at,
        resets_at=None,
        utilization=0.0,
        total_tokens=total_tokens,
        requests=requests,
        by_project=by_project,
        by_model=model_aggregates_from_projects(by_project, total_tokens),
        cache_state="live",
        stale=False,
    )
    return NormalizedSnapshot(source="claude", sampled_at=sampled_at, windows={"local_all": window}, warnings=warnings)


def _cache_state(raw_window: dict[str, Any], payload: dict[str, Any], fetched_at: int, now: int) -> str:
    state = raw_window.get("cache_state") or payload.get("cache_state")
    if state in VALID_CACHE_STATES:
        return str(state)
    if raw_window.get("stale") or payload.get("stale"):
        return "stale"
    if fetched_at <= 0:
        return "unknown"
    age = max(0, now - fetched_at)
    if age <= LIVE_MAX_AGE_SECONDS:
        return "live"
    if age <= CACHED_MAX_AGE_SECONDS:
        return "cached"
    return "stale"


def _optional_utilization(value: Any) -> float | None:
    """Reported utilization, or ``None`` when absent/unreadable (never a fake 0.0)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
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


def _usage_components(usage: dict[str, Any]) -> dict[str, int] | None:
    """Usage token components, or ``None`` when a present component is not a number.

    A partly unreadable record is skipped as a whole rather than counted with
    the bad component as zero, which would understate the total silently.
    Only an absent/null component counts as zero; empty strings, containers,
    booleans, negative and fractional/non-finite counts are malformed.
    """
    components: dict[str, int] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        value = usage.get(key)
        if value is None:
            components[key] = 0
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        if isinstance(value, float) and not value.is_integer():
            return None
        parsed = _optional_int(value)
        if parsed is None or parsed < 0:
            return None
        components[key] = parsed
    return components


def _record_model(record: dict[str, Any]) -> str:
    raw_model = None
    message = record.get("message")
    if isinstance(message, dict):
        raw_model = message.get("model")
    raw_model = raw_model or record.get("model") or "unknown"
    return str(raw_model).split("/")[-1]


