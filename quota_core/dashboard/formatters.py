"""Shared dashboard display formatting helpers."""

from __future__ import annotations

import time

from quota_core.snapshot import RuntimeBreakdown, SnapshotWindow


def runtime_share_label(runtime: RuntimeBreakdown, service_total: int) -> str:
    if runtime.total_tokens <= 0 and runtime.requests <= 0:
        return "runtime 없음"
    share = runtime.total_tokens / service_total if service_total > 0 else 0.0
    return f"{_clamped_pct(share):.1f}%"


def quota_utilization_label(window: SnapshotWindow, *, decimals: int = 1) -> str:
    if quota_utilization_is_delayed(window):
        return "집계 지연"
    return f"{_clamped_pct(window.utilization):.{decimals}f}%"


def runtime_quota_context_label(window: SnapshotWindow) -> str:
    if quota_utilization_is_delayed(window):
        return "quota 집계 지연"
    return f"{_clamped_pct(window.utilization):.1f}% of quota"


def quota_utilization_is_delayed(window: SnapshotWindow) -> bool:
    return bool(window.total_tokens > 0 and window.utilization <= 0 and (window.stale or window.cache_state == "stale"))


def window_reset_label(window: SnapshotWindow, *, now: float | None = None) -> str:
    return reset_countdown_label(
        window.resets_at,
        stale=window.stale or window.cache_state == "stale",
        none_label="",
        minute_template="{minutes:.0f}분 후 리셋",
        hour_template="{hours:.1f}시간 후 리셋",
        now=now,
    )


def timestamp_reset_label(resets_at: int | None, *, now: float | None = None) -> str:
    return reset_countdown_label(
        resets_at,
        stale=False,
        none_label="-",
        minute_template="{minutes:.0f}분 후",
        hour_template="{hours:.1f}h 후",
        now=now,
    )


def reset_countdown_label(
    resets_at: int | None,
    *,
    stale: bool,
    none_label: str,
    minute_template: str,
    hour_template: str,
    now: float | None = None,
) -> str:
    if not resets_at:
        return none_label
    diff = resets_at - (time.time() if now is None else now)
    if diff < 0:
        return "집계 지연" if stale else "리셋 시각 지남"
    if diff < 3600:
        return minute_template.format(minutes=diff / 60, hours=diff / 3600)
    return hour_template.format(minutes=diff / 60, hours=diff / 3600)


def _clamped_pct(utilization: float) -> float:
    return max(0.0, min(100.0, utilization * 100))