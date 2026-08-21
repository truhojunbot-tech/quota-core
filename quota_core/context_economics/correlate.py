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


def correlate_task_economics(
    attributions: Iterable[RuntimeAttribution],
    usage_records: Iterable[ProviderUsageRecord],
) -> list[TaskEconomicsRecord]:
    """Join attribution records with provider usage into :class:`TaskEconomicsRecord`.

    Confidence tiers:
      - ``high``: matched by ``provider_session_id`` *and* the usage record(s)
        overlap the task's [started_at, completed_at] window.
      - ``medium``: matched by ``provider_session_id`` only (no usable task
        time window, or no usage record overlapped it).
      - ``low``: matched via project + time-range heuristic only, or no
        matching usage telemetry was found at all (tokens stay unknown).
    """

    usage_list = list(usage_records)
    results: list[TaskEconomicsRecord] = []

    for a in attributions:
        matched: list[ProviderUsageRecord] = []
        notes: list[str] = []
        confidence: str = "low"

        session_matches = [u for u in usage_list if a.provider_session_id and u.provider_session_id == a.provider_session_id]
        if session_matches:
            window_matches = [u for u in session_matches if _overlaps(u, a.started_at, a.completed_at)]
            if window_matches and a.started_at is not None:
                matched = window_matches
                confidence = "high"
            else:
                matched = session_matches
                confidence = "medium"
                if a.started_at is None:
                    notes.append("matched by provider_session_id only; task has no time window to confirm overlap")
                else:
                    notes.append("provider_session_id matched but no usage record overlapped the task time window")

        if not matched and a.project and a.started_at is not None:
            project_matches = [u for u in usage_list if u.project == a.project and u.provider_session_id is None]
            window_matches = [u for u in project_matches if _overlaps(u, a.started_at, a.completed_at)]
            if window_matches:
                matched = window_matches
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
