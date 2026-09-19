"""Contract tests for quota-core#80's recommendation-only policy engine."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from quota_core.context_economics import (
    ComponentPrice, ProviderPricing, QualityEvidence, attribution_from_dict,
    correlate_task_economics, policy_contract_schema, recommend_task_policy,
    shadow_policy_report,
)
from quota_core.context_economics.agent_crew_adapter import read_attribution_jsonl

FIXTURE = Path(__file__).parent / "fixtures" / "agent_crew" / "economics_policy" / "attribution.jsonl"


def _record(**fields):
    row = {"runtime": "portable", "task_id": "organic-redacted-1", "outcome": "completed", "task_type": "implement", "agent": "provider-a"}
    row.update(fields)
    return correlate_task_economics([attribution_from_dict(row)], [])[0]


class EconomicsPolicyTests(unittest.TestCase):
    def test_quality_veto_blocks_cost_cutting_and_keeps_unknowns_unknown(self):
        decision = recommend_task_policy(_record(uncached_input_tokens=0, cache_read_tokens=None))
        self.assertFalse(decision.quality_preserving)
        self.assertEqual(decision.recommended_provider_tier, "escalate_allowed")
        self.assertTrue(decision.human_gate_required)
        self.assertEqual(decision.recommended_cache_treatment, "insufficient_evidence")
        self.assertEqual(decision.recommended_soft_budget.uncached_input_tokens, 0)
        self.assertIsNone(decision.recommended_soft_budget.cache_read_tokens)
        self.assertIn("independent_review_correctness_unknown_or_negative", decision.override_reasons)

    def test_risk_is_characteristics_not_token_volume_and_new_evidence_extends_rounds(self):
        decision = recommend_task_policy(_record(uncached_input_tokens=1), QualityEvidence(
            safety_or_live_change=True, independent_review_correct=True,
            required_context_recalled=True, new_evidence_or_progress=True,
        ))
        self.assertEqual(decision.risk_tier, "safety_or_live")
        self.assertEqual(decision.recommended_max_review_fix_rounds, 4)
        self.assertEqual(decision.recommended_provider_tier, "escalate_allowed")

    def test_unassessed_risk_defaults_to_highest_scrutiny_not_research(self):
        decision = recommend_task_policy(_record(uncached_input_tokens=1))
        self.assertEqual(decision.risk_tier, "safety_or_live")
        self.assertEqual(decision.recommended_max_review_fix_rounds, 3)
        self.assertTrue(decision.human_gate_required)

    def test_high_risk_failed_review_still_escalates_for_more_scrutiny(self):
        decision = recommend_task_policy(_record(), QualityEvidence(
            safety_or_live_change=True, broad_architecture_change=False,
            bounded_routine_fix=False, human_gate_required=False,
            independent_review_correct=False, required_context_recalled=True,
        ))
        self.assertFalse(decision.quality_preserving)
        self.assertEqual(decision.recommended_provider_tier, "escalate_allowed")

    def test_costs_are_provider_specific_and_components_are_never_totaled(self):
        pricing = ProviderPricing("provider-a", None, ComponentPrice(uncached_input=.1, cache_read=.01, output=.2, reasoning=.3))
        decision = recommend_task_policy(_record(uncached_input_tokens=10, cache_read_tokens=20, output_tokens=5, reasoning_tokens=2), QualityEvidence(independent_review_correct=True, required_context_recalled=True), pricing)
        self.assertEqual(decision.component_costs["components"]["uncached_input"], 1.0)
        self.assertEqual(decision.component_costs["components"]["cache_read"], .2)
        self.assertEqual(decision.component_costs["components"]["reasoning"], .6)
        self.assertNotIn("total", decision.component_costs)
        self.assertEqual(decision.component_costs["non_additive_components"], ["reasoning"])
        self.assertEqual(decision.component_costs["aggregation"], "prohibited_overlapping_components")
        self.assertEqual(decision.recommended_cache_treatment, "preserve")

    def test_pricing_for_a_different_model_is_unknown_not_reused(self):
        decision = recommend_task_policy(_record(model="model-b", uncached_input_tokens=10), QualityEvidence(independent_review_correct=True, required_context_recalled=True), ProviderPricing("provider-a", "model-a", ComponentPrice(uncached_input=.1)))
        self.assertIsNone(decision.component_costs["components"]["uncached_input"])

    def test_shadow_report_is_deterministic_and_has_one_artifact_per_task(self):
        first = shadow_policy_report([_record(task_id="a"), _record(task_id="b", task_type="review")])
        self.assertEqual(first, shadow_policy_report([_record(task_id="a"), _record(task_id="b", task_type="review")]))
        self.assertEqual(first["mode"], "shadow")
        self.assertEqual(first["decision_count"], 2)
        self.assertEqual({d["task_id"] for d in first["decisions"]}, {"a", "b"})
        self.assertIn("current_behavior", first["decisions"][0])
        json.dumps(first)

    def test_json_schema_is_versioned_and_returned_defensively(self):
        schema = policy_contract_schema()
        self.assertEqual(schema["properties"]["contract_version"]["const"], "1.0")
        self.assertIn("current_behavior", schema["required"])
        budget = schema["properties"]["recommended_soft_budget"]["oneOf"][0]
        self.assertEqual(set(budget["required"]), {
            "uncached_input_tokens", "cache_write_tokens", "cache_read_tokens",
            "output_tokens", "reasoning_tokens",
        })
        self.assertFalse(budget["additionalProperties"])
        self.assertNotIn("total", budget["properties"])
        costs = schema["properties"]["component_costs"]
        self.assertFalse(costs["additionalProperties"])
        self.assertEqual(costs["properties"]["components"]["required"], [
            "uncached_input", "cache_write", "cache_read", "output", "reasoning",
        ])
        schema["required"].append("mutated_by_caller")
        self.assertNotIn("mutated_by_caller", policy_contract_schema()["required"])

    def test_failed_or_unknown_outcome_has_no_budget_anchor(self):
        for outcome in ("failed", "unknown"):
            decision = recommend_task_policy(_record(outcome=outcome, uncached_input_tokens=100))
            self.assertIsNone(decision.recommended_soft_budget)
            self.assertIn("no_budget_anchor_for_non_successful_or_unknown_outcome", decision.override_reasons)

    def test_negative_evidence_is_normalized_to_unknown_for_schema_safety(self):
        decision = recommend_task_policy(_record(uncached_input_tokens=-1), QualityEvidence(
            safety_or_live_change=False, broad_architecture_change=False,
            bounded_routine_fix=True, human_gate_required=False,
            independent_review_correct=True, required_context_recalled=True,
            context_growth_tokens=-2, stale_waste_tokens=-3,
        )).to_dict()
        self.assertIsNone(decision["recommended_soft_budget"]["uncached_input_tokens"])
        self.assertIsNone(decision["evidence"]["context_growth_tokens"])
        self.assertIsNone(decision["orchestration_waste"]["stale_tokens"])

    def test_repeated_unchanged_state_does_not_spend_an_extra_round(self):
        decision = recommend_task_policy(_record(), QualityEvidence(
            safety_or_live_change=True, independent_review_correct=True,
            required_context_recalled=True, new_evidence_or_progress=True,
            repeated_unchanged_state=True,
        ))
        self.assertEqual(decision.recommended_max_review_fix_rounds, 3)

    def test_redacted_organic_post_334_samples_produce_one_shadow_artifact_each(self):
        records = correlate_task_economics(read_attribution_jsonl(FIXTURE), [])
        report = shadow_policy_report(records)
        self.assertEqual(report["decision_count"], 2)
        self.assertEqual(report["decisions"][0]["recommended_soft_budget"]["cache_read_tokens"], 61009536)
        self.assertIsNone(report["decisions"][1]["recommended_soft_budget"]["reasoning_tokens"])
