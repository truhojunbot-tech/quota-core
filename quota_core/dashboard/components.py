"""Reusable dashboard component helpers."""

from __future__ import annotations

from datetime import datetime
import html
import time

from quota_core.snapshot import AggregateBreakdown, NormalizedSnapshot, SnapshotWindow
from quota_core.dashboard.view_model import (
    DashboardWindow,
    ProviderDashboard,
    build_dashboard,
    build_provider_dashboard,
    data_state_label,
    iter_quota_windows,
    next_reset_window,
    pressure_windows,
    window_is_quota,
)

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

    provider = build_provider_dashboard(snapshot)

    source = html.escape(snapshot.source)
    if snapshot.errors:
        body = "".join(f"<p>{html.escape(error)}</p>" for error in snapshot.errors)
        return f'<section class="qc-provider qc-provider-error"><h2>{source}</h2>{badge("error", "danger")}{body}</section>'
    if not snapshot.windows:
        return f'<section class="qc-provider qc-provider-empty"><h2>{source}</h2>{badge("empty", "muted")}</section>'

    if provider.primary is None:
        return f'<section class="qc-provider qc-provider-empty"><h2>{source}</h2>{badge("empty", "muted")}</section>'
    primary = provider.primary
    windows = provider_windows(provider)
    warnings = "".join(f"<p class=\"qc-warning\">{html.escape(warning)}</p>" for warning in snapshot.warnings)
    return (
        '<section class="qc-provider">'
        '<div class="qc-provider-head">'
        f'<div><p class="qc-eyebrow">Provider</p><h2>{source}</h2></div>'
        f'{badge(primary.window.cache_state, cache_tone(primary.window))}'
        '</div>'
        f'{provider_kpis(primary)}'
        f'{warnings}'
        f'{windows}'
        '</section>'
    )


def dashboard_overview(snapshots: list[NormalizedSnapshot]) -> str:
    """Render a top-level operational summary for all providers."""

    providers = build_dashboard(snapshots)
    cards = []
    total_tokens = 0
    total_requests = 0
    active_providers = 0
    quota_windows = 0
    warnings = 0
    for provider in providers:
        snapshot = provider.snapshot
        if snapshot.errors:
            warnings += len(snapshot.errors)
            continue
        if not snapshot.windows:
            warnings += len(snapshot.warnings)
            continue
        if provider.primary is None:
            warnings += len(snapshot.warnings)
            continue
        total_tokens += provider.primary.window.total_tokens
        total_requests += provider.primary.window.requests
        active_providers += 1
        quota_windows += sum(1 for item in provider.windows if item.is_quota)
        warnings += len(snapshot.warnings)
        cards.append(provider_strip(provider))

    return (
        '<section class="qc-overview">'
        '<div class="qc-overview-copy">'
        '<div class="qc-overview-title">'
        '<p class="qc-eyebrow">Operations</p>'
        '<h2>Quota control</h2>'
        '</div>'
        f'{command_center(providers)}'
        '</div>'
        '<div class="qc-overview-metrics">'
        f'{metric_tile("Providers", str(active_providers), "enabled")}'
        f'{metric_tile("Quota windows", str(quota_windows), "live/cached")}'
        f'{metric_tile("Shown tokens", compact_number(total_tokens), "selected windows")}'
        f'{metric_tile("Requests", compact_number(total_requests), "selected windows")}'
        f'{metric_tile("Notices", str(warnings), "warnings/errors")}'
        '</div>'
        f'<div class="qc-provider-strip">{"".join(cards)}</div>'
        f'{operations_report(providers)}'
        f'{quota_matrix(providers)}'
        f'{reset_schedule(providers)}'
        f'{attention_panel(providers)}'
        f'{operations_briefing(providers)}'
        '</section>'
    )


def operations_report(providers: tuple[ProviderDashboard, ...]) -> str:
    """Render the original operations report structure as visible provider cards."""

    cards = "".join(report_card(provider) for provider in providers if provider.primary is not None or provider.snapshot.errors)
    if not cards:
        return ""
    return f'<section class="qc-report"><h3>Operations report</h3><div class="qc-report-grid">{cards}</div></section>'


