"""Portable, recommendation-only task economics policy contract (#80)."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from math import ceil
from typing import Literal

from .schema import TaskEconomicsRecord

POLICY_CONTRACT_VERSION = "1.0"
PolicyMode = Literal["shadow"]
RiskTier = Literal["safety_or_live", "architecture", "routine", "review_or_test", "research"]
ProviderTier = Literal["preserve_current", "escalate_allowed"]
SessionTreatment = Literal["preserve", "renew", "insufficient_evidence"]
CacheTreatment = Literal["preserve", "no_cache_signal", "insufficient_evidence"]

# This intentionally describes the serialized output, rather than Python
# dataclass internals, so consumers in another runtime can pin a contract.
POLICY_CONTRACT_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://quota-core.dev/contracts/context-economics-policy/1.0",
    "title": "Quota Core shadow economics policy decision",
    "type": "object",
    "required": [
        "contract_version", "mode", "task_id", "provenance",
        "current_behavior", "risk_tier", "quality_preserving",
        "human_gate_required", "recommended_soft_budget",
        "recommended_max_review_fix_rounds", "recommended_provider_tier",
        "recommended_session_treatment", "recommended_cache_treatment",
        "component_costs", "orchestration_waste", "evidence", "rationale",
        "override_reasons", "confidence",
    ],
    "properties": {
        "contract_version": {"const": POLICY_CONTRACT_VERSION},
        "mode": {"const": "shadow"},
        "task_id": {"type": "string"},
        "provenance": {
            "type": "object",
            "required": ["provider", "model", "session", "context_id", "context_generation"],
            "properties": {
                "provider": {"type": ["string", "null"]},
                "model": {"type": ["string", "null"]},
                "session": {"type": ["string", "null"]},
                "context_id": {"type": ["string", "null"]},
                "context_generation": {"type": ["integer", "null"]},
            },
            "additionalProperties": False,
        },
        "current_behavior": {
            "type": "object",
            "required": ["provider", "model", "session", "context_id", "context_generation", "context_policy", "outcome", "retry_of", "fallback_of"],
            "properties": {
                "provider": {"type": ["string", "null"]},
                "model": {"type": ["string", "null"]},
                "session": {"type": ["string", "null"]},
                "context_id": {"type": ["string", "null"]},
                "context_generation": {"type": ["integer", "null"]},
                "context_policy": {"type": ["string", "null"]},
                "outcome": {"type": ["string", "null"]},
                "retry_of": {"type": ["string", "null"]},
                "fallback_of": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
        "risk_tier": {"enum": ["safety_or_live", "architecture", "routine", "review_or_test", "research"]},
        "quality_preserving": {"type": "boolean"},
        "human_gate_required": {"type": "boolean"},
        "recommended_soft_budget": {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["uncached_input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens", "reasoning_tokens"],
                    "properties": {
                        "uncached_input_tokens": {"type": ["integer", "null"], "minimum": 0},
                        "cache_write_tokens": {"type": ["integer", "null"], "minimum": 0},
                        "cache_read_tokens": {"type": ["integer", "null"], "minimum": 0},
                        "output_tokens": {"type": ["integer", "null"], "minimum": 0},
                        "reasoning_tokens": {"type": ["integer", "null"], "minimum": 0},
                    },
                    "additionalProperties": False,
                },
                {"type": "null"},
            ],
        },
        "recommended_max_review_fix_rounds": {"type": "integer", "minimum": 1},
        "recommended_provider_tier": {"enum": ["preserve_current", "escalate_allowed"]},
        "recommended_session_treatment": {"enum": ["preserve", "renew", "insufficient_evidence"]},
        "recommended_cache_treatment": {"enum": ["preserve", "no_cache_signal", "insufficient_evidence"]},
        "component_costs": {
            "type": "object",
            "required": ["components", "non_additive_components", "aggregation"],
            "properties": {
                "components": {
                    "type": "object",
                    "required": ["uncached_input", "cache_write", "cache_read", "output", "reasoning"],
                    "properties": {name: {"type": ["number", "null"], "minimum": 0} for name in ("uncached_input", "cache_write", "cache_read", "output", "reasoning")},
                    "additionalProperties": False,
                },
                "non_additive_components": {"const": ["reasoning"]},
                "aggregation": {"const": "prohibited_overlapping_components"},
            },
            "additionalProperties": False,
        },
        "orchestration_waste": {
            "type": "object",
            "required": ["stale_tokens", "misrouted_tokens", "duplicate_tokens"],
            "properties": {name: {"type": ["integer", "null"], "minimum": 0} for name in ("stale_tokens", "misrouted_tokens", "duplicate_tokens")},
            "additionalProperties": False,
        },
        "evidence": {
            "type": "object",
            "required": ["outcome", "independent_review_correct", "required_context_recalled", "context_growth_tokens", "retry_of", "fallback_of", "token_observations"],
            "properties": {
                "outcome": {"type": ["string", "null"]},
                "independent_review_correct": {"type": ["boolean", "null"]},
                "required_context_recalled": {"type": ["boolean", "null"]},
                "context_growth_tokens": {"type": ["integer", "null"], "minimum": 0},
                "retry_of": {"type": ["string", "null"]},
                "fallback_of": {"type": ["string", "null"]},
                "token_observations": {
                    "type": "object",
                    "required": ["uncached_input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens", "reasoning_tokens"],
                    "properties": {name: {"type": ["integer", "null"], "minimum": 0} for name in ("uncached_input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens", "reasoning_tokens")},
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
        "rationale": {"type": "array", "items": {"type": "string"}},
        "override_reasons": {"type": "array", "items": {"type": "string"}},
        "confidence": {"enum": ["high", "medium", "low"]},
    },
    "additionalProperties": False,
}


def policy_contract_schema() -> dict[str, object]:
    """Return an independent JSON-Schema descriptor for contract consumers."""
    return deepcopy(POLICY_CONTRACT_SCHEMA)


@dataclass(frozen=True)
class QualityEvidence:
    """Explicit public quality/risk facts; ``None`` means unknown, not false."""
    safety_or_live_change: bool | None = None
    broad_architecture_change: bool | None = None
    bounded_routine_fix: bool | None = None
    independent_review_correct: bool | None = None
    required_context_recalled: bool | None = None
    new_evidence_or_progress: bool | None = None
    repeated_unchanged_state: bool | None = None
    human_gate_required: bool | None = None
    context_growth_tokens: int | None = None
    stale_waste_tokens: int | None = None
    misrouted_waste_tokens: int | None = None
    duplicate_waste_tokens: int | None = None


@dataclass(frozen=True)
class ComponentPrice:
    """Optional per-token prices in a caller-defined currency."""
    uncached_input: float | None = None
    cache_write: float | None = None
    cache_read: float | None = None
    output: float | None = None
    reasoning: float | None = None


@dataclass(frozen=True)
class ProviderPricing:
    """Consumer-provided provider/model pricing; quota-core ships no table."""
    provider: str
    model: str | None
    price: ComponentPrice


@dataclass(frozen=True)
class SoftBudgetEnvelope:
    """Separate soft component limits. This type intentionally has no total."""
    uncached_input_tokens: int | None
    cache_write_tokens: int | None
    cache_read_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "uncached_input_tokens": self.uncached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass(frozen=True)
class PolicyDecision:
    """Stable machine-readable shadow recommendation for one task."""
    contract_version: str
    mode: PolicyMode
    task_id: str
    provenance: dict[str, str | int | None]
    current_behavior: dict[str, object]
    risk_tier: RiskTier
    quality_preserving: bool
    human_gate_required: bool
    recommended_soft_budget: SoftBudgetEnvelope | None
    recommended_max_review_fix_rounds: int
    recommended_provider_tier: ProviderTier
    recommended_session_treatment: SessionTreatment
    recommended_cache_treatment: CacheTreatment
    component_costs: dict[str, object]
    orchestration_waste: dict[str, int | None]
    evidence: dict[str, object]
    rationale: tuple[str, ...]
    override_reasons: tuple[str, ...]
    confidence: Literal["high", "medium", "low"]

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version, "mode": self.mode, "task_id": self.task_id,
            "provenance": dict(self.provenance), "risk_tier": self.risk_tier,
            "current_behavior": dict(self.current_behavior),
            "quality_preserving": self.quality_preserving, "human_gate_required": self.human_gate_required,
            "recommended_soft_budget": None if self.recommended_soft_budget is None else self.recommended_soft_budget.to_dict(),
            "recommended_max_review_fix_rounds": self.recommended_max_review_fix_rounds,
            "recommended_provider_tier": self.recommended_provider_tier,
            "recommended_session_treatment": self.recommended_session_treatment,
            "recommended_cache_treatment": self.recommended_cache_treatment,
            "component_costs": dict(self.component_costs), "orchestration_waste": dict(self.orchestration_waste),
            "evidence": dict(self.evidence),
            "rationale": list(self.rationale), "override_reasons": list(self.override_reasons), "confidence": self.confidence,
        }


def _risk(record: TaskEconomicsRecord, evidence: QualityEvidence) -> RiskTier:
    # Risk facts are tri-state. An unassessed task must not inherit the
    # low-scrutiny path merely because no caller supplied an assessment.
    risk_facts = (
        evidence.safety_or_live_change,
        evidence.broad_architecture_change,
        evidence.bounded_routine_fix,
        evidence.human_gate_required,
    )
    if any(fact is None for fact in risk_facts):
        return "safety_or_live"
    if evidence.safety_or_live_change is True or evidence.human_gate_required is True:
        return "safety_or_live"
    if evidence.broad_architecture_change is True:
        return "architecture"
    if record.task_type in {"review", "reviewer", "test", "tester"}:
        return "review_or_test"
    return "routine" if evidence.bounded_routine_fix is True else "research"


def _envelope(record: TaskEconomicsRecord, multiplier: float) -> SoftBudgetEnvelope:
    def limit(value: int | None) -> int | None:
        return ceil(value * multiplier) if value is not None else None
    t = record.task_telemetry
    return SoftBudgetEnvelope(limit(t.uncached_input_tokens), limit(t.cache_write_tokens), limit(t.cache_read_tokens), limit(t.output_tokens), limit(t.reasoning_tokens))


def _costs(record: TaskEconomicsRecord, pricing: ProviderPricing | None) -> dict[str, object]:
    """Return non-additive component costs; reasoning overlaps output."""
    names = ("uncached_input", "cache_write", "cache_read", "output", "reasoning")
    if pricing is None or pricing.provider != (record.provider or record.agent) or (pricing.model is not None and pricing.model != record.model):
        components = {name: None for name in names}
    else:
        t, p = record.task_telemetry, pricing.price
        values = {"uncached_input": t.uncached_input_tokens, "cache_write": t.cache_write_tokens, "cache_read": t.cache_read_tokens, "output": t.output_tokens, "reasoning": t.reasoning_tokens}
        components = {name: None if values[name] is None or getattr(p, name) is None else values[name] * getattr(p, name) for name in names}
    return {
        "components": components,
        "non_additive_components": ["reasoning"],
        "aggregation": "prohibited_overlapping_components",
    }


def recommend_task_policy(record: TaskEconomicsRecord, evidence: QualityEvidence = QualityEvidence(), pricing: ProviderPricing | None = None) -> PolicyDecision:
    """Return a deterministic recommendation only; never enforce or dispatch.

    Quality is the gate: missing or negative review/recall evidence preserves
    current treatment. Cost never reduces reasoning or forces a lower tier.
    """
    tier = _risk(record, evidence)
    quality = record.outcome == "success" and evidence.independent_review_correct is True and evidence.required_context_recalled is True
    overrides = []
    if record.outcome != "success": overrides.append("task_outcome_not_success")
    if evidence.independent_review_correct is not True: overrides.append("independent_review_correctness_unknown_or_negative")
    if evidence.required_context_recalled is not True: overrides.append("required_context_recall_unknown_or_negative")
    if evidence.human_gate_required is True: overrides.append("human_gate_required")
    multiplier = {"safety_or_live": 1.5, "architecture": 1.4, "routine": 1.2, "review_or_test": 1.25, "research": 1.3}[tier]
    rounds = {"safety_or_live": 3, "architecture": 2, "routine": 1, "review_or_test": 1, "research": 1}[tier] + (1 if evidence.new_evidence_or_progress is True else 0)
    if evidence.repeated_unchanged_state is True:
        rounds = max(1, rounds - 1)
    waste = {"stale_tokens": evidence.stale_waste_tokens, "misrouted_tokens": evidence.misrouted_waste_tokens, "duplicate_tokens": evidence.duplicate_waste_tokens}
    renewable = any(value is not None and value > 0 for value in waste.values())
    cache_seen = record.task_telemetry.cache_read_tokens is not None
    confidence: Literal["high", "medium", "low"] = "high" if quality and len(record.task_telemetry.observed_components) >= 3 else "medium" if record.task_telemetry.observed_components else "low"
    rationale = ["shadow_only_no_enforcement", f"risk_tier:{tier}", "reasoning_effort_never_reduced_for_cost"]
    if cache_seen: rationale.append("cache_read_is_productive_measurement_not_waste")
    if evidence.new_evidence_or_progress is True: rationale.append("new_evidence_extends_review_fix_envelope")
    if evidence.repeated_unchanged_state is True: rationale.append("unchanged_state_does_not_extend_review_fix_envelope")
    budget = _envelope(record, multiplier) if record.outcome == "success" else None
    if budget is None:
        overrides.append("no_budget_anchor_for_non_successful_or_unknown_outcome")
    return PolicyDecision(
        POLICY_CONTRACT_VERSION, "shadow", record.task_id,
        {"provider": record.provider or record.agent, "model": record.model, "session": record.provider_session_id, "context_id": record.context_id, "context_generation": record.context_generation},
        {"provider": record.provider or record.agent, "model": record.model,
         "session": record.provider_session_id, "context_id": record.context_id,
         "context_generation": record.context_generation, "context_policy": record.context_policy,
         "outcome": record.outcome, "retry_of": record.retry_of, "fallback_of": record.fallback_of},
        tier, quality, evidence.human_gate_required is True, budget, rounds,
        "escalate_allowed" if quality and tier in {"safety_or_live", "architecture"} else "preserve_current",
        "renew" if quality and renewable else "preserve" if quality else "insufficient_evidence",
        "preserve" if quality and cache_seen else "no_cache_signal" if quality else "insufficient_evidence",
        _costs(record, pricing), waste,
        {"outcome": record.outcome, "independent_review_correct": evidence.independent_review_correct,
         "required_context_recalled": evidence.required_context_recalled, "context_growth_tokens": evidence.context_growth_tokens,
         "retry_of": record.retry_of, "fallback_of": record.fallback_of,
         "token_observations": {
             name: getattr(record.task_telemetry, name)
             for name in ("uncached_input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens", "reasoning_tokens")
         }},
        tuple(rationale), tuple(overrides), confidence,
    )


def shadow_policy_report(records: list[TaskEconomicsRecord], evidence_by_task: dict[str, QualityEvidence] | None = None) -> dict[str, object]:
    """One deterministic shadow artifact per supplied task, without actions."""
    evidence_by_task = evidence_by_task or {}
    decisions = [recommend_task_policy(r, evidence_by_task.get(r.task_id, QualityEvidence())).to_dict() for r in records]
    return {"contract_version": POLICY_CONTRACT_VERSION, "mode": "shadow", "decision_count": len(decisions), "decisions": decisions}
