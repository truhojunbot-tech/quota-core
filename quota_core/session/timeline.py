"""Timeline helpers for normalized session analytics."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


def day_key(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def peak_concurrency(session_spans: list[dict[str, Any]], *, bucket_minutes: int = 10) -> tuple[int, int]:
    buckets = [0] * int(24 * 60 / bucket_minutes)
    for span in session_spans:
        start_minute = int(span.get("start_minute") or 0)
        end_minute = max(start_minute + 1, int(span.get("end_minute") or start_minute + 1))
        lo = max(0, min(len(buckets) - 1, start_minute // bucket_minutes))
        hi = max(lo + 1, min(len(buckets), (end_minute + bucket_minutes - 1) // bucket_minutes))
        for index in range(lo, hi):
            buckets[index] += 1
    peak = max(buckets) if buckets else 0
    return peak, buckets.index(peak) * bucket_minutes if peak else 0


def build_day_rows(session_spans: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    days: dict[str, dict[str, Any]] = {}
    day_projects: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for span in session_spans.values():
        first_ts = span.get("first_ts")
        last_ts = span.get("last_ts") or first_ts
        tokens = int(span.get("tokens") or 0)
        if first_ts is None or tokens <= 0:
            continue
        date = day_key(int(first_ts))
        base = datetime.fromtimestamp(int(first_ts), timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start_minute = max(0, int((int(first_ts) - int(base.timestamp())) / 60))
        end_minute = max(start_minute + 1, int((int(last_ts) - int(base.timestamp())) / 60))
        day = days.setdefault(date, {"date": date, "tokens": 0, "sessions": 0, "_spans": []})
        day["tokens"] += tokens
        day["sessions"] += 1
        day["_spans"].append({"start_minute": start_minute, "end_minute": end_minute})
        day_projects[date][str(span.get("project") or "unknown")] += tokens
    rows = []
    for date, day in sorted(days.items()):
        peak, peak_at = peak_concurrency(day.pop("_spans"))
        total = int(day["tokens"])
        projects = [
            {"name": name, "total_tokens": tokens, "share_pct": round(tokens / total * 100, 1) if total else 0.0}
            for name, tokens in sorted(day_projects[date].items(), key=lambda item: -item[1])[:5]
        ]
        rows.append({**day, "peak_concurrency": peak, "peak_at_minute": peak_at, "top_projects": projects})
    return rows


__all__ = ["build_day_rows", "day_key", "peak_concurrency"]