"""Reusable dashboard component helpers."""

from __future__ import annotations

import html

from quota_core.snapshot import AggregateBreakdown, NormalizedSnapshot, SnapshotWindow


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

    windows = "".join(window_panel(name, window) for name, window in snapshot.windows.items())
    warnings = "".join(f"<p class=\"qc-warning\">{html.escape(warning)}</p>" for warning in snapshot.warnings)
    return f'<section class="qc-provider"><h2>{source}</h2>{warnings}{windows}</section>'


def window_panel(name: str, window: SnapshotWindow) -> str:
    """Render one normalized quota/session window."""

    title = html.escape(name.replace("_", " ").title())
    state_tone = "warning" if window.stale else "success" if window.cache_state == "live" else "neutral"
    return (
        '<article class="qc-window">'
        f'<header><h3>{title}</h3>{badge(window.cache_state, state_tone)}</header>'
        f'{usage_bar(window.utilization)}'
        f'<dl class="qc-metrics"><div><dt>Tokens</dt><dd>{window.total_tokens:,}</dd></div>'
        f'<div><dt>Requests</dt><dd>{window.requests:,}</dd></div>'
        f'<div><dt>Utilization</dt><dd>{window.utilization * 100:.1f}%</dd></div></dl>'
        f'{aggregate_table("Projects", window.by_project)}'
        f'{aggregate_table("Models", window.by_model)}'
        f'{runtime_section(window)}'
        '</article>'
    )


def usage_bar(utilization: float) -> str:
    """Render utilization bar."""

    pct = max(0.0, min(100.0, utilization * 100))
    return '<div class="qc-bar" aria-label="usage"><span style="width: %.1f%%"></span></div>' % pct


def aggregate_table(title: str, rows: dict[str, AggregateBreakdown]) -> str:
    """Render project/model aggregate table."""

    if not rows:
        return ""
    body = "".join(_aggregate_row(name, aggregate) for name, aggregate in rows.items())
    safe_title = html.escape(title)
    return (
        '<section class="qc-table-wrap">'
        f'<h4>{safe_title}</h4>'
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
        f'<p>{runtime.total_tokens:,} tokens / {runtime.requests:,} requests</p>'
        f'{aggregate_table("Runtime Projects", runtime.by_project)}'
        '</section>'
    )


def stylesheet() -> str:
    """Return minimal dashboard CSS."""

    return """
body { margin: 0; font-family: system-ui, sans-serif; color: #172026; background: #f7f8fa; }
main { max-width: 1180px; margin: 0 auto; padding: 24px; }
.qc-provider { background: #fff; border: 1px solid #d8dee4; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
.qc-provider h2, .qc-window h3, .qc-table-wrap h4, .qc-runtime h4 { margin: 0 0 12px; }
.qc-window { border-top: 1px solid #e7ebef; padding-top: 14px; margin-top: 14px; }
.qc-window header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.qc-badge { display: inline-flex; align-items: center; min-height: 22px; padding: 0 8px; border-radius: 999px; font-size: 12px; background: #eef2f5; color: #334155; }
.qc-badge-success { background: #e8f5ee; color: #17643a; }
.qc-badge-warning { background: #fff4db; color: #7a4b00; }
.qc-badge-danger { background: #ffe8e8; color: #9b1c1c; }
.qc-badge-muted { background: #f1f3f5; color: #6b7280; }
.qc-bar { height: 10px; background: #e5e7eb; border-radius: 999px; overflow: hidden; }
.qc-bar span { display: block; height: 100%; background: #2563eb; }
.qc-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 14px 0; }
.qc-metrics div { background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 6px; padding: 10px; }
.qc-metrics dt { font-size: 12px; color: #64748b; }
.qc-metrics dd { margin: 4px 0 0; font-weight: 650; }
.qc-table { width: 100%; border-collapse: collapse; margin-bottom: 14px; }
.qc-table th, .qc-table td { text-align: left; border-bottom: 1px solid #e5e7eb; padding: 8px; font-size: 14px; }
.qc-table th { color: #64748b; font-weight: 600; }
.qc-warning { color: #7a4b00; }
""".strip()


def _aggregate_row(name: str, aggregate: AggregateBreakdown) -> str:
    safe_name = html.escape(name)
    return (
        "<tr>"
        f"<td>{safe_name}</td>"
        f"<td>{aggregate.total_tokens:,}</td>"
        f"<td>{aggregate.requests:,}</td>"
        f"<td>{aggregate.share_pct:.1f}%</td>"
        "</tr>"
    )
