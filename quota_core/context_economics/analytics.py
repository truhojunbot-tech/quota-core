"""Context-efficiency analytics over :class:`TaskEconomicsRecord` collections.

These functions expose the individual token components and outcome/aging
signals separately -- there is deliberately no single universal
``efficiency_score``. Callers who want a composite metric should build it
from these primitives with their own weighting, so the underlying
components stay visible.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .schema import TaskEconomicsRecord, token_components_total


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _successful(records: Iterable[TaskEconomicsRecord]) -> list[TaskEconomicsRecord]:
    return [r for r in records if r.succeeded]


def fresh_input_per_successful_task(records: Iterable[TaskEconomicsRecord]) -> float | None:
    """Average fresh/input tokens spent per successful task (unknowns excluded)."""

    values = [float(r.tokens.fresh_input) for r in _successful(records) if r.tokens.fresh_input is not None]
    return _mean(values)


def cache_creation_per_successful_task(records: Iterable[TaskEconomicsRecord]) -> float | None:
    """Average cache-creation/write tokens spent per successful task."""

    values = [float(r.tokens.cache_creation) for r in _successful(records) if r.tokens.cache_creation is not None]
    return _mean(values)


def cache_read_per_task(records: Iterable[TaskEconomicsRecord]) -> float | None:
    """Average cache-read tokens per task, regardless of outcome."""

    values = [float(r.tokens.cache_read) for r in records if r.tokens.cache_read is not None]
    return _mean(values)


def failed_retry_token_waste(records: Iterable[TaskEconomicsRecord]) -> dict[str, object]:
    """Tokens spent on failed attempts and on the retries that followed them.

    ``failed_tokens`` is the total spent on tasks whose own outcome was a
    failure -- work that produced no usable result. ``retry_tokens`` is the
    total spent re-doing that work (tasks where ``retry_of`` is set),
    counted separately since a retry may itself have succeeded.
    """

    records = list(records)
    failed = [r for r in records if r.outcome == "failed"]
    retries = [r for r in records if r.retry_of]
    failed_totals = [token_components_total(r.tokens) for r in failed]
    retry_totals = [token_components_total(r.tokens) for r in retries]
    return {
        "failed_task_count": len(failed),
        "failed_tokens": sum(t for t in failed_totals if t is not None) if any(t is not None for t in failed_totals) else None,
        "retry_task_count": len(retries),
        "retry_tokens": sum(t for t in retry_totals if t is not None) if any(t is not None for t in retry_totals) else None,
    }


def tokens_per_successful_task(records: Iterable[TaskEconomicsRecord]) -> float | None:
    """Average total tokens (provider total, else sum of known components) per successful task."""

    values = []
    for r in _successful(records):
        total = token_components_total(r.tokens)
        if total is not None:
            values.append(float(total))
    return _mean(values)


def tokens_per_outcome(records: Iterable[TaskEconomicsRecord]) -> dict[str, dict[str, float | int | None]]:
    """Average tokens and task count grouped by ``outcome`` (e.g. success/failed/review/test/merge)."""

    by_outcome: dict[str, list[int]] = defaultdict(list)
    for r in records:
        outcome = r.outcome or "unknown"
        total = token_components_total(r.tokens)
        if total is not None:
            by_outcome[outcome].append(total)
    return {
        outcome: {"avg_tokens": _mean([float(v) for v in totals]), "count": len(totals)}
        for outcome, totals in by_outcome.items()
    }


def context_age_vs_token_usage(records: Iterable[TaskEconomicsRecord]) -> list[dict[str, float | int | None]]:
    """Average tokens per task, grouped by ``session_task_index`` (context age)."""

    by_age: dict[int, list[int]] = defaultdict(list)
    for r in records:
        if r.session_task_index is None:
            continue
        total = token_components_total(r.tokens)
        if total is not None:
            by_age[r.session_task_index].append(total)
    return [
        {"session_task_index": age, "avg_tokens": _mean([float(v) for v in totals]), "count": len(totals)}
        for age, totals in sorted(by_age.items())
    ]


def context_age_vs_failure_rate(records: Iterable[TaskEconomicsRecord]) -> list[dict[str, float | int | None]]:
    """Failure rate per ``session_task_index`` (context age)."""

    by_age: dict[int, list[bool]] = defaultdict(list)
    for r in records:
        if r.session_task_index is None or r.outcome is None:
            continue
        by_age[r.session_task_index].append(r.outcome != "success")
    rows = []
    for age, outcomes in sorted(by_age.items()):
        rate = sum(1 for failed in outcomes if failed) / len(outcomes) if outcomes else None
        rows.append({"session_task_index": age, "failure_rate": rate, "count": len(outcomes)})
    return rows


def compare_context_policies(records: Iterable[TaskEconomicsRecord]) -> dict[str, dict[str, float | int | None]]:
    """Compare resume vs compact vs fresh contexts by average tokens and success rate.

    Returns an empty dict entry is omitted for a policy with zero observed
    records rather than reporting a misleading 0/0 rate.
    """

    by_policy: dict[str, list[TaskEconomicsRecord]] = defaultdict(list)
    for r in records:
        by_policy[r.context_policy].append(r)

    result: dict[str, dict[str, float | int | None]] = {}
    for policy, rows in by_policy.items():
        totals = [token_components_total(r.tokens) for r in rows]
        known_totals = [float(t) for t in totals if t is not None]
        outcomes = [r.outcome for r in rows if r.outcome is not None]
        success_rate = (sum(1 for o in outcomes if o == "success") / len(outcomes)) if outcomes else None
        result[policy] = {
            "avg_tokens": _mean(known_totals),
            "success_rate": success_rate,
            "count": len(rows),
        }
    return result


__all__ = [
    "fresh_input_per_successful_task",
    "cache_creation_per_successful_task",
    "cache_read_per_task",
    "failed_retry_token_waste",
    "tokens_per_successful_task",
    "tokens_per_outcome",
    "context_age_vs_token_usage",
    "context_age_vs_failure_rate",
    "compare_context_policies",
]
