"""Reusable dashboard component helpers."""

from __future__ import annotations

from datetime import datetime
import html
import time

from quota_core.snapshot import AggregateBreakdown, NormalizedSnapshot, SnapshotWindow

TOP_ROW_LIMIT = 12


def badge(label: str, tone: str = "neutral") -> str:
    """Render a small status badge."""

    safe_label = html.escape(label)
    safe_tone = html.escape(tone)
    return f'<span class="qc-badge qc-badge-{safe_tone}">{safe_label}</span>'


def provider_summary(snapshot: NormalizedSnapshot) -> str:
    """Render provider-level summary."""

    source = html.escape(snapshot.source)
    if snapshot.errors:
        body = "".join(f"<p>{html.escape(error)}</p>" for error in snapshot.errors)
        return f'<section class="qc-provider qc-provider-error"><h2>{source}</h2>{badge("error", "danger")}{body}</section>'
    if not snapshot.windows:
        return f'<section class="qc-provider qc-provider-empty"><h2>{source}</h2>{badge("empty", "muted")}</section>'

    primary_name, primary_window = primary_window_for(snapshot)
    windows = "".join(window_panel(name, window, compact=name != primary_name) for name, window in snapshot.windows.items())
    warnings = "".join(f"<p class=\"qc-warning\">{html.escape(warning)}</p>" for warning in snapshot.warnings)
    return (
        '<section class="qc-provider">'
        '<div class="qc-provider-head">'
        f'<div><p class="qc-eyebrow">Provider</p><h2>{source}</h2></div>'
        f'{badge(primary_window.cache_state, cache_tone(primary_window))}'
        '</div>'
        f'{provider_kpis(primary_name, primary_window)}'
        f'{warnings}'
        f'{windows}'
        '</section>'
    )


def dashboard_overview(snapshots: list[NormalizedSnapshot]) -> str:
    """Render a top-level operational summary for all providers."""

    cards = []
    total_tokens = 0
    total_requests = 0
    active_providers = 0
    warnings = 0
    for snapshot in snapshots:
        if snapshot.errors:
            warnings += len(snapshot.errors)
            continue
        if not snapshot.windows:
            warnings += len(snapshot.warnings)
            continue
        _, window = primary_window_for(snapshot)
        total_tokens += window.total_tokens
        total_requests += window.requests
        active_providers += 1
        warnings += len(snapshot.warnings)
        cards.append(provider_strip(snapshot))

    return (
        '<section class="qc-overview">'
        '<div class="qc-overview-copy">'
        '<p class="qc-eyebrow">Local Usage</p>'
        '<h2>Quota dashboard</h2>'
        '<p>Provider usage, project share, and model mix from normalized local snapshots.</p>'
        '</div>'
        '<div class="qc-overview-metrics">'
        f'{metric_tile("Providers", str(active_providers), "enabled")}'
        f'{metric_tile("Tokens", compact_number(total_tokens), "local total")}'
        f'{metric_tile("Requests", compact_number(total_requests), "local total")}'
        f'{metric_tile("Notices", str(warnings), "warnings/errors")}'
        '</div>'
        f'<div class="qc-provider-strip">{"".join(cards)}</div>'
        '</section>'
    )


def window_panel(name: str, window: SnapshotWindow, *, compact: bool = False) -> str:
    """Render one normalized quota/session window."""

    title = html.escape(window_label(name))
    extra_class = " qc-window-compact" if compact else ""
    return (
        f'<article class="qc-window{extra_class}">'
        f'<header><div><p class="qc-eyebrow">Window</p><h3>{title}</h3></div>{badge(window.cache_state, cache_tone(window))}</header>'
        f'{usage_bar(window.utilization)}'
        f'{window_meta(window)}'
        f'{aggregate_table("Projects", window.by_project, kind="project")}'
        f'{aggregate_table("Models", window.by_model, kind="model")}'
        f'{runtime_section(window)}'
        '</article>'
    )


