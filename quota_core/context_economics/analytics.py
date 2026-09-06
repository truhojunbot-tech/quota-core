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

from .schema import FailureCategory, TaskEconomicsRecord, token_components_total


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _successful(records: Iterable[TaskEconomicsRecord]) -> list[TaskEconomicsRecord]:
    return [r for r in records if r.succeeded]


def _not_succeeded(record: TaskEconomicsRecord) -> bool:
    """A task counts as not-succeeded once it has a known outcome that isn't "success".

    This is the single definition every analytics function in this module
    uses for "failed" -- there is intentionally no separate, narrower
    ``outcome == "failed"`` check anywhere else, so e.g. ``outcome="error"``
    or ``outcome="cancelled"`` are treated consistently as failures across
    :func:`failed_retry_token_waste` and :func:`context_age_vs_failure_rate`
    instead of silently disagreeing. A record with ``outcome=None`` (unknown)
    is excluded, not counted as failed.
    """

    return record.outcome is not None and record.outcome != "success"


# quota-core issue #60: a raw "not succeeded" count/rate conflates context-
# policy-relevant failures with provider outages, dispatcher bugs, work-
# product/test failures, and cancellations (the agent_crew#205/#206 AGY
# "subscriber fell behind" incident is the motivating real-world case -- 31
# tester failures that were a transient streaming/backpressure error, not
# evidence any context was stale). These buckets partition every "not
# succeeded" record by `failure_category` so a context-age/compact-policy
# report can read `policy_relevant_failure_rate` instead of
# `raw_failure_rate` when judging whether context handling caused failures.
#
# `work_product_or_test` is deliberately its OWN bucket, not folded into
# `policy_relevant` (round-1 review of issue #60 caught this: folding it in
# meant `policy_relevant_failure_rate` silently included ordinary test/lint
# failures that have nothing to do with context or compact/resume policy --
# and since `context_or_policy` is currently unreachable from any real
# reason string, in practice `policy_relevant` contained *only*
# work-product failures, the opposite of what its docstring claimed).
_PROVIDER_OR_RUNTIME_CATEGORIES: frozenset[str] = frozenset({"provider_or_transport", "runtime_or_dispatcher"})
_POLICY_RELEVANT_CATEGORIES: frozenset[str] = frozenset({"context_or_policy"})
_WORK_PRODUCT_CATEGORIES: frozenset[str] = frozenset({"work_product_or_test"})

UNKNOWN_FAILURE_CAUSE_WARNING = (
    "every observed failure in this group has an unknown failure_category "
    "(no failure-cause evidence available) -- policy_relevant_failure_rate "
    "is not meaningful here, only raw_failure_rate is"
)

# quota-core issue #60 round-1 review: the original warning only fired when
# *100%* of a group's failures were unclassified, so a group that is e.g.
# 90% unknown reported a confident-looking policy_relevant_failure_rate with
# no warning at all. This proportional threshold catches that case too, with
# separate wording so a reader can tell "zero evidence" from "mostly no
# evidence" apart.
PARTIAL_UNKNOWN_FAILURE_CAUSE_RATE_THRESHOLD = 0.5
PARTIAL_UNKNOWN_FAILURE_CAUSE_WARNING = (
    "at least half of this group's failures have an unknown failure_category "
    "(limited failure-cause evidence) -- policy_relevant_failure_rate here may "
    "understate the true rate; treat it as a lower bound, not a confident reading"
)


def _failure_bucket(category: FailureCategory | None) -> str:
    if category in _PROVIDER_OR_RUNTIME_CATEGORIES:
        return "provider_or_runtime_operational"
    if category in _WORK_PRODUCT_CATEGORIES:
        return "work_product_or_test"
    if category in _POLICY_RELEVANT_CATEGORIES:
        return "policy_relevant"
    if category == "cancelled":
        return "cancelled"
    return "unknown"


def unknown_cause_warning(stratified: dict[str, float | int | None]) -> str | None:
    """Pick the right unknown-failure-cause warning (if any) for a
    :func:`stratified_failure_rates` result, or ``None`` when none applies.

    ``UNKNOWN_FAILURE_CAUSE_WARNING`` when every observed failure in the
    group is unclassified; ``PARTIAL_UNKNOWN_FAILURE_CAUSE_WARNING`` when at
    least :data:`PARTIAL_UNKNOWN_FAILURE_CAUSE_RATE_THRESHOLD` (50%) are,
    even if not all of them; ``None`` when there are no failures at all, or
    when unknown failures are a minority.
    """

    raw_failure_count = stratified.get("raw_failure_count") or 0
    unknown_count = stratified.get("unknown_count") or 0
    if not raw_failure_count:
        return None
    if unknown_count == raw_failure_count:
        return UNKNOWN_FAILURE_CAUSE_WARNING
    if (unknown_count / raw_failure_count) >= PARTIAL_UNKNOWN_FAILURE_CAUSE_RATE_THRESHOLD:
        return PARTIAL_UNKNOWN_FAILURE_CAUSE_WARNING
    return None