def report_card(provider: ProviderDashboard) -> str:
    source = html.escape(provider_report_title(provider.source))
    if provider.snapshot.errors:
        errors = "".join(f"<li>{html.escape(error)}</li>" for error in provider.snapshot.errors)
        return f'<article class="qc-report-card"><header><h4>{source}</h4>{badge("error", "danger")}</header><ul class="qc-report-apps">{errors}</ul></article>'
    windows = provider.comparison or ((provider.primary,) if provider.primary else ())
    body = "".join(report_window(item) for item in windows if item is not None)
    state = badge(provider.primary.window.cache_state, cache_tone(provider.primary.window)) if provider.primary else badge("empty", "muted")
    return f'<article class="qc-report-card"><header><h4>{source}</h4>{state}</header>{body}</article>'


def report_window(item: DashboardWindow) -> str:
    window = item.window
    bar = usage_bar(window.utilization) if item.is_quota else local_meter(window)
    apps = report_apps(window.by_project)
    runtime = report_runtime(window)
    return (
        '<section class="qc-report-window">'
        f'<div class="qc-report-window-head"><span>{html.escape(window_label(item.name))}</span><strong>{html.escape(report_window_value(item))}</strong></div>'
        f'{bar}'
        f'<p>{html.escape(report_window_meta(item))}</p>'
        f'{apps}'
        f'{runtime}'
        '</section>'
    )


def report_window_value(item: DashboardWindow) -> str:
    if item.is_quota:
        return percent(item.window.utilization)
    return "local history"


def report_window_meta(item: DashboardWindow) -> str:
    window = item.window
    parts = [f"{compact_number(window.total_tokens)} tokens"]
    if item.is_quota:
        parts.append(f"reset {quota_reset_text(window).replace('reset ', '')}")
        pace = pace_label(window)
        if pace != "--":
            parts.append(pace)
    else:
        parts.append(concentration_label(window))
    return " · ".join(parts)


def report_apps(rows: dict[str, AggregateBreakdown]) -> str:
    if not rows:
        return '<p class="qc-empty-list">No app usage in this window</p>'
    items = "".join(
        f'<li><span>{html.escape(display_name(name, kind="project"))}</span><strong>{aggregate.share_pct:.1f}%</strong></li>'
        for name, aggregate in list(rows.items())[:BRIEF_PROJECT_LIMIT]
    )
    return f'<ol class="qc-report-apps">{items}</ol>'


def report_runtime(window: SnapshotWindow) -> str:
    if not window.runtime.by_project:
        return ""
    items = "".join(
        f'<li><span>{html.escape(display_name(name, kind="project"))}</span><strong>{compact_number(aggregate.total_tokens)}</strong></li>'
        for name, aggregate in list(window.runtime.by_project.items())[:BRIEF_PROJECT_LIMIT]
    )
    return f'<div class="qc-report-runtime"><span>Runtime</span><ol>{items}</ol></div>'


def provider_report_title(source: str) -> str:
    titles = {
        "claude": "Claude Max",
        "codex": "Codex (ChatGPT Plus)",
        "gemini": "Gemini Code Assist",
    }
    return titles.get(source, source.title())


def provider_windows(provider: ProviderDashboard) -> str:
    """Render provider windows, pairing short and weekly quota views when present."""

    panels = []
    paired_names = {item.name for item in provider.comparison}
    if provider.comparison:
        paired = "".join(window_panel(item, project_title="Apps") for item in provider.comparison)
        panels.append(f'<div class="qc-quota-split">{paired}</div>')
    for item in provider.details:
        if item.name in paired_names:
            continue
        primary_name = provider.primary.name if provider.primary else ""
        panels.append(window_panel(item, compact=item.name != primary_name))
    return "".join(panels)


