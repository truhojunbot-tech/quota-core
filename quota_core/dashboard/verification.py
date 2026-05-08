"""Verification helpers for generated dashboard artifacts."""

from __future__ import annotations

from quota_core.dashboard.formatters import quota_utilization_label, runtime_quota_context_label, runtime_share_label, window_reset_label
from quota_core.snapshot import NormalizedSnapshot, SnapshotWindow

HISTORY_NOTE = "30일 사용량 히스토리 · 현재 quota 창 아님"
OLD_RESET_LABEL = "리셋됨"


def verify_dashboard_html(snapshots: list[NormalizedSnapshot], html: str) -> list[str]:
    errors: list[str] = []
    if OLD_RESET_LABEL in html:
        errors.append(f"dashboard contains deprecated reset label: {OLD_RESET_LABEL}")

    if _has_usage_timeline(snapshots) and HISTORY_NOTE not in html:
        errors.append(f"usage timeline is missing history-vs-quota note: {HISTORY_NOTE}")

    for snapshot in snapshots:
        for window_name, window in snapshot.windows.items():
            _verify_window_labels(errors, snapshot.source, window_name, window, html)
    return errors


def _verify_window_labels(errors: list[str], source: str, window_name: str, window: SnapshotWindow, html: str) -> None:
    quota_label = quota_utilization_label(window)
    reset_label = window_reset_label(window)
    if quota_label and quota_label not in html:
        errors.append(f"{source}.{window_name} missing quota label: {quota_label}")
    if reset_label and reset_label not in html:
        errors.append(f"{source}.{window_name} missing reset label: {reset_label}")

    runtime_label = runtime_share_label(window.runtime, window.total_tokens)
    if runtime_label and runtime_label not in html:
        errors.append(f"{source}.{window_name} missing runtime label: {runtime_label}")

    quota_context = runtime_quota_context_label(window)
    if quota_context and quota_context not in html:
        errors.append(f"{source}.{window_name} missing runtime quota context: {quota_context}")

    if (window.stale or window.cache_state == "stale") and "시간 후 리셋" in reset_label:
        errors.append(f"{source}.{window_name} stale window renders a live reset countdown")


def _has_usage_timeline(snapshots: list[NormalizedSnapshot]) -> bool:
    for snapshot in snapshots:
        timeline = snapshot.history.get("usage_timeline", {}) if isinstance(snapshot.history, dict) else {}
        if not isinstance(timeline, dict):
            continue
        daily_total = timeline.get("daily_total", {})
        if isinstance(daily_total, dict) and any(float(value or 0) > 0 for value in daily_total.values()):
            return True
    return False