def usage_bar(utilization: float) -> str:
    """Render utilization bar."""

    pct = max(0.0, min(100.0, utilization * 100))
    tone = "hot" if pct >= 85 else "warm" if pct >= 65 else "cool"
    return '<div class="qc-bar qc-bar-%s" aria-label="usage"><span style="width: %.1f%%"></span></div>' % (tone, pct)


def aggregate_table(title: str, rows: dict[str, AggregateBreakdown], *, kind: str) -> str:
    """Render project/model aggregate table."""

    if not rows:
        return ""
    limited_rows = list(rows.items())[:TOP_ROW_LIMIT]
    body = "".join(_aggregate_row(name, aggregate, kind=kind) for name, aggregate in limited_rows)
    overflow = len(rows) - len(limited_rows)
    footer = f'<p class="qc-table-note">+{overflow} more</p>' if overflow > 0 else ""
    safe_title = html.escape(title)
    return (
        '<section class="qc-table-wrap">'
        f'<div class="qc-section-head"><h4>{safe_title}</h4>{footer}</div>'
        '<table class="qc-table"><thead><tr><th>Name</th><th>Tokens</th><th>Requests</th><th>Share</th></tr></thead>'
        f'<tbody>{body}</tbody></table></section>'
    )


def runtime_section(window: SnapshotWindow) -> str:
    """Render runtime usage for a window."""

    runtime = window.runtime
    if runtime.total_tokens <= 0 and runtime.requests <= 0 and not runtime.by_project:
        return ""
    return (
        '<section class="qc-runtime">'
        '<h4>Runtime</h4>'
        f'<p>{compact_number(runtime.total_tokens)} tokens / {runtime.requests:,} requests</p>'
        f'{aggregate_table("Runtime Projects", runtime.by_project, kind="project")}'
        '</section>'
    )


