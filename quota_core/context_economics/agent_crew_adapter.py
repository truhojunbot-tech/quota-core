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

Failure classification (quota-core issue #60): real Agent Crew
``attribution.jsonl`` rows only ever expose a failure reason as the
colon-delimited suffix of ``outcome`` (e.g. ``"failed:dispatcher_timeout"``).
``schema.attribution_from_dict`` derives ``failure_reason``/
``failure_category``/``retryable`` from that suffix via
``schema.classify_failure_category`` -- this adapter does not need its own
parsing for that path. But in real local data, that suffix is almost always
an uninformative bare ``exit_1``/``dispatcher_timeout``: the dispatcher's own
richer triage of *why* (e.g. ``agy_quota_exhausted``, 321 of 413 -- 78% -- of
every real observed failure reason locally) is written to ``tasks.db``'s
``error_info`` column (``{"reason": "<tag>"}``) instead, keyed by the same
``task_id`` that ``attribution.jsonl`` rows carry. :func:`read_task_error_reasons`
and :func:`enrich_with_task_error_reasons` read and join that richer source
so a caller doesn't have to settle for reading ``attribution.jsonl`` alone
(round-1 review of issue #60 flagged that reading ``attribution.jsonl`` alone
left ~96% of observed failures unclassified in practice, defeating the
feature). This enrichment is optional, best-effort, additive information --
never a hard dependency: a missing/unreadable ``tasks.db`` degrades silently
back to ``attribution.jsonl``-only classification.

What Agent Crew's current contract does *not* expose anywhere (neither
``attribution.jsonl`` nor ``tasks.db``'s ``error_info``), and which would
materially improve classification if a future producer added it:

- a distinct ``terminal_source`` field (e.g. ``"agent_reported"`` vs
  ``"dispatcher"`` vs ``"provider"``) -- today the only signal is the
  ``reason`` substring itself, so ``terminal_source`` stays ``None`` unless
  a source dict explicitly supplies it;
- any field that positively attributes a failure to context/policy
  causation (stale context, a bad compact/resume decision) -- there is
  currently no such signal anywhere in ``tasks.db``'s ``error_info`` column
  or ``attribution.jsonl``'s ``outcome``, so ``"context_or_policy"`` is
  presently unreachable from real data (see ``schema.py``'s
  ``_CONTEXT_POLICY_MARKERS``).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple

from .schema import (
    ContextLifecycleEvent,
    ContextPackAttribution,
    RuntimeAttribution,
    attribution_from_dict,
    classify_failure_category,
    context_pack_attribution_from_event,
    infer_retryable,
    lifecycle_event_from_dict,
)


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


def context_pack_attributions_from_events(
    events: Iterable[ContextLifecycleEvent],
) -> list[ContextPackAttribution]:
    """Filter a lifecycle-event stream down to Context Pack telemetry
    (quota-core#62, Agent Crew #239's producer contract).

    Same file, same stream as :func:`read_lifecycle_events_jsonl` --
    ``"context_pack_built"`` events are interleaved with the other lifecycle
    event types in real ``context_events.jsonl`` data, not a separate file.
    A caller that only wants Context Pack telemetry filters after reading
    once; a caller that wants both keeps the original event list too.
    """

    attributions: list[ContextPackAttribution] = []
    for event in events:
        attribution = context_pack_attribution_from_event(event)
        if attribution is not None:
            attributions.append(attribution)
    return attributions


class _TaskErrorInfo(NamedTuple):
    status: str | None
    reason: str


def _read_task_error_info(db_path: str | Path) -> dict[str, _TaskErrorInfo]:
    """Read ``{task_id: (status, reason)}`` from Agent Crew's ``tasks.db``.

    ``error_info`` is a JSON blob shaped ``{"reason": "<tag>"}`` (real
    observed tags locally: ``agy_quota_exhausted``, ``exit_1``,
    ``transient_agy_timeout_max_retries``, ``no_result_submitted``,
    ``dispatcher_timeout``, ``transient_agy_subscriber_lag_max_retries``).
    ``status`` is the task's own lifecycle status column (``"failed"``,
    ``"done"``, ...) -- needed alongside ``reason`` because, in real local
    data, a large majority of tasks carrying an ``error_info`` reason never
    got a terminal row written to ``attribution.jsonl`` at all (see
    :func:`enrich_with_task_error_reasons`).

    Returns an empty dict -- never raises -- when the db file is missing,
    unreadable, lacks a ``tasks`` table/``error_info`` column, or a given
    row's ``error_info`` isn't the expected JSON shape. This is optional
    enrichment data, not a hard dependency of the adapter (quota-core issue
    #60 round-1 review): Agent Crew being absent, on an older schema, or
    the db simply being locked by a concurrent writer must never break a
    quota-core caller that only wants what ``attribution.jsonl`` alone can
    provide.
    """

    path = Path(db_path)
    if not path.exists():
        return {}

    info: dict[str, _TaskErrorInfo] = {}
    try:
        # Read-only URI connection: this file is Agent Crew's live dispatcher
        # database, potentially being written to concurrently -- quota-core
        # must never open it for writing.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        try:
            cursor = conn.execute(
                "SELECT task_id, status, error_info FROM tasks WHERE error_info IS NOT NULL AND error_info != ''"
            )
            rows = cursor.fetchall()
        except sqlite3.Error:
            return {}
        for task_id, status, error_info in rows:
            if not task_id or not error_info:
                continue
            try:
                parsed = json.loads(error_info)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(parsed, dict):
                continue
            reason = parsed.get("reason")
            if isinstance(reason, str) and reason:
                info[str(task_id)] = _TaskErrorInfo(status=str(status) if status else None, reason=reason)
    finally:
        conn.close()
    return info


def read_task_error_reasons(db_path: str | Path) -> dict[str, str]:
    """Read ``{task_id: reason}`` from Agent Crew's ``tasks.db`` ``error_info`` column.

    Convenience wrapper around :func:`_read_task_error_info` for callers who
    only want the reason string, not the status. See that function for the
    ``error_info`` shape and the tolerant-failure contract (missing/unreadable
    db degrades to ``{}``, never raises).
    """

    return {task_id: entry.reason for task_id, entry in _read_task_error_info(db_path).items()}


def _read_task_statuses(db_path: str | Path) -> dict[str, str]:
    """Read ``{task_id: status}`` for *every* row in Agent Crew's ``tasks.db``,
    regardless of whether ``error_info`` is set.

    Complements :func:`_read_task_error_info`, which only covers rows that
    carry a JSON ``error_info`` reason. In real local data, ``error_info`` is
    populated *exclusively* for ``status="failed"`` rows -- never for
    ``status="completed"`` -- so a symmetric success backfill in
    :func:`enrich_with_task_error_reasons` needs this separate, unfiltered
    read to see ``status="completed"`` rows at all. Without it, only
    failures could ever be backfilled from ``tasks.db``, which silently
    skews the failure rate upward whenever ``attribution.jsonl`` leaves many
    tasks' ``outcome`` unterminated (round-2 review of quota-core issue #60:
    measured on real local data, this asymmetry inflated the failure rate
    from an ~11% jsonl-only understatement to a ~75% overstatement -- worse
    than the bug it was meant to fix).

    Returns an empty dict -- never raises -- under the same tolerant-failure
    contract as :func:`_read_task_error_info`: a missing/unreadable db, or a
    ``tasks`` table without the expected columns, degrades silently.
    """

    path = Path(db_path)
    if not path.exists():
        return {}

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}

    statuses: dict[str, str] = {}
    try:
        try:
            cursor = conn.execute("SELECT task_id, status FROM tasks")
            rows = cursor.fetchall()
        except sqlite3.Error:
            return {}
        for task_id, status in rows:
            if not task_id or not status:
                continue
            statuses[str(task_id)] = str(status)
    finally:
        conn.close()
    return statuses


