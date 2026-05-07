"""Dashboard components rendered from normalized quota snapshots."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import html
import time

from quota_core.dashboard.view_model import DashboardWindow, ProviderDashboard, build_dashboard
from quota_core.snapshot import AggregateBreakdown, NormalizedSnapshot, RuntimeBreakdown, SnapshotQuotaGroup, SnapshotWindow

KST = timezone(timedelta(hours=9))
TOP_ROWS = 8


def dashboard_overview(snapshots: list[NormalizedSnapshot]) -> str:
    providers = build_dashboard(snapshots)
    if not providers:
        return '<section class="panel"><h2>No snapshots</h2></section>'
    sections = (
        overall_status(providers),
        runtime_usage_report(providers),
        provider_panels(providers),
        claude_session_panel(providers),
        time_series_panel(providers),
        quota_history_panel(providers),
        detail_windows(providers),
        data_state_panel(providers),
    )
    return "".join(section for section in sections if section)


def provider_summary(snapshot: NormalizedSnapshot) -> str:
    provider = build_dashboard([snapshot])[0]
    return detail_provider_card(provider)


def provider_panels(providers: tuple[ProviderDashboard, ...]) -> str:
    return "".join(provider_panel(provider) for provider in providers)


def overall_status(providers: tuple[ProviderDashboard, ...]) -> str:
    cards = "".join(overall_provider(provider) for provider in providers if provider.primary or provider.snapshot.errors)
    return f'<section class="panel overview-panel"><h2>전체 현황</h2><div class="overview-grid">{cards}</div></section>' if cards else ""


def overall_provider(provider: ProviderDashboard) -> str:
    title = provider_title(provider.source, upper=True)
    if provider.snapshot.errors:
        return f'<article><h3>{html.escape(title)}</h3><p class="error-text">{html.escape(provider.snapshot.errors[0])}</p></article>'
    rows = "".join(overall_window(item, provider.source) for item in provider.comparison if item.is_quota)
    if not rows and provider.primary and provider.primary.is_quota:
        rows = overall_window(provider.primary, provider.source)
    return f'<article><h3>{html.escape(title)}</h3>{rows}</article>'


def overall_window(item: DashboardWindow, source: str) -> str:
    return (
        '<div class="overview-window">'
        f'<div class="row"><span>{html.escape(short_window_label(item.name))}</span><strong>{percent0(item.window.utilization)}{pace_badge(item.window)}</strong></div>'
        f'<div class="mini-bar"><span class="fill-{html.escape(source)} fill-{html.escape(item.name)}" style="width:{clamped_pct(item.window.utilization):.1f}%"></span></div>'
        f'<p>{html.escape(reset_hours(item.window))}</p>'
        '</div>'
    )


def runtime_usage_report(providers: tuple[ProviderDashboard, ...]) -> str:
    cards = "".join(runtime_provider(provider) for provider in providers if provider.primary or provider.windows)
    if not cards:
        return ""
    return (
        '<section class="panel runtime-panel qc-runtime-report">'
        '<h2>자동 런타임 LLM 사용량</h2>'
        '<p class="panel-note">대상: runtime 태그가 붙은 실제 봇 세션만 집계</p>'
        f'<div class="runtime-grid">{cards}</div>'
        '</section>'
    )


def runtime_provider(provider: ProviderDashboard) -> str:
    windows = [item for item in provider.comparison if item.is_quota]
    if not windows and provider.primary and provider.primary.is_quota:
        windows = [provider.primary]
    body = "".join(runtime_window(item) for item in windows if item)
    legend = model_legend([item.window.runtime for item in windows if item])
    return f'<article class="runtime-card"><h3>{html.escape(provider.source.title())} Runtime</h3>{legend}<div class="runtime-windows">{body}</div></article>'


def runtime_window(item: DashboardWindow) -> str:
    runtime = item.window.runtime
    total = runtime.total_tokens
    service_total = item.window.total_tokens
    runtime_pct = total / service_total if service_total > 0 else 0.0
    projects = runtime_project_rows(runtime.by_project, total)
    tokens = runtime_usage_line(runtime, service_total)
    return (
        '<div class="runtime-window">'
        f'<h4>{html.escape(runtime_window_label(item.name))}</h4>'
        f'<div class="runtime-bar"><span style="width:{clamped_pct(runtime_pct):.1f}%">{model_segments(runtime)}</span></div>'
        f'<div class="row"><strong>{percent1(runtime_pct)}</strong><span>{percent1(item.window.utilization)} of quota</span></div>'
        f'<p>{html.escape(tokens)}</p>'
        f'{projects}'
        '</div>'
    )


def provider_panel(provider: ProviderDashboard) -> str:
    title = provider_title(provider.source)
    if provider.snapshot.errors:
        return panel(title, f'<p class="error-text">{html.escape(provider.snapshot.errors[0])}</p>')
    quota_windows = [item for item in provider.comparison if item.is_quota]
    if not quota_windows and provider.primary and provider.primary.is_quota:
        quota_windows = [provider.primary]
    usage_windows = provider_usage_windows(provider, quota_windows)
    if not quota_windows and not usage_windows:
        return ""
    blocks = []
    if quota_windows:
        legend = window_model_legend([item.window for item in quota_windows])
        cards = "".join(quota_window_card(item, provider.source) for item in quota_windows)
        blocks.append(f'{legend}<div class="qc-quota-split quota-grid-{len(quota_windows)}">{cards}</div>')
    if usage_windows:
        cards = "".join(usage_window_card(item) for item in usage_windows)
        blocks.append(f'<div class="provider-subhead">로컬 사용량</div><div class="usage-grid">{cards}</div>')
    return panel(f"{title} Quota", "".join(blocks), status_badge(provider.primary.window.cache_state if provider.primary else "unknown"))


def provider_usage_windows(provider: ProviderDashboard, quota_windows: list[DashboardWindow]) -> list[DashboardWindow]:
    quota_names = {item.name for item in quota_windows}
    preferred = ["today", "seven_day", "this_month", "local_all"]
    by_name = {item.name: item for item in provider.windows}
    return [
        by_name[name]
        for name in preferred
        if name in by_name
        and name not in quota_names
        and (by_name[name].is_usage or by_name[name].window.total_tokens > 0 or bool(by_name[name].window.by_project))
    ]


def quota_window_card(item: DashboardWindow, source: str) -> str:
    window = item.window
    project_section = ""
    token_line = format_quota_tokens(window)
    token_section = f'<p>{html.escape(token_line)}</p>' if token_line else ""
    if window.by_project:
        project_section = '<h4>Apps</h4>' + project_rows(window.by_project, window.total_tokens, max_rows=12)
    return (
        '<article class="quota-window">'
        f'<h3>{html.escape(short_window_label(item.name))} 창</h3>'
        f'<div class="quota-bar"><span class="fill-{html.escape(source)} fill-{html.escape(item.name)}" style="width:{clamped_pct(window.utilization):.1f}%">{model_segments_from_projects(window.by_project)}</span></div>'
        f'<div class="row"><strong>{percent1(window.utilization)}{pace_badge(window)}</strong><span>{html.escape(reset_hours(window))}</span></div>'
        f'{token_section}'
        f'{quota_group_rows(window.quota_groups)}'
        f'{project_section}'
        '</article>'
    )


def quota_group_rows(groups: dict[str, SnapshotQuotaGroup]) -> str:
    if not groups:
        return ""
    rows = "".join(quota_group_row(group) for group in groups.values())
    return '<div class="quota-group-list"><h4>그룹별 Request 한도</h4>' + rows + '</div>'


def quota_group_row(group: SnapshotQuotaGroup) -> str:
    return (
        '<div class="quota-group-row">'
        f'<span>{html.escape(group.label)}</span>'
        '<div class="quota-group-bar">'
        f'<i style="width:{clamped_pct(group.utilization):.1f}%;background:{quota_tone_color(group.utilization)}"></i>'
        '</div>'
        f'<strong>{percent0(group.utilization)}</strong>'
        f'<em>{html.escape(reset_label(group.resets_at))}</em>'
        '</div>'
    )


def usage_window_card(item: DashboardWindow) -> str:
    window = item.window
    return (
        '<article class="usage-card">'
        f'<h3>{html.escape(usage_window_label(item.name))}</h3>'
        f'<strong>{compact_number(window.total_tokens)}</strong>'
        f'{project_rows(window.by_project, window.total_tokens, max_rows=6)}'
        '</article>'
    )


def claude_session_panel(providers: tuple[ProviderDashboard, ...]) -> str:
    provider = next((item for item in providers if item.source == "claude"), None)
    if provider is None or not isinstance(provider.snapshot.history, dict):
        return ""
    report = provider.snapshot.history.get("claude_session_report")
    if not isinstance(report, dict):
        return ""
    totals = report.get("totals", {}) if isinstance(report.get("totals"), dict) else {}
    window = report.get("window", {}) if isinstance(report.get("window"), dict) else {}
    blocks = [
        session_metric("Total", compact_number(int(totals.get("total_tokens") or 0))),
        session_metric("Input", compact_number(int(totals.get("input_tokens") or 0))),
        session_metric("Output", compact_number(int(totals.get("output_tokens") or 0))),
        session_metric("Cache Read", compact_number(int(totals.get("cache_read_input_tokens") or 0))),
        session_metric("Cache Create", compact_number(int(totals.get("cache_creation_input_tokens") or 0))),
        session_metric("Cache Hit", f'{float(totals.get("cache_hit_pct") or 0):.1f}%'),
    ]
    sections = [
        f'<div class="session-metrics">{"".join(blocks)}</div>',
        f'<p class="panel-note">window: {html.escape(str(window.get("name") or "unknown"))} · {html.escape(str(report.get("cache_state") or "unknown"))}</p>',
        session_rows("Projects", report.get("by_project", [])),
        session_rows("Subagents", report.get("by_subagent", [])),
        session_rows("Skills", report.get("by_skill", [])),
        session_rows("Slash Commands", report.get("by_slash_command", [])),
        expensive_prompt_rows(report.get("expensive_prompts", [])),
        cache_break_rows(report.get("cache_breaks", [])),
    ]
    return panel("Claude Sessions", '<div class="session-grid">' + "".join(section for section in sections if section) + "</div>", status_badge(str(report.get("cache_state") or "unknown")))


def session_metric(label: str, value: str) -> str:
    return f'<div class="session-metric"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'


def session_rows(title: str, rows: object) -> str:
    if not isinstance(rows, list) or not rows:
        return ""
    items = []
    for row in rows[:6]:
        if not isinstance(row, dict):
            continue
        name = str(row.get("display_name") or row.get("name") or "unknown")
        share = float(row.get("share_pct") or 0)
        tokens = compact_number(int(row.get("total_tokens") or 0))
        items.append(f'<li><span>{html.escape(name)}</span><strong>{share:.1f}% · {html.escape(tokens)}</strong></li>')
    return f'<article class="session-card"><h3>{html.escape(title)}</h3><ol class="runtime-project-list">{"".join(items)}</ol></article>' if items else ""


def expensive_prompt_rows(rows: object) -> str:
    if not isinstance(rows, list) or not rows:
        return ""
    items = []
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        preview = str(row.get("prompt_preview") or row.get("prompt_hash") or "redacted")
        tokens = compact_number(int(row.get("total_tokens") or 0))
        project = str(row.get("project") or "unknown")
        calls = int(row.get("api_calls") or 0)
        right = f"{tokens}{f' · {calls} calls' if calls else ''}"
        items.append(f'<li><span>{html.escape(project)} · {html.escape(preview)}</span><strong>{html.escape(right)}</strong></li>')
    return f'<article class="session-card session-card-wide"><h3>Expensive Prompts</h3><ol class="runtime-project-list">{"".join(items)}</ol></article>' if items else ""


def cache_break_rows(rows: object) -> str:
    if not isinstance(rows, list) or not rows:
        return ""
    items = []
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        preview = str(row.get("prompt_preview") or row.get("reason") or row.get("prompt_hash") or "cache break")
        project = str(row.get("project") or "unknown")
        tokens = compact_number(int(row.get("tokens") or 0))
        calls = int(row.get("api_calls") or 0)
        right = f"{tokens}{f' · {calls} calls' if calls else ''}"
        items.append(f'<li><span>{html.escape(project)} · {html.escape(preview)}</span><strong>{html.escape(right)}</strong></li>')
    return f'<article class="session-card session-card-wide"><h3>Cache Breaks</h3><ol class="runtime-project-list">{"".join(items)}</ol></article>' if items else ""


def time_series_panel(providers: tuple[ProviderDashboard, ...]) -> str:
    cards = []
    for provider in providers:
        timeline = provider.snapshot.history.get("usage_timeline", {}) if isinstance(provider.snapshot.history, dict) else {}
        if not isinstance(timeline, dict):
            continue
        dates = [str(value) for value in timeline.get("dates", []) if value]
        daily_total_raw = timeline.get("daily_total", {})
        datasets = timeline.get("datasets", [])
        if not dates or not isinstance(daily_total_raw, dict):
            continue
        totals = [float(daily_total_raw.get(day, 0) or 0) for day in dates]
        if not any(totals):
            continue
        cards.append(usage_timeline_card(provider.source, dates, totals, datasets, str(timeline.get("unit") or "tokens")))
    if not cards:
        return ""
    return panel("시계열 사용량", '<div class="timeline-grid">' + "".join(cards) + "</div>")


def quota_history_panel(providers: tuple[ProviderDashboard, ...]) -> str:
    cards = []
    for provider in providers:
        history = provider.snapshot.history.get("quota_history", []) if isinstance(provider.snapshot.history, dict) else []
        if not isinstance(history, list) or not history:
            continue
        cards.append(quota_history_card(provider.source, history))
    if not cards:
        return ""
    return panel("Quota 시계열", '<div class="timeline-grid">' + "".join(cards) + "</div>")


def quota_history_card(source: str, history: list[object]) -> str:
    series = []
    for key, label in (("5h_util", "5시간"), ("7d_util", "7일"), ("usage", "현재")):
        values = quota_history_values(history, key)
        if values and any(values):
            series.append((label, values))
    if not series:
        return ""
    rows = "".join(
        '<section class="quota-history-row">'
        f'<div class="row"><span>{html.escape(label)}</span><strong>{values[-1]:.1f}%</strong></div>'
        f'{sparkline_svg(values, source)}'
        '</section>'
        for label, values in series
    )
    return f'<article class="timeline-card"><h3>{html.escape(provider_title(source))}</h3>{rows}</article>'


def quota_history_values(history: list[object], key: str) -> list[float]:
    values = [float(row.get(key, 0) or 0) for row in history if isinstance(row, dict) and key in row]
    if key.endswith("_util") and values and max(values) <= 1.0:
        return [value * 100 for value in values]
    return values


def usage_timeline_card(source: str, dates: list[str], totals: list[float], datasets: object, unit: str) -> str:
    project_rows_html = ""
    project_series: list[tuple[str, list[float]]] = []
    if isinstance(datasets, list):
        project_items = []
        total = sum(totals)
        for index, row in enumerate(datasets[:5]):
            if not isinstance(row, dict):
                continue
            name = str(row.get("project") or "unknown")
            color = project_color(index)
            row_total = float(row.get("total_tokens") or row.get("total_cost") or 0)
            share = row_total / total * 100 if total > 0 else 0.0
            daily = row.get("daily", {})
            project_values = [float(daily.get(day, 0) or 0) for day in dates] if isinstance(daily, dict) else []
            if any(project_values):
                project_series.append((name, project_values))
            project_items.append(
                '<li>'
                f'<span class="timeline-project-name"><i style="background:{color}"></i>{html.escape(display_name(name, kind="project"))}</span>'
                f'{mini_sparkline_svg(project_values, source, color)}'
                f'<strong>{html.escape(format_history_value(row_total, unit))} · {share:.1f}%</strong>'
                '</li>'
            )
        if project_items:
            project_rows_html = '<ol class="timeline-projects">' + "".join(project_items) + '</ol>'
    return (
        '<article class="timeline-card">'
        f'<div class="row"><h3>{html.escape(provider_title(source))}</h3><strong>{html.escape(format_history_value(sum(totals), unit))}</strong></div>'
        f'{usage_multiline_svg(totals, project_series, source)}'
        f'<p>{html.escape(dates[0])} - {html.escape(dates[-1])}</p>'
        f'{project_rows_html}'
        '</article>'
    )


def usage_multiline_svg(totals: list[float], project_series: list[tuple[str, list[float]]], source: str) -> str:
    if not project_series:
        return sparkline_svg(totals, source)
    width = 360
    height = 128
    maximum = max([*totals, *(value for _, values in project_series for value in values)] or [1]) or 1
    total_path = sparkline_points(totals, width=width, height=height, top=10, bottom=22, maximum=maximum)
    project_lines = []
    for index, (name, values) in enumerate(project_series[:5]):
        path = sparkline_points(values, width=width, height=height, top=10, bottom=22, maximum=maximum)
        color = project_color(index)
        project_lines.append(
            f'<polyline class="project-line" points="{path}" style="stroke:{color}"><title>{html.escape(display_name(name, kind="project"))}</title></polyline>'
        )
    return (
        f'<svg class="sparkline project-line-chart sparkline-{html.escape(source)}" viewBox="0 0 {width} {height}" role="img" aria-label="project usage timeline">'
        f'<polyline class="total-line" points="{total_path}"></polyline>'
        f'{"".join(project_lines)}'
        '</svg>'
    )


def sparkline_svg(values: list[float], source: str) -> str:
    if not values:
        return ""
    width = 360
    height = 88
    top = 8
    bottom = 18
    maximum = max(values) or 1
    if len(values) == 1:
        points = [(0.0, height - bottom - (values[0] / maximum) * (height - top - bottom))]
    else:
        points = [
            (index / (len(values) - 1) * width, height - bottom - (value / maximum) * (height - top - bottom))
            for index, value in enumerate(values)
        ]
    point_attr = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    fill_path = point_attr + f" {width:.1f},{height - bottom:.1f} 0.0,{height - bottom:.1f}"
    return (
        f'<svg class="sparkline sparkline-{html.escape(source)}" viewBox="0 0 {width} {height}" role="img" aria-label="daily usage timeline">'
        f'<polygon points="{fill_path}"></polygon>'
        f'<polyline points="{point_attr}"></polyline>'
        '</svg>'
    )


def mini_sparkline_svg(values: list[float], source: str, color: str) -> str:
    if not values or not any(values):
        return '<span class="mini-sparkline"></span>'
    path = sparkline_points(values, width=72, height=18, top=2, bottom=3)
    return (
        f'<svg class="mini-sparkline sparkline-{html.escape(source)}" viewBox="0 0 72 18" aria-hidden="true">'
        f'<polyline points="{path}" style="stroke:{color}"></polyline>'
        '</svg>'
    )


def sparkline_points(values: list[float], *, width: int, height: int, top: int, bottom: int, maximum: float | None = None) -> str:
    maximum = maximum or max(values) or 1
    if len(values) == 1:
        points = [(0.0, height - bottom - (values[0] / maximum) * (height - top - bottom))]
    else:
        points = [
            (index / (len(values) - 1) * width, height - bottom - (value / maximum) * (height - top - bottom))
            for index, value in enumerate(values)
        ]
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def format_history_value(value: float, unit: str) -> str:
    if unit == "usd":
        if value >= 100:
            return f"${value:,.0f}"
        return f"${value:,.2f}"
    return compact_number(value)


def project_color(index: int) -> str:
    palette = ["#38bdf8", "#f97316", "#2dd4bf", "#facc15", "#c084fc"]
    return palette[index % len(palette)]


def detail_windows(providers: tuple[ProviderDashboard, ...]) -> str:
    cards = "".join(detail_provider_card(provider) for provider in providers)
    return f'<details class="panel detail-panel"><summary>Provider Details</summary><div class="detail-grid">{cards}</div></details>' if cards else ""


def detail_provider_card(provider: ProviderDashboard) -> str:
    windows = provider.comparison + provider.details
    body = "".join(detail_window(item) for item in windows)
    return f'<article class="detail-card"><h3>{html.escape(provider_title(provider.source))}</h3>{body}</article>'


def detail_window(item: DashboardWindow) -> str:
    window = item.window
    utilization = percent1(window.utilization) if item.is_quota else "local history"
    return (
        '<section class="detail-window">'
        f'<h4>{html.escape(window_label(item.name))}</h4>'
        '<dl>'
        f'{metric("Utilization", utilization)}'
        f'{metric("Tokens", compact_number(window.total_tokens))}'
        f'{metric("Requests", f"{window.requests:,}")}'
        f'{metric("Window range", window_range(window) if item.is_quota else "local history")}'
        f'{metric("Concentration", concentration_label(window) if not item.is_quota else "quota window")}'
        f'{metric("Top project", top_label(window.by_project, "project"))}'
        f'{metric("Top model", top_label(window.by_model, "model"))}'
        '</dl>'
        '<h4>Apps</h4>'
        f'{project_rows(window.by_project, window.total_tokens, max_rows=5)}'
        f'{model_rows(window.by_model)}'
        '</section>'
    )


def data_state_panel(providers: tuple[ProviderDashboard, ...]) -> str:
    rows = []
    for provider in providers:
        for warning in provider.snapshot.warnings:
            rows.append(f'<li><strong>{html.escape(provider.source.title())}</strong><span>{html.escape(warning)}</span></li>')
        for error in provider.snapshot.errors:
            rows.append(f'<li class="error"><strong>{html.escape(provider.source.title())}</strong><span>{html.escape(error)}</span></li>')
    if not rows:
        rows.append('<li><strong>All providers</strong><span>No quota pressure or warnings in the current snapshot</span></li>')
    return panel("Data State", f'<ol class="state-list">{"".join(rows)}</ol>')


def panel(title: str, body: str, right: str = "") -> str:
    return f'<section class="panel"><div class="panel-head"><h2>{html.escape(title)}</h2>{right}</div>{body}</section>'


def status_badge(state: str) -> str:
    label = state or "unknown"
    tone = "available" if label == "live" else "watch" if label == "cached" else "limited" if label == "stale" else "unknown"
    return f'<span class="limit-badge limit-{tone}">{html.escape(label)}</span>'


def project_rows(rows: dict[str, AggregateBreakdown], total_tokens: int, *, max_rows: int = TOP_ROWS, include_requests: bool = False, empty: str = "데이터 없음") -> str:
    if not rows:
        return f'<p class="empty-list">{html.escape(empty)}</p>'
    normalized = share_aggregates(rows) if total_tokens > 0 else rows
    items = []
    for name, aggregate in list(normalized.items())[:max_rows]:
        share = aggregate.share_pct
        right = f"{share:.1f}%"
        if include_requests and aggregate.requests:
            right += f" · {aggregate.requests:,} req"
        width = max(0.0, min(100.0, share))
        segments = model_segments_from_aggregate(aggregate)
        items.append(
            '<li>'
            f'<span>{html.escape(display_name(name, kind="project"))}</span>'
            f'<div class="project-bar"><i style="width:{width:.1f}%">{segments}</i></div>'
            f'<strong>{html.escape(right)}</strong>'
            '</li>'
        )
    return f'<ol class="project-list">{"".join(items)}</ol>'


def runtime_project_rows(rows: dict[str, AggregateBreakdown], total_tokens: int) -> str:
    if not rows:
        return '<p class="empty-list">데이터 없음</p>'
    normalized = share_aggregates(rows) if total_tokens > 0 else rows
    items = "".join(
        '<li>'
        f'<span>{html.escape(display_name(name, kind="project"))}</span>'
        f'<strong>{aggregate.share_pct:.1f}%{f" · {aggregate.requests:,} req" if aggregate.requests else ""}</strong>'
        '</li>'
        for name, aggregate in list(normalized.items())[:3]
    )
    return f'<ol class="runtime-project-list">{items}</ol>'


def runtime_usage_line(runtime: RuntimeBreakdown, service_total: int) -> str:
    if runtime.total_tokens <= 0 and runtime.requests <= 0:
        return "runtime 데이터 없음"
    if runtime.total_tokens <= 0:
        return f"0 tokens · {runtime.requests:,} req"
    request_suffix = f" · {runtime.requests:,} req" if runtime.requests else ""
    return f"{compact_number(runtime.total_tokens)} / {compact_number(service_total)} tokens{request_suffix}"


def model_rows(rows: dict[str, AggregateBreakdown]) -> str:
    if not rows:
        return ""
    items = "".join(f'<li><span>{html.escape(display_name(name, kind="model"))}</span><strong>{aggregate.share_pct:.1f}%</strong></li>' for name, aggregate in list(rows.items())[:5])
    return f'<h4>Models</h4><ol class="model-list">{items}</ol>'


def model_legend(runtimes: list[RuntimeBreakdown]) -> str:
    totals: dict[str, int] = {}
    for runtime in runtimes:
        merge_model_totals(totals, runtime.by_model)
        for aggregate in runtime.by_project.values():
            for model, tokens in aggregate.models.items():
                totals[model] = totals.get(model, 0) + tokens
    return legend_from_totals(totals)


def window_model_legend(windows: list[SnapshotWindow]) -> str:
    totals: dict[str, int] = {}
    for window in windows:
        merge_model_totals(totals, window.by_model)
        for aggregate in window.by_project.values():
            for model, tokens in aggregate.models.items():
                totals[model] = totals.get(model, 0) + tokens
    return legend_from_totals(totals)


def merge_model_totals(totals: dict[str, int], rows: dict[str, AggregateBreakdown]) -> None:
    for model, aggregate in rows.items():
        totals[model] = totals.get(model, 0) + aggregate.total_tokens


def legend_from_totals(totals: dict[str, int]) -> str:
    if not totals:
        return ""
    items = "".join(
        f'<span><i style="background:{model_color(model)}"></i>{html.escape(display_name(model, kind="model"))}</span>'
        for model, _ in sorted(totals.items(), key=lambda item: item[1], reverse=True)[:6]
    )
    return f'<div class="model-legend">{items}</div>'


def model_segments(runtime: RuntimeBreakdown) -> str:
    totals: dict[str, int] = {}
    merge_model_totals(totals, runtime.by_model)
    for aggregate in runtime.by_project.values():
        for model, tokens in aggregate.models.items():
            totals[model] = totals.get(model, 0) + tokens
    total = sum(totals.values())
    if total <= 0:
        return ""
    return "".join(segment(model, tokens / total * 100) for model, tokens in sorted(totals.items(), key=lambda item: item[1], reverse=True))


def model_segments_from_projects(rows: dict[str, AggregateBreakdown]) -> str:
    totals: dict[str, int] = {}
    for aggregate in rows.values():
        for model, tokens in aggregate.models.items():
            totals[model] = totals.get(model, 0) + tokens
    total = sum(totals.values())
    if total <= 0:
        return ""
    return "".join(segment(model, tokens / total * 100) for model, tokens in sorted(totals.items(), key=lambda item: item[1], reverse=True))


def model_segments_from_aggregate(aggregate: AggregateBreakdown) -> str:
    total = sum(aggregate.models.values())
    if total <= 0:
        return ""
    left = 0.0
    pieces = []
    for model, tokens in sorted(aggregate.models.items(), key=lambda item: item[1], reverse=True):
        segment_width = tokens / total * 100
        pieces.append(f'<b style="left:{left:.3f}%;width:{segment_width:.3f}%;background:{model_color(model)}"></b>')
        left += segment_width
    return "".join(pieces)


def segment(model: str, width: float) -> str:
    return f'<b style="width:{max(0.0, min(100.0, width)):.3f}%;background:{model_color(model)}"></b>'


def model_color(name: str) -> str:
    known = {
        "haiku-4-5": "#3b82f6",
        "claude-haiku-4-5-20251001": "#3b82f6",
        "sonnet-4-6": "#f97316",
        "claude-sonnet-4-6": "#f97316",
        "opus-4-6": "#10b981",
        "claude-opus-4-6": "#10b981",
        "gemini-2.5-pro": "#0ea5e9",
        "gemini-2.5-flash": "#8b5cf6",
    }
    normalized = name.replace("claude-", "").replace("-20251001", "")
    if name in known:
        return known[name]
    if normalized in known:
        return known[normalized]
    palette = ["#60a5fa", "#fb7185", "#34d399", "#fbbf24", "#a78bfa", "#22d3ee", "#f97316", "#4ade80"]
    return palette[sum(ord(char) for char in name) % len(palette)]


def share_aggregates(rows: dict[str, AggregateBreakdown]) -> dict[str, AggregateBreakdown]:
    total = sum(row.total_tokens for row in rows.values())
    if total <= 0:
        return rows
    return {
        name: AggregateBreakdown(
            total_tokens=aggregate.total_tokens,
            requests=aggregate.requests,
            share_pct=aggregate.total_tokens / total * 100,
            models=aggregate.models,
            model_requests=aggregate.model_requests,
        )
        for name, aggregate in sorted(rows.items(), key=lambda item: item[1].total_tokens, reverse=True)
    }


def metric(label: str, value: str) -> str:
    return f'<div><dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd></div>'


def provider_title(source: str, *, upper: bool = False) -> str:
    titles = {"claude": "Claude Max", "codex": "Codex (ChatGPT Plus)", "gemini": "Gemini (Google)"}
    title = titles.get(source, source.title())
    return title.upper() if upper else title


def short_window_label(name: str) -> str:
    labels = {
        "five_hour": "5시간",
        "seven_day": "7일",
        "current_quota": "현재",
        "current_session": "현재 세션",
        "current_week": "이번 주",
        "current_week_sonnet": "이번 주 Sonnet",
    }
    return labels.get(name, window_label(name))


def runtime_window_label(name: str) -> str:
    return "현재 quota window" if name == "current_quota" else short_window_label(name)


def usage_window_label(name: str) -> str:
    return {"today": "오늘 사용량 (KST)", "seven_day": "7일 전체 사용량", "this_month": "이번 달 전체 사용량", "local_all": "전체 사용량"}.get(name, window_label(name))


def window_label(name: str) -> str:
    return name.replace("_", " ").title()


def display_name(name: str, *, kind: str) -> str:
    if kind == "model":
        return name.split("/")[-1].replace("claude-", "")
    return name


def top_label(rows: dict[str, AggregateBreakdown], kind: str) -> str:
    if not rows:
        return "--"
    name, aggregate = next(iter(rows.items()))
    return f"{display_name(name, kind=kind)} {aggregate.share_pct:.1f}%"


def concentration_label(window: SnapshotWindow) -> str:
    top = next(iter(window.by_project.values()), None)
    if top is None:
        return "no local history"
    if top.share_pct >= 75:
        return f"top-heavy {top.share_pct:.0f}%"
    if top.share_pct >= 50:
        return f"focused {top.share_pct:.0f}%"
    return "distributed"


def clamped_pct(utilization: float) -> float:
    return max(0.0, min(100.0, utilization * 100))


def percent0(utilization: float) -> str:
    return f"{clamped_pct(utilization):.0f}%"


def percent1(utilization: float) -> str:
    return f"{clamped_pct(utilization):.1f}%"


def pace_badge(window: SnapshotWindow) -> str:
    label = pace_label(window)
    if not label:
        return ""
    tone = "hot" if "과속" in label else "cool"
    return f' <em class="pace-{tone}">{html.escape(label)}</em>'


def pace_label(window: SnapshotWindow) -> str:
    if not window.resets_at or not window.window_start or not window.utilization:
        return ""
    window_sec = window.resets_at - window.window_start
    if window_sec <= 0:
        return ""
    elapsed = time.time() - window.window_start
    if elapsed <= 0 or elapsed > window_sec:
        return ""
    diff = window.utilization * 100 - elapsed / window_sec * 100
    if diff > 3:
        return f"▲+{diff:.0f}%p 과속"
    if diff < -3:
        return f"▼{abs(diff):.0f}%p 여유"
    return ""


def reset_hours(window: SnapshotWindow) -> str:
    if not window.resets_at:
        return ""
    diff = window.resets_at - time.time()
    if diff < 0:
        return "리셋됨"
    if diff < 3600:
        return f"{diff / 60:.0f}분 후 리셋"
    return f"{diff / 3600:.1f}시간 후 리셋"


def reset_label(resets_at: int | None, *, suffix: str = " 후") -> str:
    if not resets_at:
        return "-"
    diff = resets_at - time.time()
    if diff < 0:
        return "리셋됨"
    if diff < 3600:
        return f"{diff / 60:.0f}분{suffix}"
    return f"{diff / 3600:.1f}h{suffix}"


def quota_tone_color(utilization: float) -> str:
    pct = clamped_pct(utilization)
    if pct >= 90:
        return "#f87171"
    if pct >= 50:
        return "#f59e0b"
    return "#22c55e"


def format_quota_tokens(window: SnapshotWindow) -> str:
    if window.total_tokens <= 0:
        return ""
    return f"{compact_number(window.total_tokens)} tokens"


def window_range(window: SnapshotWindow) -> str:
    if not window.window_start or not window.resets_at:
        return "--"
    start = datetime.fromtimestamp(window.window_start, tz=KST).strftime("%b %-d %H:%M")
    end = datetime.fromtimestamp(window.resets_at, tz=KST).strftime("%b %-d %H:%M")
    return f"{start} to {end}"


def compact_number(value: int | float) -> str:
    value = float(value or 0)
    for suffix, divisor in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if abs(value) >= divisor:
            return f"{value / divisor:.1f}{suffix}"
    return str(int(value))


def stylesheet() -> str:
    return """
