"""Render normalized snapshots to HTML."""

from __future__ import annotations

import html

from quota_core.snapshot import NormalizedSnapshot
from quota_core.dashboard.components import dashboard_overview, provider_summary, stylesheet

AUTO_REFRESH_MS = 60_000


def render_snapshot(snapshot: NormalizedSnapshot) -> str:
    """Render a minimal HTML fragment for one normalized snapshot."""

    _ = html
    return provider_summary(snapshot)


def render_page(snapshots: list[NormalizedSnapshot], title: str = "quota_core") -> str:
    """Render a complete HTML page for normalized snapshots."""

    safe_title = html.escape(title)
    body = dashboard_overview(snapshots)
    if not body:
        body = '<section class="qc-provider qc-provider-empty"><h2>No snapshots</h2></section>'
    return (
        "<!doctype html>"
        '<html lang="ko"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{safe_title}</title>"
        f"<style>{stylesheet()}</style>"
        f"<script>setInterval(function(){{window.location.reload();}},{AUTO_REFRESH_MS});</script>"
        "</head><body>"
        f"<header><h1>LLM Dashboard</h1><span class=\"updated\">{safe_title}</span></header>"
        f"<main>{body}</main>"
        "</body></html>"
    )