def enrich_with_task_error_reasons(
    attributions: Iterable[RuntimeAttribution],
    db_path: str | Path,
) -> list[RuntimeAttribution]:
    """Join ``tasks.db``'s richer task-state data onto ``attribution.jsonl``
    records by ``task_id`` (quota-core issue #60).

    Three distinct real-data gaps this closes:

    1. **The dominant failure case (round-1 review; 78% of every real
       observed ``tasks.db`` ``error_info`` row locally, all
       ``agy_quota_exhausted``):** the task's own ``tasks.db`` row has
       ``status="failed"``, but its ``attribution.jsonl`` stream never wrote
       a terminal row at all -- the reconciled attribution's ``outcome`` is
       ``None`` ("still in progress"), silently hiding a real, permanent
       failure. When this happens, this backfills ``outcome="failed"`` from
       ``tasks.db`` alone, and additionally pulls in the richer
       ``error_info`` reason (and the derived ``failure_category``/
       ``retryable``) when that row also has one.
    2. **The symmetric success case (round-2 review; this was the missing
       half of case 1):** the task's ``tasks.db`` row has
       ``status="completed"`` but ``attribution.jsonl`` never wrote a
       terminal row either. Case 1 alone backfilled only failures into
       ``outcome`` and never backfilled the matching successes, which made
       the *reported* failure rate on real local data go from an ~11%
       understatement (jsonl-only, the original bug) to a ~75%
       overstatement (only-failures-backfilled) -- worse than the bug it
       replaced, because every newly-visible failure entered the numerator
       while the far more numerous newly-visible successes never entered
       the denominator. This backfills ``outcome="success"`` from
       ``tasks.db`` alone for exactly this shape.
    3. **The minority case:** the attribution row already has
       ``outcome="failed"``, but its own reason (the colon-delimited
       ``outcome`` suffix) is a less specific bare tag (e.g. ``exit_1``) than
       ``tasks.db``'s triaged ``error_info`` one. ``failure_category``/
       ``retryable`` are re-derived from the richer reason.

    An attribution row with any other explicit, already-terminal outcome
    (``"success"``, ``"unknown"``, or an already-set ``"failed"`` with no
    richer reason available) is never overridden this way --
    ``attribution.jsonl``'s own terminal call always wins when it exists. A
    task with no ``tasks.db`` match at all (including when the db is
    entirely missing/unreadable) keeps its original ``attribution.jsonl``
    -only classification unchanged; this enrichment is additive, never a
    hard dependency.

    Deliberately conservative about which ``tasks.db`` ``status`` values are
    treated as authoritative: only the two literal values actually observed
    driving the real terminal population (``"failed"``, ``"completed"``) are
    backfilled. Other real observed statuses (``"needs_human"``,
    ``"pending"``, ``"blocked"``, ``"in_progress"``, ``"cancelled"``) are
    left alone -- they are not evidence of a definite success or failure,
    and guessing one would be exactly the kind of unevidenced fabrication
    issue #60 forbids.
    """

    info = _read_task_error_info(db_path)
    statuses = _read_task_statuses(db_path)
    if not info and not statuses:
        return list(attributions)

    enriched: list[RuntimeAttribution] = []
    for a in attributions:
        entry = info.get(a.task_id)
        status = statuses.get(a.task_id)

        if a.outcome == "failed":
            if entry is None:
                enriched.append(a)
                continue
            richer_reason = entry.reason
            if not richer_reason or richer_reason == a.failure_reason:
                enriched.append(a)
                continue
            richer_category = classify_failure_category("failed", f"failed:{richer_reason}")
            enriched.append(
                replace(
                    a,
                    failure_reason=richer_reason,
                    failure_category=richer_category,
                    retryable=infer_retryable(richer_reason),
                    extra={**a.extra, "failure_reason_source": "tasks_db"},
                )
            )
        elif a.outcome is None and status == "failed":
            # attribution.jsonl never terminated this task at all, but
            # tasks.db's own dispatcher marked it status=failed -- this is
            # the single highest-yield case in real local data (see
            # docstring above). `entry` (the error_info reason) may still be
            # unavailable even though status="failed" (real local data has
            # 49 such rows alongside 416 that do carry a reason) -- backfill
            # outcome either way, just without a reason when none exists.
            reason = entry.reason if entry is not None else None
            raw_tag = f"failed:{reason}" if reason else "failed"
            category = classify_failure_category("failed", raw_tag)
            extra_update = {**a.extra, "outcome_source": "tasks_db_status"}
            if reason:
                extra_update["failure_reason_source"] = "tasks_db"
            enriched.append(
                replace(
                    a,
                    outcome="failed",
                    raw_outcome=a.raw_outcome or raw_tag,
                    failure_reason=reason,
                    failure_category=category,
                    retryable=infer_retryable(reason),
                    extra=extra_update,
                )
            )
        elif a.outcome is None and status == "completed":
            # Symmetric counterpart to the branch above (round-2 review):
            # tasks.db's dispatcher marked this task done, but
            # attribution.jsonl never wrote a terminal row for it either.
            enriched.append(
                replace(
                    a,
                    outcome="success",
                    raw_outcome=a.raw_outcome or "completed",
                    extra={**a.extra, "outcome_source": "tasks_db_status"},
                )
            )
        else:
            enriched.append(a)
    return enriched


