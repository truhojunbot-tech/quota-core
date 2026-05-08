"""Quota scanner/session reconciliation helpers."""

from __future__ import annotations

from typing import Any


def reconcile_totals(session_total_tokens: int, quota_scanner_total_tokens: int | None, notes: list[str] | None = None) -> dict[str, Any]:
    """Build the public reconciliation object for a session report."""

    delta = None if quota_scanner_total_tokens is None else int(session_total_tokens) - int(quota_scanner_total_tokens)
    delta_pct = None
    if delta is not None and quota_scanner_total_tokens:
        delta_pct = round(delta / quota_scanner_total_tokens * 100, 1)
    return {
        "quota_scanner_total_tokens": quota_scanner_total_tokens,
        "session_total_tokens": int(session_total_tokens),
        "delta_tokens": delta,
        "delta_pct": delta_pct,
        "notes": list(notes or []),
    }


__all__ = ["reconcile_totals"]