"""Shared dashboard display formatting helpers."""

from __future__ import annotations

import time

from quota_core.snapshot import SnapshotWindow


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