def read_attribution_jsonl_with_task_errors(
    attribution_path: str | Path,
    tasks_db_path: str | Path,
) -> list[RuntimeAttribution]:
    """Convenience: :func:`read_attribution_jsonl`, :func:`reconcile_attribution_by_task`,
    then :func:`enrich_with_task_error_reasons`.

    This is the recommended entry point for a caller that has both files
    available (the normal case -- both live under the same
    ``~/.agent_crew/<project>/`` directory). Falls back to
    ``attribution.jsonl``-only behavior automatically if ``tasks_db_path``
    doesn't exist or isn't readable.

    Always reconciles to one row per ``task_id`` before returning (round-2
    review of quota-core issue #60): real ``attribution.jsonl`` is
    snapshot/event-like -- a single task can have a dispatch row, zero or
    more progress rows, and a terminal row, all sharing one ``task_id`` (see
    :func:`reconcile_attribution_by_task`). Reading every raw line as an
    independent task execution double/triple-counts the same task, so a
    caller that counts rows directly (e.g. a failure-rate computation) would
    multiply a single real outcome across every duplicate row for that
    task -- measured on real local data, an earlier version of this that
    skipped reconciliation entirely inflated an already-wrong ~75% failure
    rate to ~96%.

    Reconciliation runs *before* enrichment (round-3 review of quota-core
    issue #60), not after: reconciliation's own tiebreak picks a task's
    terminal row over any in-progress row, but that only holds while
    "terminal" still means "``attribution.jsonl`` itself reported an
    outcome". Enriching first would give every one of a task's rows *some*
    outcome (including its still-in-progress rows, backfilled from
    ``tasks.db`` ``status``) before reconciliation ever ran, collapsing the
    tiebreak to "latest timestamp wins" for every task -- so a ``tasks.db``
    backfill on a non-terminal row could outrank a real, explicit terminal
    row from ``attribution.jsonl`` itself whenever the backfilled row
    happened to carry a later timestamp, contradicting the documented
    contract that ``attribution.jsonl``'s own terminal call always wins (see
    :func:`reconcile_attribution_by_task`). Reconciling the raw stream first
    means the tiebreak only ever sees an ``attribution.jsonl``-native
    terminal outcome as "terminal" -- a ``tasks.db`` backfill is applied
    afterward, strictly per task, and only when that task's winning row still
    has ``outcome=None``, so it can add information but never outrank an
    explicit terminal call regardless of timestamps.
    """

    attributions = read_attribution_jsonl(attribution_path)
    reconciled = reconcile_attribution_by_task(attributions)
    return enrich_with_task_error_reasons(reconciled, tasks_db_path)


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
    "read_attribution_jsonl_with_task_errors",
    "read_lifecycle_events_jsonl",
    "read_task_error_reasons",
    "enrich_with_task_error_reasons",
    "reconcile_attribution_by_task",
    "filter_by_task",
    "filter_by_context",
    "filter_by_provider",
    "filter_by_time_range",
    "events_for_context",
]
