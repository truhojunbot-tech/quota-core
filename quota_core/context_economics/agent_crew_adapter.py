"""Tolerant adapter for Agent Crew's context-identity/lifecycle telemetry contract.

Agent Crew issue #202 ("feat: expose durable context identity and lifecycle
telemetry") defines a durable attribution record and an append-only JSONL
lifecycle event stream. This module never imports ``agent_crew`` -- it only
reads the documented JSON shape from a file path the caller provides.

If #202 has not landed in a given Agent Crew checkout yet, this adapter
still works against the documented contract fixture under
``tests/fixtures/agent_crew/`` (see that directory's README), so quota-core
development is not blocked on Agent Crew's release schedule. Parsing is
deliberately tolerant: unknown event types are skipped, missing/older
fields fall back to safe defaults, and unrecognized top-level keys are
preserved under each record's ``extra`` dict rather than dropped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Iterator

from .schema import ContextLifecycleEvent, RuntimeAttribution, attribution_from_dict, lifecycle_event_from_dict


def _iter_jsonl(path: str | Path) -> Iterator[dict]:
    p = Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8", errors="replace") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # Tolerant: skip malformed lines instead of failing the whole file.
                continue
            if isinstance(record, dict):
                yield record


def read_attribution_jsonl(path: str | Path) -> list[RuntimeAttribution]:
    """Read an Agent Crew attribution JSONL file into public attribution records.

    Missing file, empty file, or malformed lines all degrade to an empty/partial
    list rather than raising -- Agent Crew being absent or not yet emitting this
    contract must never break a quota-core caller.
    """

    records: list[RuntimeAttribution] = []
    for raw in _iter_jsonl(path):
        try:
            records.append(attribution_from_dict(raw))
        except Exception:
            continue
    return records


def read_lifecycle_events_jsonl(path: str | Path) -> list[ContextLifecycleEvent]:
    """Read an Agent Crew context/task lifecycle event stream, tolerantly."""

    events: list[ContextLifecycleEvent] = []
    for raw in _iter_jsonl(path):
        event = lifecycle_event_from_dict(raw)
        if event is not None:
            events.append(event)
    return events


def _reconciliation_key(attribution: RuntimeAttribution) -> tuple[int, float]:
    """Rank a row: terminal (has a normalized outcome) beats in-progress, then latest wins."""

    timestamp = attribution.updated_at
    if timestamp is None:
        timestamp = attribution.completed_at
    if timestamp is None:
        timestamp = attribution.started_at
    return (1 if attribution.outcome is not None else 0, float(timestamp or 0))


def reconcile_attribution_by_task(attributions: Iterable[RuntimeAttribution]) -> list[RuntimeAttribution]:
    """Collapse Agent Crew's real attribution stream to one row per ``task_id``.

    Real Agent Crew ``attribution.jsonl`` is snapshot/event-like: a task
    typically gets a dispatch row (``outcome`` unset), zero or more progress
    updates, and a terminal row once it finishes -- all sharing the same
    ``task_id`` (quota-core issue #58 point 3). Reading every line as an
    independent task execution double/triple-counts the same task. This
    picks, per ``task_id``, whichever row has a terminal outcome and (among
    ties) the latest ``updated_at``/``completed_at``/``started_at`` --
    preserving each task_id's first-seen order in the output.
    """

    groups: dict[str, list[RuntimeAttribution]] = {}
    order: list[str] = []
    for a in attributions:
        if a.task_id not in groups:
            order.append(a.task_id)
        groups.setdefault(a.task_id, []).append(a)

    return [max(groups[task_id], key=_reconciliation_key) for task_id in order]


def filter_by_task(attributions: Iterable[RuntimeAttribution], task_id: str) -> list[RuntimeAttribution]:
    return [a for a in attributions if a.task_id == task_id]


def filter_by_context(attributions: Iterable[RuntimeAttribution], context_id: str) -> list[RuntimeAttribution]:
    return [a for a in attributions if a.context_id == context_id]


def filter_by_provider(attributions: Iterable[RuntimeAttribution], provider: str) -> list[RuntimeAttribution]:
    return [a for a in attributions if a.provider == provider]


def filter_by_time_range(
    attributions: Iterable[RuntimeAttribution],
    *,
    start: int | None = None,
    end: int | None = None,
) -> list[RuntimeAttribution]:
    """Attribution overlaps [start, end] if either endpoint is unset it is unbounded."""

    result = []
    for a in attributions:
        lo = a.started_at if a.started_at is not None else a.completed_at
        hi = a.completed_at if a.completed_at is not None else a.started_at
        if lo is None and hi is None:
            continue
        if start is not None and hi is not None and hi < start:
            continue
        if end is not None and lo is not None and lo > end:
            continue
        result.append(a)
    return result


def events_for_context(events: Iterable[ContextLifecycleEvent], context_id: str) -> list[ContextLifecycleEvent]:
    return sorted((e for e in events if e.context_id == context_id), key=lambda e: e.timestamp)


__all__ = [
    "read_attribution_jsonl",
    "read_lifecycle_events_jsonl",
    "reconcile_attribution_by_task",
    "filter_by_task",
    "filter_by_context",
    "filter_by_provider",
    "filter_by_time_range",
    "events_for_context",
]
