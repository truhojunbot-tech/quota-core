"""Before-vs-after analysis primitives for compact/reset lifecycle events.

Given a compact or reset event on a context, compare the N task records
immediately before it to the N task records immediately after it. This is
the primitive `quota-ops` issue #7's compact telemetry is meant to feed --
it answers "did this proactive compact actually help?" from data instead of
intuition.
"""

from __future__ import annotations

from typing import Iterable

from .analytics import stratified_failure_rates, unknown_cause_warning
from .schema import ContextLifecycleEvent, TaskEconomicsRecord, token_components_total

_RESET_EVENT_TYPES = {"context_compacted", "context_reset"}


def _task_timestamp(record: TaskEconomicsRecord) -> int | None:
    if record.completed_at is not None:
        return record.completed_at
    return record.started_at


def _window_stats(records: list[TaskEconomicsRecord]) -> dict[str, float | int | None | str]:
    totals = [token_components_total(r.tokens) for r in records]
    known_totals = [float(t) for t in totals if t is not None]
    outcomes = [r.outcome for r in records if r.outcome is not None]
    durations = [r.duration_seconds for r in records if r.duration_seconds is not None]
    stats: dict[str, float | int | None | str] = {
        "task_count": len(records),
        "avg_tokens": (sum(known_totals) / len(known_totals)) if known_totals else None,
        "success_rate": (sum(1 for o in outcomes if o == "success") / len(outcomes)) if outcomes else None,
        "avg_duration_seconds": (sum(durations) / len(durations)) if durations else None,
    }
    # quota-core issue #60: expose the same policy-relevant vs
    # provider/runtime-operational failure split here as
    # analytics.context_age_vs_failure_rate, so a before/after compact
    # comparison isn't read as "compact made things worse" when the "after"
    # window's failures were actually a provider outage or dispatcher bug.
    stratified = stratified_failure_rates(records)
    stats.update(stratified)
    warning = unknown_cause_warning(stratified)
    if warning is not None:
        stats["warning"] = warning
    return stats


def before_after_compact(
    records: Iterable[TaskEconomicsRecord],
    events: Iterable[ContextLifecycleEvent],
    *,
    n: int = 5,
) -> list[dict[str, object]]:
    """Compare up to ``n`` tasks before vs after each compact/reset event.

    Matching is by ``context_id``. Events without a ``context_id`` are
    skipped -- there is nothing to scope the comparison to. Tasks with no
    usable timestamp are excluded from both windows.

    Known limitation: this assumes the runtime keeps the *same* ``context_id``
    across a compact/reset (true for Claude Code's own ``/compact``, which
    compresses in place). If a runtime instead mints a *new* context identity
    at compaction (bumping ``context_generation`` rather than reusing
    ``context_id``), the "after" window here will come back empty rather than
    silently attributing the wrong tasks -- callers comparing generations
    across a context split should join on ``context_generation`` lineage
    themselves rather than relying on this function alone.
    """

    all_records = list(records)
    comparisons: list[dict[str, object]] = []

    for event in events:
        if event.event_type not in _RESET_EVENT_TYPES:
            continue
        if not event.context_id:
            continue

        context_records = [
            r for r in all_records if r.context_id == event.context_id and _task_timestamp(r) is not None
        ]
        context_records.sort(key=lambda r: _task_timestamp(r))  # type: ignore[arg-type,return-value]

        before = [r for r in context_records if (_task_timestamp(r) or 0) <= event.timestamp]
        after = [r for r in context_records if (_task_timestamp(r) or 0) > event.timestamp]

        before_window = before[-n:] if n > 0 else before
        after_window = after[:n] if n > 0 else after

        comparisons.append(
            {
                "context_id": event.context_id,
                "event_type": event.event_type,
                "event_timestamp": event.timestamp,
                "provider": event.provider,
                "project": event.project,
                "before": _window_stats(before_window),
                "after": _window_stats(after_window),
            }
        )

    return comparisons


__all__ = ["before_after_compact"]
