"""Context Pack composition and retrieval-efficiency analytics (quota-core#62).

Consumes :class:`ContextPackAttribution` records (Agent Crew #239's real
producer contract, see that dataclass's docstring). This module answers the
"Context composition" and "Retrieval efficiency" sections of issue #62's
metrics scope. It deliberately does NOT cover:

- "Context Pack value indicators" (no-pack vs pack, lexical vs semantic/
  hybrid outcome comparisons) -- these need a real paired sample of tasks
  run both ways, which does not exist on any fleet today (see
  quota-core#62's KNOWN_LIMITATIONS-equivalent discussion); left for a
  follow-up issue once real data exists rather than built against fixtures
  that would only prove the arithmetic works, not that the comparison is
  meaningful.
- "Compact/retrieval interaction" -- needs real paired compact-lifecycle
  and Context Pack telemetry for the same tasks, same reasoning as above.

Every rate/mean below is computed over only the attributions that actually
carry the relevant field, with its own denominator reported alongside it
(``*_known_count``) -- a producer that hasn't started reporting a field yet
must never silently drag a rate toward zero.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .schema import ContextPackAttribution


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile over ``values`` (0.0-1.0). ``None`` for an
    empty list -- there is no percentile of nothing, and 0 would misrepresent
    "no data" as "zero latency"."""

    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct * (len(ordered) - 1))))
    return ordered[idx]


def _mean(values: list[float] | list[int]) -> float | None:
    return (sum(values) / len(values)) if values else None


def context_composition(attributions: Iterable[ContextPackAttribution]) -> dict:
    """Aggregate token composition across a set of Context Pack attributions.

    ``category_totals`` only contains keys some attribution actually
    reported in its ``tokens_by_category`` dict -- a category no producer
    version has ever emitted (e.g. a future category) simply never appears,
    rather than appearing as a fabricated 0.
    """

    packs = list(attributions)
    if not packs:
        return {
            "pack_count": 0,
            "total_tokens_known_count": 0,
            "mean_total_tokens": None,
            "category_totals": {},
            "category_share": {},
        }

    total_tokens_values = [p.total_tokens for p in packs if p.total_tokens is not None]

    category_totals: dict[str, int] = defaultdict(int)
    for pack in packs:
        for key, value in pack.tokens_by_category.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                category_totals[key] += int(value)

    grand_total = sum(category_totals.values())
    category_share = {k: v / grand_total for k, v in category_totals.items()} if grand_total else {}

    return {
        "pack_count": len(packs),
        "total_tokens_known_count": len(total_tokens_values),
        "mean_total_tokens": _mean(total_tokens_values),
        "category_totals": dict(category_totals),
        "category_share": category_share,
    }


def context_pack_efficiency(attributions: Iterable[ContextPackAttribution]) -> dict:
    """Retrieval-efficiency indicators: candidate->selected compression,
    latency percentiles, and degraded/stale/conflict rates.

    ``candidate_count == 0`` is excluded from the compression-ratio sample
    (a division-by-zero pack, not a 0% or 100% compression pack) rather than
    silently dropped without a trace -- see ``candidate_selected_known_count``
    for the actual denominator used.
    """

    packs = list(attributions)

    compression_ratios = [
        pack.selected_count / pack.candidate_count
        for pack in packs
        if pack.candidate_count is not None
        and pack.selected_count is not None
        and pack.candidate_count > 0
    ]
    latencies = [pack.latency_ms for pack in packs if pack.latency_ms is not None]
    degraded_flags = [pack.degraded for pack in packs if pack.degraded is not None]
    stale_counts = [pack.stale_count for pack in packs if pack.stale_count is not None]
    conflict_counts = [pack.conflict_count for pack in packs if pack.conflict_count is not None]

    return {
        "pack_count": len(packs),
        "candidate_selected_known_count": len(compression_ratios),
        "mean_compression_ratio": _mean(compression_ratios),
        "latency_known_count": len(latencies),
        "latency_ms_p50": _percentile(latencies, 0.50),
        "latency_ms_p95": _percentile(latencies, 0.95),
        "degraded_known_count": len(degraded_flags),
        "degraded_rate": (sum(1 for d in degraded_flags if d) / len(degraded_flags)) if degraded_flags else None,
        "stale_known_count": len(stale_counts),
        "mean_stale_count": _mean([float(c) for c in stale_counts]),
        "conflict_known_count": len(conflict_counts),
        "mean_conflict_count": _mean([float(c) for c in conflict_counts]),
    }


def retrieval_mode_comparison(attributions: Iterable[ContextPackAttribution]) -> dict:
    """Break composition and efficiency down per retrieval ``mode`` (e.g.
    ``lexical``/``semantic``/``hybrid``), each carrying its own sample size.

    Issue #62 acceptance criteria: "APIs expose lexical vs semantic/hybrid
    ... comparisons with sample sizes" -- never a bare aggregate that hides
    how thin one side of a comparison is. A pack with no reported ``mode``
    groups under the literal key ``"unknown"``, not silently dropped or
    merged into any real mode.
    """

    by_mode: dict[str, list[ContextPackAttribution]] = defaultdict(list)
    for pack in attributions:
        by_mode[pack.mode or "unknown"].append(pack)

    return {
        mode: {
            "sample_size": len(packs),
            "composition": context_composition(packs),
            "efficiency": context_pack_efficiency(packs),
        }
        for mode, packs in by_mode.items()
    }