def stratified_failure_rates(records: Iterable[TaskEconomicsRecord]) -> dict[str, float | int | None]:
    """Break a "not succeeded" rate down by `failure_category`.

    Returns, alongside the existing raw rate: `provider_or_runtime_operational`
    (provider/transport transient errors plus dispatcher/orchestration
    failures -- not evidence about context/policy), `work_product_or_test`
    (a positively-identified test/lint/assertion failure -- a real failure on
    the task's own merits, but still not evidence about context/policy),
    `policy_relevant` (context/policy causes only -- the signal
    context-age/compact comparisons should actually read), `cancelled`, and
    `unknown` (failed with no recognizable cause). Every count/rate pair is
    reported explicitly so a reader never has to infer a bucket's size from
    the others. A record whose `outcome` is `None` (in progress / not yet
    terminal) is excluded from every count and rate, matching
    `_not_succeeded`'s existing convention.
    """

    observed = [r for r in records if r.outcome is not None]
    total = len(observed)
    failed = [r for r in observed if _not_succeeded(r)]
    raw_failure_count = len(failed)

    bucket_counts: dict[str, int] = {
        "provider_or_runtime_operational": 0,
        "work_product_or_test": 0,
        "policy_relevant": 0,
        "cancelled": 0,
        "unknown": 0,
    }
    for r in failed:
        bucket_counts[_failure_bucket(r.failure_category)] += 1

    def _rate(count: int) -> float | None:
        return (count / total) if total else None

    return {
        "observed_count": total,
        "raw_failure_count": raw_failure_count,
        "raw_failure_rate": _rate(raw_failure_count),
        "provider_or_runtime_operational_count": bucket_counts["provider_or_runtime_operational"],
        "provider_or_runtime_operational_failure_rate": _rate(bucket_counts["provider_or_runtime_operational"]),
        "work_product_or_test_count": bucket_counts["work_product_or_test"],
        "work_product_or_test_failure_rate": _rate(bucket_counts["work_product_or_test"]),
        "policy_relevant_count": bucket_counts["policy_relevant"],
        "policy_relevant_failure_rate": _rate(bucket_counts["policy_relevant"]),
        "cancelled_count": bucket_counts["cancelled"],
        "cancelled_failure_rate": _rate(bucket_counts["cancelled"]),
        "unknown_count": bucket_counts["unknown"],
        "unknown_failure_rate": _rate(bucket_counts["unknown"]),
    }


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
    failed = [r for r in records if _not_succeeded(r)]
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


def context_age_vs_failure_rate(records: Iterable[TaskEconomicsRecord]) -> list[dict[str, float | int | None | str]]:
    """Failure rate per ``session_task_index`` (context age), raw and stratified.

    ``failure_rate``/``count`` are unchanged from before quota-core issue #60
    (existing callers keep working). Each row also carries
    :func:`stratified_failure_rates`'s breakdown so a caller can tell whether
    a spike in raw failure rate at a given context age is actually
    policy-relevant or just a provider outage/dispatcher bug that happened to
    land there. If every failure at this age has an unrecognized cause, a
    ``"warning"`` key explains that ``policy_relevant_failure_rate`` is not
    meaningful for that row.
    """

    by_age: dict[int, list[TaskEconomicsRecord]] = defaultdict(list)
    for r in records:
        if r.session_task_index is None or r.outcome is None:
            continue
        by_age[r.session_task_index].append(r)
    rows: list[dict[str, float | int | None | str]] = []
    for age, group in sorted(by_age.items()):
        outcomes = [_not_succeeded(r) for r in group]
        rate = sum(1 for failed in outcomes if failed) / len(outcomes) if outcomes else None
        row: dict[str, float | int | None | str] = {"session_task_index": age, "failure_rate": rate, "count": len(outcomes)}
        stratified = stratified_failure_rates(group)
        row.update(stratified)
        warning = unknown_cause_warning(stratified)
        if warning is not None:
            row["warning"] = warning
        rows.append(row)
    return rows


def compare_context_policies(records: Iterable[TaskEconomicsRecord]) -> dict[str, dict[str, float | int | None | str]]:
    """Compare resume vs compact vs fresh contexts by average tokens and success rate.

    A policy with zero observed records is omitted from the result entirely,
    rather than reporting a misleading 0/0 rate.

    Also exposes :func:`stratified_failure_rates`'s breakdown per policy
    (quota-core issue #60), plus a ``"warning"`` key (see
    :func:`unknown_cause_warning`) when a large share of the failures
    observed under a policy have an unrecognized cause -- so a
    resume/compact/fresh comparison never gets read as confident
    context-policy evidence when the underlying failure-cause data is mostly
    or entirely missing.
    """

    by_policy: dict[str, list[TaskEconomicsRecord]] = defaultdict(list)
    for r in records:
        by_policy[r.context_policy].append(r)

    result: dict[str, dict[str, float | int | None | str]] = {}
    for policy, rows in by_policy.items():
        totals = [token_components_total(r.tokens) for r in rows]
        known_totals = [float(t) for t in totals if t is not None]
        outcomes = [r.outcome for r in rows if r.outcome is not None]
        success_rate = (sum(1 for o in outcomes if o == "success") / len(outcomes)) if outcomes else None
        entry: dict[str, float | int | None | str] = {
            "avg_tokens": _mean(known_totals),
            "success_rate": success_rate,
            "count": len(rows),
        }
        stratified = stratified_failure_rates(rows)
        entry.update(stratified)
        warning = unknown_cause_warning(stratified)
        if warning is not None:
            entry["warning"] = warning
        result[policy] = entry
    return result


