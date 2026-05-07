"""Public session analytics API envelope and query semantics."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Literal

CacheState = Literal["live", "cached", "stale"]
RedactionMode = Literal["summary", "preview", "none"]
WindowKind = Literal["quota", "rolling", "all"]

DEFAULT_SINCE = "24h"
DEFAULT_REDACTION: RedactionMode = "preview"

QUOTA_WINDOWS = {
    "5h": ("five_hour", 5 * 3600),
    "7d": ("seven_day", 7 * 24 * 3600),
}
ROLLING_SINCE_SECONDS = {
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
}


@dataclass(frozen=True)
class SessionReportWindow:
    """Window selected for a Claude session analytics report."""

    kind: WindowKind
    name: str
    window_start: int | None
    window_end: int | None
    window_source: str


@dataclass(frozen=True)
class SessionReportQuery:
    """Normalized query options for the session report endpoint."""

    window: SessionReportWindow
    redaction: RedactionMode = DEFAULT_REDACTION
    requested_window: str | None = None
    requested_since: str | None = DEFAULT_SINCE


def normalize_session_report_query(
    *,
    window: str | None = None,
    since: str | None = None,
    redaction: str | None = None,
    now: int | None = None,
    quota_windows: dict[str, Any] | None = None,
) -> SessionReportQuery:
    """Normalize endpoint query parameters into one selected report window.

    `window` wins over `since` because quota-card diagnostics should use the
    current quota window even if callers pass a stale rolling selector too.
    """

    selected_redaction = _normalize_redaction(redaction)
    selected_now = int(time.time()) if now is None else int(now)
    selected_window = _select_window(
        window=window,
        since=since or DEFAULT_SINCE,
        now=selected_now,
        quota_windows=quota_windows or {},
    )
    return SessionReportQuery(
        window=selected_window,
        redaction=selected_redaction,
        requested_window=window,
        requested_since=since or DEFAULT_SINCE,
    )


def build_empty_session_report(
    *,
    window: str | None = None,
    since: str | None = None,
    redaction: str | None = None,
    generated_at: int | None = None,
    cache_state: CacheState = "live",
    cache_age_seconds: int = 0,
    warnings: list[str] | tuple[str, ...] = (),
    errors: list[str] | tuple[str, ...] = (),
    quota_windows: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a schema-shaped empty Claude session analytics response."""

    now = int(time.time()) if generated_at is None else int(generated_at)
    query = normalize_session_report_query(
        window=window,
        since=since,
        redaction=redaction,
        now=now,
        quota_windows=quota_windows,
    )
    return build_session_report(
        query=query,
        generated_at=now,
        cache_state=cache_state,
        cache_age_seconds=cache_age_seconds,
        warnings=warnings,
        errors=errors,
    )


