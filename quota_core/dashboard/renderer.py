"""Render normalized snapshots to HTML."""

from __future__ import annotations

import html

from quota_core.snapshot import NormalizedSnapshot
from quota_core.dashboard.components import provider_summary, stylesheet


def render_snapshot(snapshot: NormalizedSnapshot) -> str:
    """Render a minimal HTML fragment for one normalized snapshot."""

    _ = html
    return provider_summary(snapshot)


def render_page(snapshots: list[NormalizedSnapshot], title: str = "quota_core") -> str:
    """Render a complete HTML page for normalized snapshots."""

    safe_title = html.escape(title)
    body = "".join(render_snapshot(snapshot) for snapshot in snapshots)
    if not body:
        body = '<section class="qc-provider qc-provider-empty"><h2>No snapshots</h2></section>'
    return (
        "<!doctype html>"
        "<html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{safe_title}</title>"
        f"<style>{stylesheet()}</style>"
        "</head><body>"
        f"<main><h1>{safe_title}</h1>{body}</main>"
        "</body></html>"
    )