# quota-core issue #68: Agent Crew #278/#279 resolves a tester's actual
# `effective_test_scope` (targeted vs full_suite) per dispatch, since #275
# changed the default treatment from unconditional full-suite to targeted.
# Two test tasks with the same role/provider/project can cost radically
# different amounts depending on which treatment ran -- comparing them as one
# undifferentiated cohort would attribute a scope change's cost delta to
# something else entirely (context policy, provider variance, diff size).
_KNOWN_TEST_SCOPES: frozenset[str] = frozenset({"targeted", "full_suite"})


def test_treatment_cohorts(
    records: Iterable[TaskEconomicsRecord],
) -> dict[str, list[TaskEconomicsRecord]]:
    """Partition records by Agent Crew #279's resolved `effective_test_scope`.

    Three cohorts: `targeted`, `full_suite`, and `unknown` -- the last covers
    both a `None` scope (never resolved: not a test task, or a pre-#279
    historical row with no producer support yet) and any value this version
    doesn't recognize, so a future scope name added upstream degrades to
    "uncounted" rather than silently misclassified as one of the two known
    treatments. Every record appears in exactly one cohort; callers get an
    explicit sample-size denominator per cohort via `len(...)`.
    """

    cohorts: dict[str, list[TaskEconomicsRecord]] = {
        "targeted": [], "full_suite": [], "unknown": [],
    }
    for r in records:
        scope = r.effective_test_scope
        cohorts[scope if scope in _KNOWN_TEST_SCOPES else "unknown"].append(r)
    return cohorts


def test_treatment_failure_rates(
    records: Iterable[TaskEconomicsRecord],
) -> dict[str, dict[str, float | int | None]]:
    """:func:`stratified_failure_rates` computed separately per test-scope cohort.

    Targeted and full_suite are never pooled -- a pooled rate would let a
    scope-mix shift (e.g. more full_suite runs this week) masquerade as a
    context-policy or provider effect. Each cohort's result carries its own
    `observed_count`, so a caller can see immediately when one side of a
    targeted-vs-full_suite comparison has too few samples to mean anything
    (this function makes no significance claim itself). The `unknown` cohort
    is included for completeness, not for comparison -- most of it is
    non-test-task records, which is a different population entirely from
    "test task with a treatment we couldn't resolve".
    """

    return {
        name: stratified_failure_rates(rows)
        for name, rows in test_treatment_cohorts(records).items()
    }


def lock_wait_summary(records: Iterable[TaskEconomicsRecord]) -> dict[str, float | int | None]:
    """Scheduler lock-wait accounting for Agent Crew #272/#278's per-worktree
    test-stage lock, kept strictly separate from provider execution time and
    token economics.

    ⛔ `lock_wait_seconds` measures time a test task spent deferred behind
      another test task holding the same worktree's lock -- BEFORE the
      provider process is dispatched. It is never added into
      `duration_seconds`, any token total, or `stratified_failure_rates`:
      doing so would relabel scheduler contention as provider/context cost.
      A deferred attempt does not even get its own attribution row (the
      dispatcher writes none until the lock is acquired, per #279), so there
      is no separate "task record" to double-count here in the first place --
      this function only aggregates the wait already folded into the one
      record the eventually-dispatched attempt produced.

    `known_count` excludes records where `lock_wait_seconds is None` (not a
    test task, or a pre-#279 historical row) from every average -- a `None`
    is "never measured", not zero contention, and must not pull the mean
    toward zero.
    """

    rows = list(records)
    known = [r for r in rows if r.lock_wait_seconds is not None]
    waits = [r.lock_wait_seconds for r in known if r.lock_wait_seconds is not None]
    defer_counts = [r.lock_defer_count or 0 for r in known]
    deferred = [r for r in known if (r.lock_defer_count or 0) > 0]
    return {
        "observed_count": len(rows),
        "known_count": len(known),
        "unknown_count": len(rows) - len(known),
        "deferred_count": len(deferred),
        "total_lock_wait_seconds": sum(waits) if known else None,
        "mean_lock_wait_seconds": _mean(waits) if known else None,
        "max_lock_defer_count": max(defer_counts) if defer_counts else None,
    }


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
    "stratified_failure_rates",
    "unknown_cause_warning",
    "UNKNOWN_FAILURE_CAUSE_WARNING",
    "PARTIAL_UNKNOWN_FAILURE_CAUSE_WARNING",
    "PARTIAL_UNKNOWN_FAILURE_CAUSE_RATE_THRESHOLD",
    "test_treatment_cohorts",
    "test_treatment_failure_rates",
    "lock_wait_summary",
]
