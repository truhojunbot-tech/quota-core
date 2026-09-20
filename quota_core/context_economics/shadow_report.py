"""Read-only actual-versus-recommended economics shadow reporting."""

from __future__ import annotations

from typing import Iterable

from .policy import POLICY_CONTRACT_VERSION, QualityEvidence, recommend_task_policy
from .pricing import PRICED_COMPONENTS, PricingBook, price_task
from .schema import TaskEconomicsRecord

#: Telemetry field behind each soft-budget component, so baseline usage and a
#: recommended envelope are compared on the same axis rather than by position.
_BUDGET_TOKEN_FIELD: dict[str, str] = {
    "uncached_input_tokens": "uncached_input_tokens",
    "cache_write_tokens": "cache_write_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "output_tokens": "output_tokens",
    "reasoning_tokens": "reasoning_tokens",
}


def _baseline_vs_recommended(
    record: TaskEconomicsRecord, soft_budget: dict[str, object] | None
) -> dict[str, object]:
    """Per-component observed usage against the recommended envelope (#80).

    ⛔Compared component by component, never as totals. `headroom` is
      recommended minus observed for one component; it is `None` whenever
      either side is unknown, because a difference against an unknown is not
      a number. A negative headroom is impossible by construction today (every
      multiplier is >= 1.0) but is reported as measured rather than clamped,
      so a future multiplier change cannot hide itself here.

    A task with no recommended envelope -- a failed or unfinished one, which
    has no valid budget anchor -- reports observed usage with `headroom: null`
    rather than inventing a comparison.
    """
    rows: dict[str, object] = {}
    for component, field_name in _BUDGET_TOKEN_FIELD.items():
        observed = getattr(record.task_telemetry, field_name)
        recommended = soft_budget.get(component) if isinstance(soft_budget, dict) else None
        headroom = (
            recommended - observed
            if isinstance(observed, int) and not isinstance(observed, bool)
            and isinstance(recommended, int) and not isinstance(recommended, bool)
            else None
        )
        rows[component] = {
            "observed": observed,
            "recommended": recommended,
            "headroom": headroom,
        }
    return rows


def _actual_session_treatment(record: TaskEconomicsRecord) -> str:
    """State only what the normalized context-policy provenance supports."""
    if record.context_policy == "resume":
        return "resume"
    if record.context_policy == "fresh":
        return "renew"
    return "unknown"


def _actual_review_fix_rounds() -> int | None:
    """No normalized source count exists yet; never infer one from retries."""
    return None


def shadow_comparison_report(
    records: Iterable[TaskEconomicsRecord],
    evidence_by_task: dict[str, QualityEvidence] | None = None,
    pricing: PricingBook | None = None,
    evidence_provenance: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    """Emit one policy artifact and an actual/recommended comparison per task.

    This is a report only. It never changes a task, selects a provider, or
    writes back to the source database. Round counts are ``null`` unless a
    future normalized contract exposes them explicitly; retries/fallbacks are
    reported as their real provenance links, never converted to guessed counts.
    """

    evidence_by_task = evidence_by_task or {}
    evidence_provenance = evidence_provenance or {}
    artifacts: list[dict[str, object]] = []
    for record in records:
        decision = recommend_task_policy(
            record, evidence_by_task.get(record.task_id, QualityEvidence())
        ).to_dict()
        actual = {
            "review_fix_rounds": _actual_review_fix_rounds(),
            "retry_of": record.retry_of,
            "fallback_of": record.fallback_of,
            "provider": record.provider or record.agent,
            "model": record.model,
            "session_treatment": _actual_session_treatment(record),
            "context_policy": record.context_policy,
        }
        recommended = {
            "max_review_fix_rounds": decision["recommended_max_review_fix_rounds"],
            "provider_tier": decision["recommended_provider_tier"],
            "session_treatment": decision["recommended_session_treatment"],
            "cache_treatment": decision["recommended_cache_treatment"],
            "soft_budget": decision["recommended_soft_budget"],
        }
        artifacts.append({
            "task_id": record.task_id,
            "runtime": record.runtime,
            "policy_decision": decision,
            "shadow_comparison": {"actual": actual, "recommended": recommended},
            # quota-core#80: the per-component baseline the recommendation is
            # measured against, plus what the task actually cost at the
            # caller's rates. Both are null-safe and neither is totalled.
            "baseline_vs_recommended": _baseline_vs_recommended(
                record, decision["recommended_soft_budget"]
            ),
            "cost": price_task(record, pricing).to_dict(),
            # Why each evidence field says what it says, so a reader can tell
            # "measured false" from "never recorded" without reading source.
            "evidence_provenance": dict(evidence_provenance.get(record.task_id, {})),
        })
    return {
        "contract_version": POLICY_CONTRACT_VERSION,
        "mode": "shadow",
        "report_kind": "actual_vs_recommended",
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }
