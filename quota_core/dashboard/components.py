"""Reusable dashboard component helpers."""

from __future__ import annotations

from datetime import datetime
import html
import time

from quota_core.snapshot import AggregateBreakdown, NormalizedSnapshot, SnapshotWindow

TOP_ROW_LIMIT = 12
BRIEF_PROJECT_LIMIT = 5
RESET_ROW_LIMIT = 8


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
    quota_windows = 0
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
        quota_windows += sum(1 for name, item in snapshot.windows.items() if is_quota_window(name, item))
        warnings += len(snapshot.warnings)
        cards.append(provider_strip(snapshot))

    return (
        '<section class="qc-overview">'
        '<div class="qc-overview-copy">'
        '<div class="qc-overview-title">'
        '<p class="qc-eyebrow">Operations</p>'
        '<h2>Quota control</h2>'
        '</div>'
        f'{command_center(snapshots)}'
        '</div>'
        '<div class="qc-overview-metrics">'
        f'{metric_tile("Providers", str(active_providers), "enabled")}'
        f'{metric_tile("Quota windows", str(quota_windows), "live/cached")}'
        f'{metric_tile("Shown tokens", compact_number(total_tokens), "selected windows")}'
        f'{metric_tile("Requests", compact_number(total_requests), "selected windows")}'
        f'{metric_tile("Notices", str(warnings), "warnings/errors")}'
        '</div>'
        f'<div class="qc-provider-strip">{"".join(cards)}</div>'
        f'{quota_matrix(snapshots)}'
        f'{reset_schedule(snapshots)}'
        f'{attention_panel(snapshots)}'
        f'{operations_briefing(snapshots)}'
        '</section>'
    )


def window_panel(name: str, window: SnapshotWindow, *, compact: bool = False) -> str:
    """Render one normalized quota/session window."""

    title = html.escape(window_label(name))
    extra_class = " qc-window-compact" if compact else ""
    quota = is_quota_window(name, window)
    bar = usage_bar(window.utilization) if quota else local_meter(window)
    return (
        f'<article class="qc-window{extra_class}">'
        f'<header><div><p class="qc-eyebrow">Window</p><h3>{title}</h3></div>{badge(window.cache_state, cache_tone(window))}</header>'
        f'{bar}'
        f'{window_meta(window)}'
        f'{window_context(name, window)}'
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


def local_meter(window: SnapshotWindow) -> str:
    """Render a local-history meter without pretending it is quota utilization."""

    leader = next(iter(window.by_project.values()), AggregateBreakdown())
    share = max(0.0, min(100.0, leader.share_pct))
    return (
        '<div class="qc-local-meter" aria-label="local history">'
        f'<span style="width: {share:.1f}%"></span>'
        f'<em>top project {share:.1f}% share</em>'
        '</div>'
    )


def command_center(snapshots: list[NormalizedSnapshot]) -> str:
    """Render the most important operating state in one compact block."""

    pressure = pressure_windows(snapshots)
    highest = pressure[0] if pressure else None
    next_reset = next_reset_window(snapshots)
    data_state = data_state_label(snapshots)
    if highest is None:
        pressure_text = "No live quota pressure"
    else:
        source, name, window = highest
        pressure_text = f"{source.title()} {window_label(name)} {percent(window.utilization)}"
    if next_reset is None:
        reset_text = "No quota reset scheduled"
    else:
        source, name, window = next_reset
        reset_text = f"{source.title()} {window_label(name)} resets {reset_label(window)}"
    return (
        '<div class="qc-command-center">'
        f'<div><span>Highest pressure</span><strong>{html.escape(pressure_text)}</strong></div>'
        f'<div><span>Next reset</span><strong>{html.escape(reset_text)}</strong></div>'
        f'<div><span>Data state</span><strong>{html.escape(data_state)}</strong></div>'
        '</div>'
    )


def quota_matrix(snapshots: list[NormalizedSnapshot]) -> str:
    """Render a compact 5h/7d comparison table across providers."""

    rows = []
    for snapshot in snapshots:
        for name in ("five_hour", "seven_day", "current_quota", "today"):
            window = snapshot.windows.get(name)
            if window is None or not is_quota_window(name, window):
                continue
            top_project = top_aggregate_label(window.by_project, kind="project")
            rows.append(
                "<tr>"
                f"<td>{html.escape(snapshot.source.title())}</td>"
                f"<td>{html.escape(window_label(name))}</td>"
                f'<td><span class="qc-pressure qc-pressure-{pressure_tone(window.utilization)}">{percent(window.utilization)}</span></td>'
                f"<td>{compact_number(window.total_tokens)}</td>"
                f"<td>{html.escape(reset_label(window))}</td>"
                f"<td>{html.escape(pace_label(window))}</td>"
                f"<td>{html.escape(top_project)}</td>"
                f"<td>{badge(window.cache_state, cache_tone(window))}</td>"
                "</tr>"
            )
    if not rows:
        return ""
    return (
        '<section class="qc-matrix">'
        '<div class="qc-section-head"><h3>Quota matrix</h3><p class="qc-table-note">5h and 7d quota windows</p></div>'
        '<table class="qc-table qc-matrix-table"><thead><tr>'
        '<th>Provider</th><th>Window</th><th>Used</th><th>Tokens</th><th>Reset</th><th>Pace</th><th>Top project</th><th>State</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
        '</section>'
    )


def reset_schedule(snapshots: list[NormalizedSnapshot]) -> str:
    """Render upcoming quota reset order."""

    rows = []
    for source, name, window in sorted(
        (item for item in iter_quota_windows(snapshots) if item[2].resets_at),
        key=lambda item: item[2].resets_at or 0,
    )[:RESET_ROW_LIMIT]:
        rows.append(
            '<li>'
            f'<strong>{html.escape(source.title())} {html.escape(window_label(name))}</strong>'
            f'<span>{html.escape(reset_label(window))} · {html.escape(window_range(window))} · {percent(window.utilization)}</span>'
            '</li>'
        )
    if not rows:
        return ""
    return f'<section class="qc-reset-schedule"><h3>Reset schedule</h3><ol>{"".join(rows)}</ol></section>'


def window_context(name: str, window: SnapshotWindow) -> str:
    """Render extra context that helps explain a window beyond the main KPIs."""

    quota = is_quota_window(name, window)
    context = [
        ("Window range", window_range(window) if quota else "local history"),
        ("Sampled", sampled_label(window)),
        ("Top project", top_aggregate_label(window.by_project, kind="project")),
        ("Top model", top_aggregate_label(window.by_model, kind="model")),
    ]
    return '<dl class="qc-window-context">' + "".join(metric_block(label, value) for label, value in context) + '</dl>'


def iter_quota_windows(snapshots: list[NormalizedSnapshot]) -> list[tuple[str, str, SnapshotWindow]]:
    windows: list[tuple[str, str, SnapshotWindow]] = []
    for snapshot in snapshots:
        for name, window in snapshot.windows.items():
            if is_quota_window(name, window):
                windows.append((snapshot.source, name, window))
    return windows


def pressure_windows(snapshots: list[NormalizedSnapshot]) -> list[tuple[str, str, SnapshotWindow]]:
    return sorted(iter_quota_windows(snapshots), key=lambda item: item[2].utilization, reverse=True)


def next_reset_window(snapshots: list[NormalizedSnapshot]) -> tuple[str, str, SnapshotWindow] | None:
    candidates = [item for item in iter_quota_windows(snapshots) if item[2].resets_at and item[2].resets_at > time.time()]
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[2].resets_at or 0)


def data_state_label(snapshots: list[NormalizedSnapshot]) -> str:
    states = [window.cache_state for snapshot in snapshots for window in snapshot.windows.values()]
    if not states:
        return "no data"
    cached = sum(1 for state in states if state in {"cached", "stale"})
    live = sum(1 for state in states if state == "live")
    if cached and live:
        return f"{live} live / {cached} cached"
    if cached:
        return f"{cached} cached"
    return f"{live} live"


def top_aggregate_label(rows: dict[str, AggregateBreakdown], *, kind: str) -> str:
    item = next(iter(rows.items()), None)
    if item is None:
        return "--"
    name, aggregate = item
    return f"{display_name(name, kind=kind)} {aggregate.share_pct:.1f}%"


def window_range(window: SnapshotWindow) -> str:
    if not window.window_start or not window.resets_at:
        return "--"
    start = datetime.fromtimestamp(window.window_start).strftime("%b %-d %H:%M")
    end = datetime.fromtimestamp(window.resets_at).strftime("%b %-d %H:%M")
    return f"{start} to {end}"


def sampled_label(window: SnapshotWindow) -> str:
    if not window.window_end:
        return "--"
    return datetime.fromtimestamp(window.window_end).strftime("%b %-d %H:%M")


def pressure_tone(utilization: float) -> str:
    pct = utilization * 100
    if pct >= 85:
        return "hot"
    if pct >= 65:
        return "warm"
    return "cool"


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
main { max-width: 1380px; margin: 0 auto; padding: 22px; }
h1 { margin: 0 0 16px; font-size: 24px; font-weight: 760; }
.qc-eyebrow { margin: 0 0 4px; color: #697586; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; }
.qc-overview { display: grid; grid-template-columns: 1fr; gap: 14px; align-items: start; margin-bottom: 18px; }
.qc-overview-copy, .qc-provider, .qc-overview-metrics, .qc-provider-strip article, .qc-attention, .qc-briefing, .qc-matrix, .qc-reset-schedule { background: #fff; border: 1px solid #d9e0e8; border-radius: 8px; box-shadow: 0 1px 2px rgba(18, 26, 38, .04); }
.qc-overview-copy { display: grid; grid-template-columns: 220px 1fr; gap: 16px; align-items: center; padding: 18px; }
.qc-overview-title h2 { margin: 0; font-size: 22px; }
.qc-command-center { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.qc-command-center div { border: 1px solid #e3e8ef; border-radius: 6px; padding: 10px; background: #f9fafb; }
.qc-command-center span { display: block; color: #697586; font-size: 12px; }
.qc-command-center strong { display: block; margin-top: 4px; font-size: 14px; overflow-wrap: anywhere; }
.qc-overview-metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 0; align-self: start; overflow: hidden; }
.qc-metric { min-width: 0; padding: 16px; border-left: 1px solid #e5eaf0; }
.qc-metric:first-child { border-left: 0; }
.qc-metric dt { margin: 0; color: #697586; font-size: 12px; }
.qc-metric dd { margin: 5px 0 2px; font-size: 24px; font-weight: 760; }
.qc-metric small { color: #7c8796; }
.qc-provider-strip { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.qc-provider-strip article { padding: 14px; }
.qc-provider-strip h3 { margin: 0 0 10px; font-size: 16px; }
.qc-strip-row { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-top: 8px; color: #536173; }
.qc-strip-row strong { color: #18212f; text-align: right; overflow-wrap: anywhere; }
.qc-matrix { grid-column: 1 / -1; padding: 14px; overflow-x: auto; }
.qc-matrix h3, .qc-reset-schedule h3 { margin: 0; font-size: 16px; }
.qc-matrix-table { min-width: 1040px; table-layout: auto; }
.qc-matrix-table th:first-child, .qc-matrix-table td:first-child { width: auto; }
.qc-matrix-table th, .qc-matrix-table td { white-space: nowrap; }
.qc-matrix-table th:nth-child(7), .qc-matrix-table td:nth-child(7) { min-width: 240px; white-space: normal; overflow-wrap: anywhere; }
.qc-matrix-table th:nth-child(8), .qc-matrix-table td:nth-child(8) { width: 92px; text-align: right; }
.qc-pressure { display: inline-flex; align-items: center; min-height: 22px; padding: 0 8px; border-radius: 999px; font-weight: 720; }
.qc-pressure-cool { background: #e4f2ef; color: #176355; }
.qc-pressure-warm { background: #fff1d6; color: #7a4b00; }
.qc-pressure-hot { background: #ffe5e5; color: #9b1c1c; }
.qc-reset-schedule { grid-column: 1 / -1; padding: 14px; }
.qc-reset-schedule ol { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 12px 0 0; padding: 0; list-style: none; }
.qc-reset-schedule li { border: 1px solid #e3e8ef; border-radius: 6px; padding: 10px; background: #f9fafb; min-width: 0; }
.qc-reset-schedule strong, .qc-reset-schedule span { display: block; overflow-wrap: anywhere; }
.qc-reset-schedule span { margin-top: 4px; color: #536173; font-size: 12px; }
.qc-attention { grid-column: 1 / -1; padding: 14px; }
.qc-attention h3, .qc-briefing h3 { margin: 0; font-size: 16px; }
.qc-attention ol { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; list-style: none; margin: 12px 0 0; padding: 0; }
.qc-attention li { min-width: 0; border: 1px solid #e3e8ef; border-radius: 6px; padding: 10px; background: #f9fafb; }
.qc-attention strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.qc-attention span { display: block; margin-top: 4px; color: #536173; font-size: 12px; }
.qc-risk-high { border-color: #f0b4af !important; background: #fff5f5 !important; }
.qc-risk-medium { border-color: #ead29d !important; background: #fffaf0 !important; }
.qc-risk-low { border-color: #c8d9d4 !important; background: #f2fbf7 !important; }
.qc-briefing { grid-column: 1 / -1; padding: 14px; }
.qc-brief-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
.qc-brief-card { border: 1px solid #e3e8ef; border-radius: 6px; overflow: hidden; }
.qc-brief-card header { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 10px 12px; background: #f7f9fb; border-bottom: 1px solid #e3e8ef; }
.qc-brief-card h4 { margin: 0; text-transform: capitalize; }
.qc-brief-lines { margin: 0; padding: 10px 12px; }
.qc-brief-lines div { display: grid; grid-template-columns: 82px 1fr; gap: 8px; padding: 4px 0; font-size: 13px; }
.qc-brief-lines dt { color: #697586; }
.qc-brief-lines dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.qc-brief-projects { margin: 0; padding: 0 12px 12px 28px; color: #536173; font-size: 13px; }
.qc-brief-projects li { padding: 2px 0; }
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
.qc-local-meter { position: relative; height: 26px; background: #eef2f5; border-radius: 6px; overflow: hidden; margin: 14px 0; }
.qc-local-meter span { display: block; height: 100%; background: #c8d7e1; }
.qc-local-meter em { position: absolute; inset: 0; display: flex; align-items: center; padding: 0 10px; color: #334155; font-size: 12px; font-style: normal; font-weight: 650; }
.qc-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 14px 0; }
.qc-metrics div { background: #f9fafb; border: 1px solid #e3e8ef; border-radius: 6px; padding: 10px; }
.qc-metrics dt { font-size: 12px; color: #697586; }
.qc-metrics dd { margin: 4px 0 0; font-weight: 700; overflow-wrap: anywhere; }
.qc-window-context { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 14px 0; }
.qc-window-context div { background: #fff; border: 1px solid #e3e8ef; border-radius: 6px; padding: 10px; }
.qc-window-context dt { font-size: 12px; color: #697586; }
.qc-window-context dd { margin: 4px 0 0; font-weight: 650; overflow-wrap: anywhere; }
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
        .qc-overview-copy, .qc-provider-strip, .qc-brief-grid, .qc-attention ol, .qc-reset-schedule ol { grid-template-columns: 1fr; }
        .qc-command-center { grid-template-columns: 1fr; }
        .qc-overview-metrics, .qc-kpis, .qc-metrics, .qc-window-context { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 620px) {
    .qc-overview-metrics, .qc-kpis, .qc-metrics, .qc-window-context { grid-template-columns: 1fr; }
  .qc-table th:nth-child(3), .qc-table td:nth-child(3) { display: none; }
  .qc-table th:first-child, .qc-table td:first-child { width: 54%; }
}
""".strip()


def primary_window_for(snapshot: NormalizedSnapshot) -> tuple[str, SnapshotWindow]:
    """Return the most operationally useful window for a provider."""

    quota_windows = [
        (name, snapshot.windows[name])
        for name in ("five_hour", "current_quota", "seven_day", "today")
        if name in snapshot.windows and is_quota_window(name, snapshot.windows[name])
    ]
    if quota_windows:
        return max(quota_windows, key=lambda item: item[1].utilization)
    if "local_all" in snapshot.windows:
        return "local_all", snapshot.windows["local_all"]
    return next(iter(snapshot.windows.items()))


def is_quota_window(name: str, window: SnapshotWindow) -> bool:
    return name != "local_all" and (window.resets_at is not None or window.window_start is not None or window.utilization > 0)


def provider_strip(snapshot: NormalizedSnapshot) -> str:
    """Render a compact provider status card."""

    name, window = primary_window_for(snapshot)
    source = html.escape(snapshot.source.title())
    quota = is_quota_window(name, window)
    meter = usage_bar(window.utilization) if quota else local_meter(window)
    rows = provider_strip_rows(snapshot, fallback_name=name, fallback_window=window)
    return (
        '<article>'
        f'<h3>{source}</h3>'
        f'{meter}'
        f'{rows}'
        '</article>'
    )


def provider_strip_rows(snapshot: NormalizedSnapshot, *, fallback_name: str, fallback_window: SnapshotWindow) -> str:
    """Render compact provider rows without hiding the short quota window."""

    rows = []
    for name in ("five_hour", "seven_day"):
        window = snapshot.windows.get(name)
        if window is None or not is_quota_window(name, window):
            continue
        value = f"{percent(window.utilization)} · {compact_number(window.total_tokens)} · {quota_reset_text(window)}"
        rows.append(strip_row(window_label(name), value))
    if rows:
        return "".join(rows)
    quota = is_quota_window(fallback_name, fallback_window)
    status = percent(fallback_window.utilization) if quota else "local history"
    token_label = "Used" if quota else "Tokens"
    return strip_row(window_label(fallback_name), status) + strip_row(token_label, compact_number(fallback_window.total_tokens))


def strip_row(label: str, value: str) -> str:
    return f'<div class="qc-strip-row"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'


def provider_kpis(window_name: str, window: SnapshotWindow) -> str:
    """Render provider KPI tiles."""

    quota = is_quota_window(window_name, window)
    return (
        '<dl class="qc-kpis">'
        f'{kpi("Window", window_label(window_name))}'
        f'{kpi("Utilization", percent(window.utilization) if quota else "local history")}'
        f'{kpi("Tokens", compact_number(window.total_tokens))}'
        f'{kpi("Requests", f"{window.requests:,}")}'
        f'{kpi("Reset", reset_label(window))}'
        '</dl>'
    )


def window_meta(window: SnapshotWindow) -> str:
    """Render window metrics."""

    quota = window.resets_at is not None or window.window_start is not None or window.utilization > 0
    return (
        '<dl class="qc-metrics">'
        f'{metric_block("Tokens", compact_number(window.total_tokens))}'
        f'{metric_block("Requests", f"{window.requests:,}")}'
        f'{metric_block("Utilization", percent(window.utilization) if quota else "local history")}'
        f'{metric_block("Pace", pace_label(window) if quota else concentration_label(window))}'
        '</dl>'
    )


def attention_panel(snapshots: list[NormalizedSnapshot]) -> str:
    items: list[tuple[str, str, str]] = []
    for snapshot in snapshots:
        source = snapshot.source.title()
        for error in snapshot.errors:
            items.append(("high", source, error))
        for warning in snapshot.warnings:
            items.append(("medium", source, warning))
        for name, window in snapshot.windows.items():
            if is_quota_window(name, window):
                if window.utilization >= 0.85:
                    items.append(("high", source, f"{window_label(name)} at {percent(window.utilization)}"))
                elif window.utilization >= 0.65:
                    items.append(("medium", source, f"{window_label(name)} at {percent(window.utilization)}"))
            top_project = next(iter(window.by_project.items()), None)
            if top_project and top_project[1].share_pct >= 50:
                items.append(("medium", source, f"{display_name(top_project[0], kind='project')} owns {top_project[1].share_pct:.1f}%"))
    if not items:
        items.append(("low", "All providers", "No quota pressure or warnings in the current snapshot"))
    rows = "".join(
        f'<li class="qc-risk-{html.escape(tone)}"><strong>{html.escape(title)}</strong><span>{html.escape(detail)}</span></li>'
        for tone, title, detail in items[:6]
    )
    return f'<section class="qc-attention"><h3>Attention</h3><ol>{rows}</ol></section>'


def operations_briefing(snapshots: list[NormalizedSnapshot]) -> str:
    cards = "".join(briefing_card(snapshot) for snapshot in snapshots)
    return f'<section class="qc-briefing"><h3>Operations briefing</h3><div class="qc-brief-grid">{cards}</div></section>'


def briefing_card(snapshot: NormalizedSnapshot) -> str:
    source = html.escape(snapshot.source)
    if snapshot.errors:
        errors = "".join(f"<li>{html.escape(error)}</li>" for error in snapshot.errors)
        return f'<article class="qc-brief-card"><header><h4>{source}</h4>{badge("error", "danger")}</header><ul class="qc-brief-projects">{errors}</ul></article>'
    if not snapshot.windows:
        return f'<article class="qc-brief-card"><header><h4>{source}</h4>{badge("empty", "muted")}</header></article>'

    lines = []
    for name in ("five_hour", "seven_day", "current_quota", "local_all"):
        window = snapshot.windows.get(name)
        if window is None:
            continue
        label = window_label(name)
        value = quota_brief(window) if is_quota_window(name, window) else local_brief(window)
        lines.append(f'<div><dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd></div>')
    if not lines:
        name, window = primary_window_for(snapshot)
        lines.append(f'<div><dt>{html.escape(window_label(name))}</dt><dd>{html.escape(local_brief(window))}</dd></div>')

    _, primary = primary_window_for(snapshot)
    projects = "".join(
        f'<li>{html.escape(display_name(name, kind="project"))}: {aggregate.share_pct:.1f}%</li>'
        for name, aggregate in list(primary.by_project.items())[:BRIEF_PROJECT_LIMIT]
    )
    projects_block = f'<ol class="qc-brief-projects">{projects}</ol>' if projects else ""
    return (
        '<article class="qc-brief-card">'
        f'<header><h4>{source}</h4>{badge(primary.cache_state, cache_tone(primary))}</header>'
        f'<dl class="qc-brief-lines">{"".join(lines)}</dl>'
        f'{projects_block}'
        '</article>'
    )


def quota_brief(window: SnapshotWindow) -> str:
    pace = pace_label(window)
    suffix = f", {pace}" if pace != "--" else ""
    return f"{percent(window.utilization)} · {quota_reset_text(window)} · {compact_number(window.total_tokens)} tokens{suffix}"


def quota_reset_text(window: SnapshotWindow) -> str:
    label = reset_label(window)
    if label == "--":
        return "no reset"
    if label == "reset":
        return "reset now"
    return f"reset {label}"


def local_brief(window: SnapshotWindow) -> str:
    return f"{compact_number(window.total_tokens)} tokens · {window.requests:,} requests · {concentration_label(window)}"


def concentration_label(window: SnapshotWindow) -> str:
    top_project = next(iter(window.by_project.values()), None)
    if top_project is None:
        return "no project mix"
    if top_project.share_pct >= 50:
        return f"top-heavy {top_project.share_pct:.0f}%"
    if top_project.share_pct >= 25:
        return f"focused {top_project.share_pct:.0f}%"
    return f"spread {top_project.share_pct:.0f}% top"


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