def window_panel(item: DashboardWindow, *, compact: bool = False, project_title: str = "Projects") -> str:
    """Render one normalized quota/session window."""

    name = item.name
    window = item.window
    title = html.escape(window_label(name))
    extra_class = " qc-window-compact" if compact else ""
    bar = usage_bar(window.utilization) if item.is_quota else local_meter(window)
    return (
        f'<article class="qc-window{extra_class}">'
        f'<header><div><p class="qc-eyebrow">Window</p><h3>{title}</h3></div>{badge(window.cache_state, cache_tone(window))}</header>'
        f'{bar}'
        f'{window_meta(window)}'
        f'{window_context(name, window)}'
        f'{aggregate_table(project_title, window.by_project, kind="project", empty_label="No app usage in this window")}'
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


def command_center(providers: tuple[ProviderDashboard, ...]) -> str:
    """Render the most important operating state in one compact block."""

    pressure = pressure_windows(providers)
    highest = pressure[0] if pressure else None
    next_reset = next_reset_window(providers)
    data_state = data_state_label(providers)
    if highest is None:
        pressure_text = "No live quota pressure"
    else:
        source, item = highest
        pressure_text = f"{source.title()} {window_label(item.name)} {percent(item.window.utilization)}"
    if next_reset is None:
        reset_text = "No quota reset scheduled"
    else:
        source, item = next_reset
        reset_text = f"{source.title()} {window_label(item.name)} resets {reset_label(item.window)}"
    return (
        '<div class="qc-command-center">'
        f'<div><span>Highest pressure</span><strong>{html.escape(pressure_text)}</strong></div>'
        f'<div><span>Next reset</span><strong>{html.escape(reset_text)}</strong></div>'
        f'<div><span>Data state</span><strong>{html.escape(data_state)}</strong></div>'
        '</div>'
    )


def quota_matrix(providers: tuple[ProviderDashboard, ...]) -> str:
    """Render a compact 5h/7d comparison table across providers."""

    rows = []
    for provider in providers:
        for item in provider.windows:
            if not item.is_quota or item.name not in {"five_hour", "seven_day", "current_quota", "today"}:
                continue
            window = item.window
            top_project = top_aggregate_label(window.by_project, kind="project")
            rows.append(
                "<tr>"
                f"<td>{html.escape(provider.source.title())}</td>"
                f"<td>{html.escape(window_label(item.name))}</td>"
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


def reset_schedule(providers: tuple[ProviderDashboard, ...]) -> str:
    """Render upcoming quota reset order."""

    rows = []
    for source, name, window in sorted(
        ((source, item.name, item.window) for source, item in iter_quota_windows(providers) if item.window.resets_at),
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


def aggregate_table(title: str, rows: dict[str, AggregateBreakdown], *, kind: str, empty_label: str = "") -> str:
    """Render project/model aggregate table."""

    if not rows:
        if empty_label:
            safe_title = html.escape(title)
            safe_empty = html.escape(empty_label)
            return (
                '<section class="qc-table-wrap">'
                f'<div class="qc-section-head"><h4>{safe_title}</h4></div>'
                f'<p class="qc-empty-list">{safe_empty}</p>'
                '</section>'
            )
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
.qc-overview-copy, .qc-provider, .qc-overview-metrics, .qc-provider-strip article, .qc-report, .qc-attention, .qc-briefing, .qc-matrix, .qc-reset-schedule { background: #fff; border: 1px solid #d9e0e8; border-radius: 8px; box-shadow: 0 1px 2px rgba(18, 26, 38, .04); }
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
.qc-strip-window { margin-top: 10px; }
.qc-strip-window .qc-strip-row { margin-top: 0; }
.qc-strip-bar { height: 8px; margin-top: 6px; border-radius: 999px; overflow: hidden; background: #e5eaf0; }
.qc-strip-bar span { display: block; height: 100%; }
.qc-strip-bar-cool span { background: #287f71; }
.qc-strip-bar-warm span { background: #c17a1b; }
.qc-strip-bar-hot span { background: #c2413a; }
.qc-report { grid-column: 1 / -1; padding: 14px; }
.qc-report h3 { margin: 0; font-size: 16px; }
.qc-report-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
.qc-report-card { min-width: 0; border: 1px solid #e3e8ef; border-radius: 6px; overflow: hidden; background: #fcfdfe; }
.qc-report-card header { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 12px; border-bottom: 1px solid #e3e8ef; background: #f7f9fb; }
.qc-report-card h4 { margin: 0; font-size: 15px; }
.qc-report-window { padding: 10px 12px; border-top: 1px solid #edf1f5; }
.qc-report-card header + .qc-report-window { border-top: 0; }
.qc-report-window-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.qc-report-window-head span { color: #536173; }
.qc-report-window-head strong { font-size: 16px; overflow-wrap: anywhere; text-align: right; }
.qc-report-window .qc-bar, .qc-report-window .qc-local-meter { margin: 8px 0; }
.qc-report-window p { margin: 0 0 8px; color: #536173; font-size: 12px; overflow-wrap: anywhere; }
.qc-report-apps { margin: 0; padding: 0; list-style: none; }
.qc-report-apps li { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; padding: 3px 0; border-top: 1px solid #edf1f5; font-size: 13px; }
.qc-report-apps li:first-child { border-top: 0; }
.qc-report-apps span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.qc-report-apps strong { white-space: nowrap; }
.qc-report-runtime { margin-top: 8px; padding-top: 8px; border-top: 1px dashed #ccd5df; }
.qc-report-runtime > span { display: block; margin-bottom: 4px; color: #697586; font-size: 12px; font-weight: 700; }
.qc-report-runtime ol { margin: 0; padding: 0; list-style: none; }
.qc-report-runtime li { display: flex; justify-content: space-between; gap: 8px; padding: 2px 0; font-size: 12px; color: #536173; }
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
.qc-quota-split { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 16px; }
.qc-quota-split .qc-window { min-width: 0; margin-top: 0; padding: 14px; border: 1px solid #e3e8ef; border-radius: 8px; background: #fcfdfe; }
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
.qc-empty-list { margin: 8px 0 0; padding: 10px; border: 1px dashed #ccd5df; border-radius: 6px; color: #697586; font-size: 13px; background: #f9fafb; }
.qc-runtime { margin-top: 16px; padding: 12px; border: 1px solid #e3e8ef; border-radius: 6px; background: #f9fafb; }
.qc-runtime p { margin: 6px 0 0; color: #536173; }
.qc-warning { margin: 12px 0 0; color: #7a4b00; }
@media (max-width: 900px) {
  main { padding: 16px; }
        .qc-overview-copy, .qc-provider-strip, .qc-report-grid, .qc-brief-grid, .qc-attention ol, .qc-reset-schedule ol, .qc-quota-split { grid-template-columns: 1fr; }
        .qc-command-center { grid-template-columns: 1fr; }
        .qc-overview-metrics, .qc-kpis, .qc-metrics, .qc-window-context { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 620px) {
    .qc-overview-metrics, .qc-kpis, .qc-metrics, .qc-window-context { grid-template-columns: 1fr; }
  .qc-table th:nth-child(3), .qc-table td:nth-child(3) { display: none; }
  .qc-table th:first-child, .qc-table td:first-child { width: 54%; }
}
""".strip()


def is_quota_window(name: str, window: SnapshotWindow) -> bool:
    return window_is_quota(name, window)


def provider_strip(provider: ProviderDashboard) -> str:
    """Render a compact provider status card."""

    if provider.primary is None:
        return ""
    source = html.escape(provider.source.title())
    rows = provider_strip_rows(provider)
    fallback_meter = "" if provider.comparison else local_meter(provider.primary.window)
    return (
        '<article>'
        f'<h3>{source}</h3>'
        f'{fallback_meter}'
        f'{rows}'
        '</article>'
    )


def provider_strip_rows(provider: ProviderDashboard) -> str:
    """Render compact provider rows without hiding the short quota window."""

    rows = []
    for item in provider.comparison:
        window = item.window
        if item.is_quota:
            value = f"{percent(window.utilization)} · {compact_number(window.total_tokens)} · {quota_reset_text(window)}"
        else:
            value = f"local history · {compact_number(window.total_tokens)}"
        rows.append(strip_window_row(window_label(item.name), value, window))
    if rows:
        return "".join(rows)
    if provider.primary is None:
        return ""
    quota = provider.primary.is_quota
    fallback_window = provider.primary.window
    status = percent(fallback_window.utilization) if quota else "local history"
    token_label = "Used" if quota else "Tokens"
    return strip_row(window_label(provider.primary.name), status) + strip_row(token_label, compact_number(fallback_window.total_tokens))


def strip_row(label: str, value: str) -> str:
    return f'<div class="qc-strip-row"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'


def strip_window_row(label: str, value: str, window: SnapshotWindow) -> str:
    pct = max(0.0, min(100.0, window.utilization * 100))
    tone = pressure_tone(window.utilization)
    return (
        '<div class="qc-strip-window">'
        f'<div class="qc-strip-row"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
        f'<div class="qc-strip-bar qc-strip-bar-{html.escape(tone)}" aria-label="{html.escape(label)} usage"><span style="width: {pct:.1f}%"></span></div>'
        '</div>'
    )


def provider_kpis(item: DashboardWindow) -> str:
    """Render provider KPI tiles."""

    window = item.window
    return (
        '<dl class="qc-kpis">'
        f'{kpi("Window", window_label(item.name))}'
        f'{kpi("Utilization", percent(window.utilization) if item.is_quota else "local history")}'
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


def attention_panel(providers: tuple[ProviderDashboard, ...]) -> str:
    items: list[tuple[str, str, str]] = []
    for provider in providers:
        source = provider.source.title()
        for error in provider.snapshot.errors:
            items.append(("high", source, error))
        for warning in provider.snapshot.warnings:
            items.append(("medium", source, warning))
        for item in provider.windows:
            window = item.window
            if item.is_quota:
                if window.utilization >= 0.85:
                    items.append(("high", source, f"{window_label(item.name)} at {percent(window.utilization)}"))
                elif window.utilization >= 0.65:
                    items.append(("medium", source, f"{window_label(item.name)} at {percent(window.utilization)}"))
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


def operations_briefing(providers: tuple[ProviderDashboard, ...]) -> str:
    cards = "".join(briefing_card(provider) for provider in providers)
    return f'<section class="qc-briefing"><h3>Operations briefing</h3><div class="qc-brief-grid">{cards}</div></section>'


def briefing_card(provider: ProviderDashboard) -> str:
    source = html.escape(provider.source)
    if provider.snapshot.errors:
        errors = "".join(f"<li>{html.escape(error)}</li>" for error in provider.snapshot.errors)
        return f'<article class="qc-brief-card"><header><h4>{source}</h4>{badge("error", "danger")}</header><ul class="qc-brief-projects">{errors}</ul></article>'
    if not provider.windows:
        return f'<article class="qc-brief-card"><header><h4>{source}</h4>{badge("empty", "muted")}</header></article>'

    lines = []
    ordered = list(provider.comparison) + [item for item in provider.details if item.name == "local_all"]
    for item in ordered:
        label = window_label(item.name)
        value = quota_brief(item.window) if item.is_quota else local_brief(item.window)
        lines.append(f'<div><dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd></div>')
    if not lines:
        primary = provider.primary
        if primary is not None:
            lines.append(f'<div><dt>{html.escape(window_label(primary.name))}</dt><dd>{html.escape(local_brief(primary.window))}</dd></div>')

    primary = provider.primary
    if primary is None:
        return f'<article class="qc-brief-card"><header><h4>{source}</h4>{badge("empty", "muted")}</header></article>'
    projects = "".join(
        f'<li>{html.escape(display_name(name, kind="project"))}: {aggregate.share_pct:.1f}%</li>'
        for name, aggregate in list(primary.window.by_project.items())[:BRIEF_PROJECT_LIMIT]
    )
    projects_block = f'<ol class="qc-brief-projects">{projects}</ol>' if projects else ""
    return (
        '<article class="qc-brief-card">'
        f'<header><h4>{source}</h4>{badge(primary.window.cache_state, cache_tone(primary.window))}</header>'
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