:root { --bg:#0b0c0f; --card:#15151b; --card2:#101217; --card3:#18191f; --border:#2b2b34; --border-soft:#22232b; --text:#eee9df; --muted:#a09a8f; --subtle:#756f66; --accent:#2dd4bf; --ok:#34d399; --warn:#f4b860; --fail:#fb7185; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family:'Segoe UI', system-ui, sans-serif; font-size:14px; color-scheme:dark; }
header { background:#121318; border-bottom:1px solid var(--border); padding:16px 24px; display:flex; align-items:center; justify-content:space-between; gap:12px; box-shadow:0 1px 0 rgba(255,255,255,.03); }
header h1 { margin:0; font-size:18px; font-weight:700; }
main { max-width:1400px; margin:0 auto; padding:20px 24px; }
.updated { color:var(--muted); font-size:12px; }
.panel { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:16px; margin-bottom:16px; box-shadow:0 12px 28px rgba(0,0,0,.22), inset 0 1px 0 rgba(255,255,255,.03); }
.detail-panel summary { cursor:pointer; color:var(--muted); font-size:13px; font-weight:700; text-transform:uppercase; letter-spacing:.5px; }
.detail-panel .detail-grid { margin-top:14px; }
.panel-head { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:14px; }
.panel h2 { margin:0; color:#c9c2b6; font-size:13px; font-weight:750; text-transform:uppercase; letter-spacing:.5px; }
.panel-note, .panel p { color:var(--muted); font-size:12px; }
.overview-grid, .runtime-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; }
.overview-grid h3 { margin:0 0 8px; color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
.overview-window { margin-top:10px; }
.row { display:flex; align-items:baseline; justify-content:space-between; gap:8px; font-size:12px; }
.row strong { color:#fffaf1; }
.row span { color:var(--muted); }
.mini-bar, .quota-bar, .runtime-bar, .project-bar { background:#24252c; border-radius:4px; overflow:hidden; position:relative; }
.mini-bar { height:6px; margin-top:3px; }
.mini-bar span { display:block; height:100%; min-width:0; }
.quota-bar > span { display:flex; height:100%; min-width:0; }
.overview-window p { margin:4px 0 0; color:var(--muted); font-size:11px; }
.runtime-card, .quota-window, .usage-card, .detail-card { background:var(--card2); border:1px solid var(--border); border-radius:8px; padding:14px; min-width:0; overflow:hidden; box-shadow:inset 0 1px 0 rgba(255,255,255,.025); }
.runtime-card h3, .quota-window h3, .usage-card h3, .detail-card h3 { margin:0 0 10px; font-size:12px; color:#fffaf1; }
.runtime-windows, .qc-quota-split { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
.runtime-card:last-child .runtime-windows { grid-template-columns:1fr; }
.quota-grid-1 { grid-template-columns:minmax(0,1fr); }
.provider-subhead { margin:14px 0 8px; color:var(--muted); font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.5px; }
.runtime-window h4, .quota-window h4, .detail-window h4 { margin:12px 0 6px; color:var(--muted); font-size:12px; font-weight:650; }
.runtime-bar { height:20px; margin-bottom:4px; }
.runtime-bar > span { display:flex; height:100%; min-width:0; border-radius:4px; overflow:hidden; }
.runtime-bar b, .quota-bar b { display:block; height:100%; }
.quota-bar { height:20px; margin:8px 0 5px; }
.quota-bar.compact { height:12px; }
.quota-group-list { border-top:1px solid var(--border); margin-top:10px; padding-top:8px; }
.quota-group-list h4 { margin:0 0 6px; color:var(--muted); font-size:11px; font-weight:650; }
.quota-group-row { display:grid; grid-template-columns:80px 60px 32px minmax(56px,auto); align-items:center; gap:8px; margin-bottom:4px; font-size:11px; }
.quota-group-row > span { color:#fff; font-size:10px; font-weight:650; white-space:nowrap; }
.quota-group-row strong { color:#fff; font-size:10px; }
.quota-group-row em { color:var(--muted); font-size:10px; font-style:normal; white-space:nowrap; }
.quota-group-bar { background:var(--border); border-radius:2px; height:5px; overflow:hidden; }
.quota-group-bar i { display:block; height:100%; }
.fill-claude, .fill-five_hour { background:#8b7cf6; }
.fill-claude.fill-seven_day { background:#2dd4bf; }
.fill-codex.fill-five_hour { background:#f97316; }
.fill-codex.fill-seven_day { background:#f59e0b; }
.fill-gemini { background:#eab308; }
.pace-hot { color:var(--warn); font-style:normal; font-size:11px; }
.pace-cool { color:#45d6a3; font-style:normal; font-size:11px; }
.model-legend { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:10px; color:var(--muted); font-size:11px; }
.model-legend span { display:inline-flex; align-items:center; gap:5px; min-width:0; }
.model-legend i { width:8px; height:8px; border-radius:2px; }
.project-list, .model-list, .state-list { margin:8px 0 0; padding:0; list-style:none; }
.project-list li { display:grid; grid-template-columns:130px minmax(80px,1fr) 112px; align-items:center; gap:6px; margin-bottom:4px; font-size:11px; }
.project-list span { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:650; color:#fffaf1; }
.project-list strong, .model-list strong { color:var(--muted); text-align:right; font-weight:600; white-space:nowrap; }
.project-bar { height:8px; }
.project-bar i { display:block; height:100%; background:var(--accent); position:relative; }
.project-bar b { position:absolute; top:0; height:100%; }
.runtime-project-list { display:grid; gap:6px; margin:10px 0 0; padding:0; list-style:none; }
.runtime-project-list li { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px; align-items:baseline; font-size:11px; }
.runtime-project-list span { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#fffaf1; font-weight:650; }
.runtime-project-list strong { color:var(--muted); white-space:nowrap; font-weight:600; }
.empty-list { margin:6px 0 0; color:var(--muted); font-size:11px; }
.usage-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:20px; margin-top:8px; }
.usage-card > strong { display:block; margin-bottom:8px; font-size:20px; color:#fff; }
.session-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; }
.session-metrics { grid-column:1 / -1; display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; }
.session-metric, .session-card { background:var(--card2); border:1px solid var(--border-soft); border-radius:8px; padding:10px; min-width:0; }
.session-metric span { display:block; color:var(--muted); font-size:11px; }
.session-metric strong { display:block; margin-top:3px; color:#fffaf1; font-size:16px; }
.session-card h3 { margin:0 0 8px; color:#fffaf1; font-size:12px; }
.session-card-wide { grid-column:span 2; }
.timeline-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; }
.timeline-card { background:var(--card2); border:1px solid var(--border); border-radius:8px; padding:14px; min-width:0; overflow:hidden; box-shadow:inset 0 1px 0 rgba(255,255,255,.025); }
.timeline-card h3 { margin:0; color:#fffaf1; font-size:12px; }
.timeline-card p { margin:6px 0 0; color:var(--muted); font-size:11px; }
.quota-history-row { margin-top:10px; }
.quota-history-row .sparkline { height:58px; margin-top:6px; }
.sparkline { display:block; width:100%; height:88px; margin-top:10px; overflow:visible; }
.project-line-chart { height:128px; }
.sparkline polygon { fill:rgba(139,124,246,.13); }
.sparkline polyline { fill:none; stroke:#8b7cf6; stroke-width:3; stroke-linejoin:round; stroke-linecap:round; }
.project-line-chart .total-line { stroke:rgba(160,154,143,.38); stroke-width:2; }
.project-line-chart .project-line { fill:none; stroke-width:2.4; stroke-linejoin:round; stroke-linecap:round; opacity:.95; }
.sparkline-codex polygon { fill:rgba(249,115,22,.14); }
.sparkline-codex polyline { stroke:#f97316; }
.sparkline-gemini polygon { fill:rgba(234,179,8,.14); }
.sparkline-gemini polyline { stroke:#eab308; }
.timeline-projects { margin:10px 0 0; padding:0; list-style:none; }
.timeline-projects li { display:grid; grid-template-columns:minmax(0,1fr) 72px auto; gap:10px; padding:5px 0; border-top:1px solid var(--border-soft); font-size:11px; align-items:center; }
.timeline-project-name { display:flex; align-items:center; gap:7px; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#fffaf1; font-weight:650; }
.timeline-project-name i { width:8px; height:8px; border-radius:2px; flex:0 0 auto; }
.timeline-projects strong { color:var(--muted); white-space:nowrap; font-weight:600; }
.mini-sparkline { display:block; width:72px; height:18px; }
.mini-sparkline polyline { fill:none; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
.detail-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; }
.detail-window { border-top:1px solid var(--border-soft); padding-top:10px; margin-top:10px; }
dl { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin:0; }
dt { color:var(--muted); font-size:11px; }
dd { margin:2px 0 0; color:#fffaf1; font-weight:650; overflow-wrap:anywhere; }
.model-list li, .state-list li { display:flex; justify-content:space-between; gap:10px; padding:5px 0; border-bottom:1px solid var(--border-soft); }
.state-list span { color:var(--muted); }
.error-text, .state-list .error span { color:var(--fail); }
.limit-badge { display:inline-block; padding:3px 8px; border-radius:999px; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.3px; }
.limit-available { background:rgba(52,211,153,.14); color:#5ee0aa; }
.limit-watch { background:rgba(244,184,96,.15); color:#f4b860; }
.limit-limited { background:rgba(251,113,133,.16); color:#fb7185; }
.limit-unknown { background:rgba(148,163,184,.15); color:#94a3b8; }
@media (max-width: 980px) { .overview-grid, .runtime-grid, .detail-grid, .usage-grid, .timeline-grid, .session-grid, .session-metrics { grid-template-columns:1fr; } .runtime-windows, .qc-quota-split { grid-template-columns:1fr; } .session-card-wide { grid-column:auto; } }
@media (max-width: 640px) { header { padding:14px 16px; } main { padding:16px; } .project-list li, .timeline-projects li { grid-template-columns:minmax(0,1fr); } .project-list strong { text-align:left; } .mini-sparkline { display:none; } dl { grid-template-columns:1fr; } }
""".strip()