def build_session_report(
    *,
    query: SessionReportQuery,
    generated_at: int,
    cache_state: CacheState = "live",
    cache_age_seconds: int = 0,
    totals: dict[str, Any] | None = None,
    by_project: list[dict[str, Any]] | None = None,
    by_model: list[dict[str, Any]] | None = None,
    by_subagent: list[dict[str, Any]] | None = None,
    by_skill: list[dict[str, Any]] | None = None,
    by_slash_command: list[dict[str, Any]] | None = None,
    hourly_bursts: list[dict[str, Any]] | None = None,
    top_sessions: list[dict[str, Any]] | None = None,
    cache_efficiency: list[dict[str, Any]] | None = None,
    expensive_prompts: list[dict[str, Any]] | None = None,
    cache_breaks: list[dict[str, Any]] | None = None,
    runtime_attribution: dict[str, Any] | None = None,
    reconciliation: dict[str, Any] | None = None,
    warnings: list[str] | tuple[str, ...] = (),
    errors: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a Claude session analytics response envelope."""

    return {
        "source": "claude",
        "generated_at": int(generated_at),
        "cache_state": cache_state,
        "cache_age_seconds": int(cache_age_seconds),
        "redaction": query.redaction,
        "window": _window_to_dict(query.window),
        "totals": _totals(totals),
        "by_project": list(by_project or []),
        "by_model": list(by_model or []),
        "by_subagent": list(by_subagent or []),
        "by_skill": list(by_skill or []),
        "by_slash_command": list(by_slash_command or []),
        "hourly_bursts": list(hourly_bursts or []),
        "top_sessions": list(top_sessions or []),
        "cache_efficiency": list(cache_efficiency or []),
        "expensive_prompts": list(expensive_prompts or []),
        "cache_breaks": list(cache_breaks or []),
        "runtime_attribution": runtime_attribution or _runtime_attribution(),
        "reconciliation": reconciliation or _reconciliation(),
        "warnings": [str(warning) for warning in warnings],
        "errors": [str(error) for error in errors],
    }


def validate_session_report_dict(report: dict[str, Any]) -> tuple[str, ...]:
    """Return validation errors for the public session analytics envelope."""

    errors: list[str] = []
    required = (
        "source",
        "generated_at",
        "cache_state",
        "cache_age_seconds",
        "window",
        "totals",
        "by_project",
        "by_model",
        "by_subagent",
        "by_skill",
        "by_slash_command",
        "hourly_bursts",
        "top_sessions",
        "cache_efficiency",
        "expensive_prompts",
        "cache_breaks",
        "runtime_attribution",
        "reconciliation",
        "warnings",
        "errors",
    )
    for key in required:
        if key not in report:
            errors.append(f"{key} is required")
    if report.get("source") != "claude":
        errors.append("source must be claude")
    if not isinstance(report.get("generated_at"), int):
        errors.append("generated_at must be an integer unix timestamp")
    if report.get("cache_state") not in {"live", "cached", "stale"}:
        errors.append("cache_state must be live, cached, or stale")
    if not isinstance(report.get("cache_age_seconds"), int):
        errors.append("cache_age_seconds must be an integer")
    _validate_window(report.get("window"), errors)
    _validate_totals(report.get("totals"), errors)
    for key in (
        "by_project",
        "by_model",
        "by_subagent",
        "by_skill",
        "by_slash_command",
        "hourly_bursts",
        "top_sessions",
        "cache_efficiency",
        "expensive_prompts",
        "cache_breaks",
    ):
        if not isinstance(report.get(key), list):
            errors.append(f"{key} must be a list")
    if not isinstance(report.get("runtime_attribution"), dict):
        errors.append("runtime_attribution must be an object")
    if not isinstance(report.get("reconciliation"), dict):
        errors.append("reconciliation must be an object")
    if not isinstance(report.get("warnings"), list):
        errors.append("warnings must be a list")
    if not isinstance(report.get("errors"), list):
        errors.append("errors must be a list")
    return tuple(errors)


def _select_window(*, window: str | None, since: str, now: int, quota_windows: dict[str, Any]) -> SessionReportWindow:
    if window in QUOTA_WINDOWS:
        name, default_seconds = QUOTA_WINDOWS[window]
        metadata = quota_windows.get(name, {})
        resets_at = _optional_int(metadata.get("resets_at")) if isinstance(metadata, dict) else None
        window_seconds = _optional_int(metadata.get("window_seconds")) if isinstance(metadata, dict) else None
        if window_seconds is None:
            window_seconds = default_seconds
        window_start = resets_at - window_seconds if resets_at is not None else None
        window_end = min(now, resets_at) if resets_at is not None else now
        return SessionReportWindow(
            kind="quota",
            name=name,
            window_start=window_start,
            window_end=window_end,
            window_source="quota_resets_at",
        )

    if since == "all":
        return SessionReportWindow(
            kind="all",
            name="all",
            window_start=None,
            window_end=now,
            window_source="all_transcripts",
        )

    seconds = ROLLING_SINCE_SECONDS.get(since, ROLLING_SINCE_SECONDS[DEFAULT_SINCE])
    name = since if since in ROLLING_SINCE_SECONDS else DEFAULT_SINCE
    return SessionReportWindow(
        kind="rolling",
        name=name,
        window_start=now - seconds,
        window_end=now,
        window_source="rolling_since",
    )


def _normalize_redaction(redaction: str | None) -> RedactionMode:
    if redaction in {"summary", "preview", "none"}:
        return redaction  # type: ignore[return-value]
    return DEFAULT_REDACTION


def _window_to_dict(window: SessionReportWindow) -> dict[str, Any]:
    return {
        "kind": window.kind,
        "name": window.name,
        "window_start": window.window_start,
        "window_end": window.window_end,
        "window_source": window.window_source,
    }


def _totals(totals: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "total_tokens": 0,
        "api_calls": 0,
        "deduped_api_calls": 0,
        "cache_hit_pct": 0.0,
        "active_seconds": 0,
        "wall_seconds": 0,
    }
    if totals:
        data.update(totals)
    return data


def _runtime_attribution() -> dict[str, Any]:
    return {
        "human_tokens": 0,
        "runtime_tokens": 0,
        "unknown_tokens": 0,
        "by_class": [],
    }


def _reconciliation() -> dict[str, Any]:
    return {
        "quota_scanner_total_tokens": None,
        "session_total_tokens": 0,
        "delta_tokens": None,
        "delta_pct": None,
        "notes": [],
    }


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate_window(window: Any, errors: list[str]) -> None:
    if not isinstance(window, dict):
        errors.append("window must be an object")
        return
    for key in ("kind", "name", "window_source"):
        if not isinstance(window.get(key), str) or not window.get(key):
            errors.append(f"window.{key} must be a non-empty string")
    for key in ("window_start", "window_end"):
        if window.get(key) is not None and not isinstance(window.get(key), int):
            errors.append(f"window.{key} must be an integer or null")


def _validate_totals(totals: Any, errors: list[str]) -> None:
    if not isinstance(totals, dict):
        errors.append("totals must be an object")
        return
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "total_tokens",
        "api_calls",
        "deduped_api_calls",
        "active_seconds",
        "wall_seconds",
    ):
        if not isinstance(totals.get(key), int):
            errors.append(f"totals.{key} must be an integer")
    if not isinstance(totals.get("cache_hit_pct"), (int, float)):
        errors.append("totals.cache_hit_pct must be numeric")