def stylesheet() -> str:
    """Return dashboard CSS."""

    return """
* { box-sizing: border-box; }
body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #18212f; background: #eef1f4; }
main { max-width: 1320px; margin: 0 auto; padding: 24px; }
h1 { margin: 0 0 18px; font-size: 26px; font-weight: 720; }
.qc-eyebrow { margin: 0 0 4px; color: #697586; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; }
.qc-overview { display: grid; grid-template-columns: minmax(240px, 1fr) 2fr; gap: 16px; margin-bottom: 18px; }
.qc-overview-copy, .qc-provider, .qc-overview-metrics, .qc-provider-strip article { background: #fff; border: 1px solid #d9e0e8; border-radius: 8px; box-shadow: 0 1px 2px rgba(18, 26, 38, .04); }
.qc-overview-copy { padding: 18px; }
.qc-overview-copy h2 { margin: 0 0 6px; font-size: 22px; }
.qc-overview-copy p:last-child { margin: 0; color: #536173; line-height: 1.5; }
.qc-overview-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0; overflow: hidden; }
.qc-metric { min-width: 0; padding: 16px; border-left: 1px solid #e5eaf0; }
.qc-metric:first-child { border-left: 0; }
.qc-metric dt { margin: 0; color: #697586; font-size: 12px; }
.qc-metric dd { margin: 5px 0 2px; font-size: 24px; font-weight: 760; }
.qc-metric small { color: #7c8796; }
.qc-provider-strip { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.qc-provider-strip article { padding: 14px; }
.qc-provider-strip h3 { margin: 0 0 10px; font-size: 16px; }
.qc-strip-row { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-top: 8px; color: #536173; }
.qc-strip-row strong { color: #18212f; }
.qc-provider { padding: 18px; margin-bottom: 18px; }
.qc-provider-head, .qc-window header, .qc-section-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.qc-provider h2, .qc-window h3, .qc-table-wrap h4, .qc-runtime h4 { margin: 0; }
.qc-provider h2 { font-size: 22px; text-transform: capitalize; }
.qc-kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin: 16px 0; }
.qc-kpi { min-width: 0; border: 1px solid #e3e8ef; border-radius: 6px; padding: 12px; background: #f9fafb; }
.qc-kpi dt { color: #697586; font-size: 12px; }
.qc-kpi dd { margin: 5px 0 0; font-weight: 720; overflow-wrap: anywhere; }
.qc-window { border-top: 1px solid #e5eaf0; padding-top: 16px; margin-top: 16px; }
.qc-window-compact { padding-top: 14px; }
.qc-badge { display: inline-flex; align-items: center; min-height: 22px; padding: 0 8px; border-radius: 999px; font-size: 12px; font-weight: 650; background: #eef2f5; color: #334155; white-space: nowrap; }
.qc-badge-success { background: #e5f4ea; color: #16633b; }
.qc-badge-warning { background: #fff1d6; color: #7a4b00; }
.qc-badge-danger { background: #ffe5e5; color: #9b1c1c; }
.qc-badge-muted { background: #f1f3f5; color: #6b7280; }
.qc-bar { height: 9px; background: #e5eaf0; border-radius: 999px; overflow: hidden; margin: 14px 0; }
.qc-bar span { display: block; height: 100%; }
.qc-bar-cool span { background: #287f71; }
.qc-bar-warm span { background: #c17a1b; }
.qc-bar-hot span { background: #c2413a; }
.qc-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 14px 0; }
.qc-metrics div { background: #f9fafb; border: 1px solid #e3e8ef; border-radius: 6px; padding: 10px; }
.qc-metrics dt { font-size: 12px; color: #697586; }
.qc-metrics dd { margin: 4px 0 0; font-weight: 700; overflow-wrap: anywhere; }
.qc-table-wrap { margin-top: 16px; }
.qc-table { width: 100%; border-collapse: collapse; table-layout: fixed; }
.qc-table th, .qc-table td { text-align: left; border-bottom: 1px solid #e5eaf0; padding: 8px 10px; font-size: 13px; vertical-align: middle; }
.qc-table th { color: #697586; font-weight: 700; }
.qc-table th:first-child, .qc-table td:first-child { width: 46%; }
.qc-name { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.qc-share { display: grid; grid-template-columns: 52px 1fr; align-items: center; gap: 8px; }
.qc-share-bar { height: 6px; border-radius: 999px; background: #e5eaf0; overflow: hidden; }
.qc-share-bar span { display: block; height: 100%; background: #4d6f91; }
.qc-table-note { margin: 0; color: #697586; font-size: 12px; }
.qc-runtime { margin-top: 16px; padding: 12px; border: 1px solid #e3e8ef; border-radius: 6px; background: #f9fafb; }
.qc-runtime p { margin: 6px 0 0; color: #536173; }
.qc-warning { margin: 12px 0 0; color: #7a4b00; }
@media (max-width: 900px) {
  main { padding: 16px; }
  .qc-overview, .qc-provider-strip { grid-template-columns: 1fr; }
  .qc-overview-metrics, .qc-kpis, .qc-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 620px) {
  .qc-overview-metrics, .qc-kpis, .qc-metrics { grid-template-columns: 1fr; }
  .qc-table th:nth-child(3), .qc-table td:nth-child(3) { display: none; }
  .qc-table th:first-child, .qc-table td:first-child { width: 54%; }
}
""".strip()


def primary_window_for(snapshot: NormalizedSnapshot) -> tuple[str, SnapshotWindow]:
    """Return the most operationally useful window for a provider."""

    for name in ("five_hour", "current_quota", "local_all", "today", "seven_day"):
        if name in snapshot.windows:
            return name, snapshot.windows[name]
    return next(iter(snapshot.windows.items()))


def provider_strip(snapshot: NormalizedSnapshot) -> str:
    """Render a compact provider status card."""

    name, window = primary_window_for(snapshot)
    source = html.escape(snapshot.source.title())
    return (
        '<article>'
        f'<h3>{source}</h3>'
        f'{usage_bar(window.utilization)}'
        f'<div class="qc-strip-row"><span>{html.escape(window_label(name))}</span><strong>{percent(window.utilization)}</strong></div>'
        f'<div class="qc-strip-row"><span>Tokens</span><strong>{compact_number(window.total_tokens)}</strong></div>'
        '</article>'
    )


