"""Dashboard view model derived from normalized provider snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Literal

from quota_core.snapshot import NormalizedSnapshot, SnapshotWindow

WindowKind = Literal["quota", "usage", "local"]
WindowRole = Literal["short", "weekly", "detail"]


@dataclass(frozen=True)
class DashboardWindow:
    """A window with dashboard-specific meaning."""

    name: str
    window: SnapshotWindow
    kind: WindowKind
    role: WindowRole

    @property
    def is_quota(self) -> bool:
        return self.kind == "quota"

    @property
    def is_usage(self) -> bool:
        return self.kind == "usage"


@dataclass(frozen=True)
class ProviderDashboard:
    """Provider dashboard structure matching the original operations report."""

    snapshot: NormalizedSnapshot
    windows: tuple[DashboardWindow, ...]
    primary: DashboardWindow | None
    comparison: tuple[DashboardWindow, ...]
    details: tuple[DashboardWindow, ...]

    @property
    def source(self) -> str:
        return self.snapshot.source


def build_provider_dashboard(snapshot: NormalizedSnapshot) -> ProviderDashboard:
    windows = tuple(_dashboard_window(name, window) for name, window in snapshot.windows.items())
    by_name = {item.name: item for item in windows}
    comparison_names = _comparison_window_names(by_name)
    comparison = tuple(by_name[name] for name in comparison_names)
    primary = _primary_window(windows)
    details = tuple(item for item in windows if item.name not in comparison_names)
    return ProviderDashboard(
        snapshot=snapshot,
        windows=windows,
        primary=primary,
        comparison=comparison,
        details=details,
    )


def build_dashboard(snapshots: list[NormalizedSnapshot]) -> tuple[ProviderDashboard, ...]:
    return tuple(build_provider_dashboard(snapshot) for snapshot in snapshots)


def iter_quota_windows(providers: tuple[ProviderDashboard, ...]) -> list[tuple[str, DashboardWindow]]:
    return [
        (provider.source, window)
        for provider in providers
        for window in provider.windows
        if window.is_quota
    ]


def pressure_windows(providers: tuple[ProviderDashboard, ...]) -> list[tuple[str, DashboardWindow]]:
    return sorted(iter_quota_windows(providers), key=lambda item: item[1].window.utilization, reverse=True)


def next_reset_window(providers: tuple[ProviderDashboard, ...]) -> tuple[str, DashboardWindow] | None:
    candidates = [
        item
        for item in iter_quota_windows(providers)
        if item[1].window.resets_at and item[1].window.resets_at > time.time()
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[1].window.resets_at or 0)


def data_state_label(providers: tuple[ProviderDashboard, ...]) -> str:
    states = [window.window.cache_state for provider in providers for window in provider.windows]
    if not states:
        return "no data"
    cached = sum(1 for state in states if state in {"cached", "stale"})
    live = sum(1 for state in states if state == "live")
    if cached and live:
        return f"{live} live / {cached} cached"
    if cached:
        return f"{cached} cached"
    return f"{live} live"


def window_is_quota(name: str, window: SnapshotWindow) -> bool:
    return name != "local_all" and (window.resets_at is not None or window.window_start is not None or window.utilization > 0)


def window_is_usage(name: str, window: SnapshotWindow) -> bool:
    return name in {"seven_day", "today", "this_month"} and (window.total_tokens > 0 or bool(window.by_project))


def _dashboard_window(name: str, window: SnapshotWindow) -> DashboardWindow:
    kind: WindowKind
    if window_is_quota(name, window):
        kind = "quota"
    elif window_is_usage(name, window):
        kind = "usage"
    else:
        kind = "local"
    role: WindowRole = "weekly" if name == "seven_day" else "short" if name in {"five_hour", "current_quota", "today"} else "detail"
    return DashboardWindow(name=name, window=window, kind=kind, role=role)


def _comparison_window_names(windows: dict[str, DashboardWindow]) -> tuple[str, ...]:
    short_name = next(
        (
            name
            for name in ("five_hour", "current_quota", "today")
            if name in windows and _is_report_window(windows[name])
        ),
        None,
    )
    has_week = "seven_day" in windows and _is_report_window(windows["seven_day"])
    if short_name and has_week:
        return (short_name, "seven_day")
    if has_week:
        return ("seven_day",)
    return (short_name,) if short_name else ()


def _is_report_window(window: DashboardWindow) -> bool:
    return window.kind in {"quota", "usage"}


def _primary_window(windows: tuple[DashboardWindow, ...]) -> DashboardWindow | None:
    quota_windows = [window for window in windows if window.is_quota and window.name in {"five_hour", "current_quota", "seven_day", "today"}]
    if quota_windows:
        return max(quota_windows, key=lambda item: item.window.utilization)
    for window in windows:
        if window.name == "local_all":
            return window
    return windows[0] if windows else None