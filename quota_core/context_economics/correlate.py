"""Join runtime attribution with provider usage telemetry into task-level records.

Exact correlation is possible when both the attribution record and the usage
telemetry agree on a provider-native session id and the task's time window.
When that is not available, correlation falls back to project + time-range
heuristics. Confidence is always reported explicitly so downstream analytics
(and humans) can decide whether to trust an ambiguous join.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .schema import RuntimeAttribution, TaskEconomicsRecord, TokenComponents
from .token_components import merge_token_components

# Timestamps within this many seconds of a task's [started_at, completed_at]
# window still count as "overlapping" -- provider usage telemetry timestamps
# (e.g. an API request log line) do not line up to the millisecond with a
# runtime's own task-start/task-complete bookkeeping.
_WINDOW_SLOP_SECONDS = 30


@dataclass(frozen=True)
class ProviderUsageRecord:
    """One provider-reported usage observation, in the public/normalized shape.

    Produced by a provider adapter (Claude/Codex/Gemini) from raw telemetry;
    this type carries no private paths or credentials, only what is needed to
    correlate usage back to a task.
    """

    provider: str
    tokens: TokenComponents = field(default_factory=TokenComponents)
    provider_session_id: str | None = None
    project: str | None = None
    model: str | None = None
    started_at: int | None = None
    completed_at: int | None = None


def _overlaps(record: ProviderUsageRecord, start: int | None, end: int | None) -> bool:
    task_start = start if start is not None else end
    task_end = end if end is not None else start
    if task_start is None or task_end is None:
        return True
    r_start = record.started_at
    r_end = record.completed_at if record.completed_at is not None else record.started_at
    if r_start is None and r_end is None:
        # Usage record carries no timing info -- do not exclude it outright,
        # the caller already narrowed by session id/project.
        return True
    if r_start is None:
        r_start = r_end
    if r_end is None:
        r_end = r_start
    assert r_start is not None and r_end is not None
    lo = task_start - _WINDOW_SLOP_SECONDS
    hi = task_end + _WINDOW_SLOP_SECONDS
    return r_start <= hi and r_end >= lo


def _distance_to_window(record: ProviderUsageRecord, start: int | None, end: int | None) -> int:
    """0 if the record's own window overlaps [start, end] directly, else the gap in seconds.

    Used to pick a single best-matching task when a usage record's window
    (with slop) touches more than one candidate task, so one unit of usage
    is never counted as belonging to several tasks at once.
    """

    task_start = start if start is not None else end
    task_end = end if end is not None else start
    if task_start is None or task_end is None:
        return 0
    r_start = record.started_at if record.started_at is not None else record.completed_at
    r_end = record.completed_at if record.completed_at is not None else record.started_at
    if r_start is None or r_end is None:
        return 0
    if r_end < task_start:
        return task_start - r_end
    if r_start > task_end:
        return r_start - task_end
    return 0


def correlate_task_economics(
    attributions: Iterable[RuntimeAttribution],
    usage_records: Iterable[ProviderUsageRecord],
) -> list[TaskEconomicsRecord]:
    """Join attribution records with provider usage into :class:`TaskEconomicsRecord`.

    Each usage record is attributed to **at most one** task. When a record's
    window (with slop) overlaps more than one candidate task in the same
    provider session -- e.g. several quick sequential tasks close together --
    it is assigned exclusively to the task whose own window it is closest to
    (ties broken by task order), and the other candidate tasks' notes record
    that it was assigned elsewhere. Without this, a single unit of usage
    could be double- or triple-counted across every task it happens to
    overlap.

    Confidence tiers:
      - ``high``: matched by ``provider_session_id`` *and* the usage record(s)
        overlap the task's [started_at, completed_at] window, and this task
        won the exclusive assignment for at least one such record.
      - ``medium``: matched by ``provider_session_id`` only -- no usable task
        time window, no usage record overlapped it, or every overlapping
        record was exclusively assigned to a different task.
      - ``low``: matched via project + time-range heuristic only, or no
        matching usage telemetry was found at all (tokens stay unknown).
    """

    attribution_list = list(attributions)
    usage_list = list(usage_records)

    # Pass 1: for each attribution, find usage records sharing its provider_session_id
    # whose window overlaps it directly (candidate "high"-tier matches).
    window_candidates: list[list[ProviderUsageRecord]] = []
    for a in attribution_list:
        session_matches = [u for u in usage_list if a.provider_session_id and u.provider_session_id == a.provider_session_id]
        window_candidates.append(
            [u for u in session_matches if a.started_at is not None and _overlaps(u, a.started_at, a.completed_at)]
        )

    # Pass 2: resolve exclusivity -- each usage record (by identity) goes to the single
    # attribution whose window it is closest to.
    claims: dict[int, list[tuple[int, int]]] = {}  # id(record) -> [(attribution_index, distance), ...]
    for idx, a in enumerate(attribution_list):
        for u in window_candidates[idx]:
            claims.setdefault(id(u), []).append((idx, _distance_to_window(u, a.started_at, a.completed_at)))
    winner_index: dict[int, int] = {
        record_id: min(candidates, key=lambda pair: (pair[1], pair[0]))[0] for record_id, candidates in claims.items()
    }

    results: list[TaskEconomicsRecord] = []
    for idx, a in enumerate(attribution_list):
        matched: list[ProviderUsageRecord] = []
        notes: list[str] = []
        confidence: str = "low"

        won = [u for u in window_candidates[idx] if winner_index.get(id(u)) == idx]
        lost_count = len(window_candidates[idx]) - len(won)

        if won:
            matched = won
            confidence = "high"
            if lost_count:
                notes.append(
                    f"{lost_count} overlapping usage record(s) assigned to a different task with a closer time match"
                )
        else:
            session_matches = [u for u in usage_list if a.provider_session_id and u.provider_session_id == a.provider_session_id]
            unclaimed_session_matches = [u for u in session_matches if id(u) not in winner_index]
            if unclaimed_session_matches:
                matched = unclaimed_session_matches
                confidence = "medium"
                if a.started_at is None:
                    notes.append("matched by provider_session_id only; task has no time window to confirm overlap")
                elif window_candidates[idx]:
                    notes.append("all overlapping usage records were exclusively assigned to other tasks with a closer time match")
                else:
                    notes.append("provider_session_id matched but no usage record overlapped the task time window")
            elif session_matches:
                notes.append("provider_session_id matched but all overlapping usage records were exclusively assigned to other tasks")

        if not matched and a.project and a.started_at is not None:
            project_matches = [
                u for u in usage_list if u.project == a.project and u.provider_session_id is None and id(u) not in winner_index
            ]
            proj_window_matches = [u for u in project_matches if _overlaps(u, a.started_at, a.completed_at)]
            if proj_window_matches:
                matched = proj_window_matches
                confidence = "low"
                notes.append("matched by project + time-range heuristic only (no provider_session_id available)")

        if matched:
            tokens = merge_token_components([u.tokens for u in matched])
            if len(matched) > 1:
                notes.append(f"{len(matched)} usage records merged for this task")
        else:
            tokens = TokenComponents()
            notes.append("no matching usage telemetry found; token components unknown")

        results.append(
            TaskEconomicsRecord(
                task_id=a.task_id,
                runtime=a.runtime,
                tokens=tokens,
                project=a.project,
                provider=a.provider,
                model=a.model,
                role=a.role,
                agent=a.agent,
                task_type=a.task_type,
                context_id=a.context_id,
                provider_session_id=a.provider_session_id,
                context_policy=a.context_policy,
                context_generation=a.context_generation,
                session_task_index=a.session_task_index,
                retry_of=a.retry_of,
                fallback_of=a.fallback_of,
                started_at=a.started_at,
                completed_at=a.completed_at,
                outcome=a.outcome,
                attribution_confidence=confidence,  # type: ignore[arg-type]
                attribution_notes=tuple(notes),
            )
        )

    return results


__all__ = ["ProviderUsageRecord", "correlate_task_economics"]