def provider_kpis(window_name: str, window: SnapshotWindow) -> str:
    """Render provider KPI tiles."""

    return (
        '<dl class="qc-kpis">'
        f'{kpi("Window", window_label(window_name))}'
        f'{kpi("Utilization", percent(window.utilization))}'
        f'{kpi("Tokens", compact_number(window.total_tokens))}'
        f'{kpi("Requests", f"{window.requests:,}")}'
        f'{kpi("Reset", reset_label(window))}'
        '</dl>'
    )


def window_meta(window: SnapshotWindow) -> str:
    """Render window metrics."""

    return (
        '<dl class="qc-metrics">'
        f'{metric_block("Tokens", compact_number(window.total_tokens))}'
        f'{metric_block("Requests", f"{window.requests:,}")}'
        f'{metric_block("Utilization", percent(window.utilization))}'
        f'{metric_block("Pace", pace_label(window))}'
        '</dl>'
    )


def kpi(label: str, value: str) -> str:
    return f'<div class="qc-kpi"><dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd></div>'


def metric_block(label: str, value: str) -> str:
    return f'<div><dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd></div>'


def metric_tile(label: str, value: str, detail: str) -> str:
    return f'<dl class="qc-metric"><dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd><small>{html.escape(detail)}</small></dl>'


def _aggregate_row(name: str, aggregate: AggregateBreakdown, *, kind: str) -> str:
    label = display_name(name, kind=kind)
    safe_name = html.escape(label)
    raw_name = html.escape(name)
    share = max(0.0, min(100.0, aggregate.share_pct))
    return (
        "<tr>"
        f'<td><span class="qc-name" title="{raw_name}">{safe_name}</span></td>'
        f"<td>{compact_number(aggregate.total_tokens)}</td>"
        f"<td>{aggregate.requests:,}</td>"
        f'<td><span class="qc-share"><span>{aggregate.share_pct:.1f}%</span><span class="qc-share-bar"><span style="width: {share:.1f}%"></span></span></span></td>'
        "</tr>"
    )


def cache_tone(window: SnapshotWindow) -> str:
    if window.stale:
        return "warning"
    if window.cache_state == "live":
        return "success"
    if window.cache_state == "stale":
        return "warning"
    return "neutral"


def window_label(name: str) -> str:
    labels = {
        "five_hour": "5 hour",
        "seven_day": "7 day",
        "local_all": "Local all",
        "current_quota": "Current quota",
        "this_month": "This month",
    }
    return labels.get(name, name.replace("_", " ").title())


def compact_number(value: int) -> str:
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,}"


def percent(utilization: float) -> str:
    return f"{utilization * 100:.1f}%"


def reset_label(window: SnapshotWindow) -> str:
    if not window.resets_at:
        return "--"
    remaining = int(window.resets_at - time.time())
    if remaining <= 0:
        return "reset"
    if remaining < 3600:
        return f"{remaining // 60}m"
    hours, minutes = divmod(remaining // 60, 60)
    if hours < 48:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return datetime.fromtimestamp(window.resets_at).strftime("%b %-d")


def pace_label(window: SnapshotWindow) -> str:
    if not window.window_start or not window.resets_at or window.resets_at <= window.window_start:
        return "--"
    elapsed = time.time() - window.window_start
    duration = window.resets_at - window.window_start
    if elapsed <= 0 or elapsed > duration:
        return "--"
    expected = elapsed / duration
    delta = window.utilization - expected
    if delta > 0.03:
        return f"+{delta * 100:.0f}pt fast"
    if delta < -0.03:
        return f"{delta * 100:.0f}pt spare"
    return "on pace"


def display_name(name: str, *, kind: str) -> str:
    if kind != "project":
        return name
    normalized = name.replace("\\", "/")
    if "/" in normalized:
        parts = [part for part in normalized.split("/") if part]
        return parts[-1] if parts else name
    if not normalized.startswith("-"):
        return normalized
    parts = [part for part in normalized.split("-") if part]
    for marker in ("instances", "repos", "work", "projects"):
        if marker in parts:
            index = parts.index(marker) + 1
            if index < len(parts):
                return "-".join(parts[index:])
    return "-".join(parts[-2:]) if len(parts) >= 2 else normalized
