"""Contract tests for quota-core#80's recommendation-only policy engine."""

from __future__ import annotations

import json
import hashlib
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
    def test_quality_evidence_state_distinguishes_unknown_from_observed_negative(self):
        known_risk = dict(
            safety_or_live_change=False, broad_architecture_change=False,
            bounded_routine_fix=True, human_gate_required=False,
            independent_review_correct=True,
        )
        cases = (
            ("unknown_recall", QualityEvidence(**known_risk), "unknown", False,
             "required_context_recall_unknown", "required_context_recall_negative"),
            ("negative_recall", QualityEvidence(**known_risk, required_context_recalled=False),
             "not_preserving", False, "required_context_recall_negative", "required_context_recall_unknown"),
            ("positive_recall", QualityEvidence(**known_risk, required_context_recalled=True),
             "preserving", True, None, None),
            ("failed_review_unknown_recall", QualityEvidence(**{**known_risk, "independent_review_correct": False}),
             "not_preserving", False, "required_context_recall_unknown", "required_context_recall_negative"),
            ("blocked_exemption", QualityEvidence(independent_review_correct=True, recall_not_applicable=True),
             "unknown", False, None, None),
        )
        for name, evidence, expected_state, expected_quality, specific, forbidden in cases:
            with self.subTest(name=name):
                decision = recommend_task_policy(_record(), evidence)
                self.assertEqual(decision.quality_preserving, expected_quality)
                self.assertEqual(decision.quality_evidence_state, expected_state)
                self.assertEqual(decision.to_dict()["quality_evidence_state"], expected_state)
                if specific:
                    self.assertIn("required_context_recall_unknown_or_negative", decision.override_reasons)
                    self.assertIn(specific, decision.override_reasons)
                if forbidden:
                    self.assertNotIn(forbidden, decision.override_reasons)
        failed_outcome = recommend_task_policy(
            _record(outcome="failed"), QualityEvidence(**known_risk, required_context_recalled=True)
        )
        self.assertEqual(failed_outcome.quality_evidence_state, "not_preserving")

    def test_additive_quality_state_preserves_legacy_fixture_decisions(self):
        known_risk = dict(safety_or_live_change=False, broad_architecture_change=False,
                          bounded_routine_fix=True, human_gate_required=False,
                          independent_review_correct=True)
        cases = {
            "unknown_recall": (recommend_task_policy(_record(), QualityEvidence(**known_risk)),
                               "09b568f76c6331e0adb44e899cb12eefaba2f310ff5e70631e64728d1eded796"),
            "negative_recall": (recommend_task_policy(_record(), QualityEvidence(**known_risk, required_context_recalled=False)),
                                "8802ee5847c9baaa895fabc7e8b7242d1aa94ab12374f8dc247e87365267552d"),
            "positive_recall": (recommend_task_policy(_record(), QualityEvidence(**known_risk, required_context_recalled=True)),
                                "cc9c4495c44dc9397b499d5099ec016a024cb71f02bf9d3084d36190bad4260e"),
            "failed_review_unknown_recall": (recommend_task_policy(_record(), QualityEvidence(**{**known_risk, "independent_review_correct": False})),
                                             "bc9bdc6cf9a82fba4705261801052ba1b524eff61a1a5bb43760726a43927fb0"),
            "blocked_exemption": (recommend_task_policy(_record(), QualityEvidence(independent_review_correct=True, recall_not_applicable=True)),
                                  "16232f59d3054604a67f0d73b0a5e62a3e79626a920426e0caf8b29ae9a2a390"),
        }
        fixture_hashes = {
            "organic-implement-redacted": "640c62f4b0a9df00ab40cf1d014d38f7b095454760e8a4d93b3cdab710561e57",
            "organic-review-redacted": "58ed229fcc0a57da0668d5fc816de484262f78eab2539bfd707d3c2d0e85c223",
        }
        for record in correlate_task_economics(read_attribution_jsonl(FIXTURE), []):
            cases[record.task_id] = (recommend_task_policy(record), fixture_hashes[record.task_id])
        legacy_keys = set(policy_contract_schema()["required"])
        for name, (decision, expected_digest) in cases.items():
            with self.subTest(name=name):
                actual = decision.to_dict()
                self.assertEqual(set(actual) - {"quality_evidence_state"}, legacy_keys)
                legacy = {key: value for key, value in actual.items() if key != "quality_evidence_state"}
                legacy["override_reasons"] = [value for value in legacy["override_reasons"]
                                              if value not in {"required_context_recall_unknown", "required_context_recall_negative"}]
                encoded = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
                self.assertEqual(hashlib.sha256(encoded).hexdigest(), expected_digest)

    def test_quality_evidence_state_is_optional_in_schema_for_old_decisions(self):
        schema = policy_contract_schema()
        self.assertEqual(schema["properties"]["quality_evidence_state"]["enum"],
                         ["preserving", "not_preserving", "unknown"])
        self.assertNotIn("quality_evidence_state", schema["required"])

    def test_unknown_risk_with_renewable_waste_cannot_renew_session(self):
        from quota_core.context_economics.acceptance import PASS, check_acceptance

        record = _record(cache_read_tokens=10)
        decision = recommend_task_policy(record, QualityEvidence(
            independent_review_correct=True, required_context_recalled=True,
            stale_waste_tokens=5000,
        ))
        self.assertEqual(decision.orchestration_waste["stale_tokens"], 5000)
        self.assertTrue(decision.quality_preserving)
        self.assertEqual(decision.recommended_session_treatment, "insufficient_evidence")
        self.assertEqual(decision.recommended_cache_treatment, "preserve")

        risk_fields = (
            "safety_or_live_change", "broad_architecture_change",
            "bounded_routine_fix", "human_gate_required",
        )
        report = {"mode": "shadow", "decisions": {record.task_id: {
            "created_at": 100.25,
            "risk_facts": {field: None for field in risk_fields},
            "evidence_provenance": {field: "not_recorded_by_producer" for field in risk_fields},
            "policy_decision": decision.to_dict(),
        }}}
        self.assertEqual(
            check_acceptance(report, since=100, rerun_bytes_equal=True)["criteria"]["V1"]["status"],
            PASS,
        )

    def test_unknown_risk_with_observed_review_and_recall_keeps_session_fail_safe(self):
        from quota_core.context_economics.acceptance import PASS, check_acceptance

        record = _record(cache_read_tokens=10)
        decision = recommend_task_policy(record, QualityEvidence(
            independent_review_correct=True, required_context_recalled=True,
        ))
        self.assertTrue(decision.quality_preserving)
        self.assertEqual(decision.risk_tier, "safety_or_live")
        self.assertTrue(decision.human_gate_required)
        self.assertEqual(decision.recommended_session_treatment, "insufficient_evidence")
        # Observed cache productivity remains independent of risk classification.
        self.assertEqual(decision.recommended_cache_treatment, "preserve")
        self.assertEqual(decision.recommended_max_review_fix_rounds, 3)
        self.assertIsNotNone(decision.recommended_soft_budget)

        report = {"mode": "shadow", "decisions": {record.task_id: {
            "task_id": record.task_id, "created_at": 100.25,
            "risk_facts": {field: None for field in (
                "safety_or_live_change", "broad_architecture_change",
                "bounded_routine_fix", "human_gate_required",
            )},
            "evidence_provenance": {field: "not_recorded_by_producer" for field in (
                "safety_or_live_change", "broad_architecture_change",
                "bounded_routine_fix", "human_gate_required",
            )},
            "policy_decision": decision.to_dict(),
        }}}
        self.assertEqual(check_acceptance(report, since=100, rerun_bytes_equal=True)["criteria"]["V1"]["status"], PASS)

    def test_zero_or_negative_cache_read_is_not_a_hit(self):
        from dataclasses import replace
        from quota_core.context_economics.schema import TaskTokenTelemetry
        evidence = QualityEvidence(
            safety_or_live_change=False, broad_architecture_change=False,
            bounded_routine_fix=True, human_gate_required=False,
            independent_review_correct=True, recall_not_applicable=True,
        )
        for amount in (0, -1):
            decision = recommend_task_policy(replace(
                _record(), task_telemetry=TaskTokenTelemetry(cache_read_tokens=amount)), evidence)
            self.assertEqual(decision.recommended_cache_treatment, "no_cache_signal")

    def test_quality_veto_blocks_cost_cutting_and_keeps_unknowns_unknown(self):
        decision = recommend_task_policy(_record(uncached_input_tokens=0, cache_read_tokens=None))
        self.assertFalse(decision.quality_preserving)
        self.assertEqual(decision.recommended_provider_tier, "escalate_allowed")
        self.assertTrue(decision.human_gate_required)
        self.assertEqual(decision.recommended_cache_treatment, "insufficient_evidence")
        self.assertEqual(decision.recommended_soft_budget.uncached_input_tokens, 0)
        self.assertIsNone(decision.recommended_soft_budget.cache_read_tokens)
        self.assertIn("independent_review_correctness_unknown_or_negative", decision.override_reasons)

    def test_no_retrieval_recall_exemption_keeps_review_quality_floor(self):
        decision = recommend_task_policy(_record(), QualityEvidence(
            safety_or_live_change=False, broad_architecture_change=False,
            bounded_routine_fix=True, human_gate_required=False,
            independent_review_correct=True, recall_not_applicable=True,
        ))
        self.assertTrue(decision.quality_preserving)
        self.assertEqual(decision.recommended_session_treatment, "preserve")

    def test_no_retrieval_exemption_cannot_bypass_unknown_risk_fail_safe(self):
        decision = recommend_task_policy(_record(), QualityEvidence(
            independent_review_correct=True, recall_not_applicable=True,
        ))
        self.assertFalse(decision.quality_preserving)
        self.assertEqual(decision.recommended_session_treatment, "insufficient_evidence")

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
