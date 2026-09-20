"""Read-only actual-versus-recommended economics shadow reporting."""

from __future__ import annotations

from typing import Iterable

from .policy import POLICY_CONTRACT_VERSION, QualityEvidence, recommend_task_policy
from .schema import TaskEconomicsRecord


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
) -> dict[str, object]:
    """Emit one policy artifact and an actual/recommended comparison per task.

    This is a report only. It never changes a task, selects a provider, or
    writes back to the source database. Round counts are ``null`` unless a
    future normalized contract exposes them explicitly; retries/fallbacks are
    reported as their real provenance links, never converted to guessed counts.
    """

    evidence_by_task = evidence_by_task or {}
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
            "policy_decision": decision,
            "shadow_comparison": {"actual": actual, "recommended": recommended},
        })
    return {
        "contract_version": POLICY_CONTRACT_VERSION,
        "mode": "shadow",
        "report_kind": "actual_vs_recommended",
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }
