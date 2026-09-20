"""quota-core#80 final slice: derived quality evidence and priced components.

PR #82's shadow report over live databases produced artifacts that were all
`safety_or_live` with `insufficient_evidence`, because nothing supplied
QualityEvidence and nothing supplied rates. These tests pin the two halves of
the fix and, just as importantly, pin what is still honestly unknown.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quota_core.context_economics import (
    NOT_RECORDED,
    OPERATOR_DECLARED,
    REVIEW_TASK_TYPES,
    PRICED_COMPONENTS,
    PricingBook,
    QualityEvidence,
    TaskEvidence,
    TaskTypeRiskDeclaration,
    apply_risk_declaration,
    attribution_from_dict,
    correlate_task_economics,
    derive_quality_evidence,
    evidence_map,
    price_task,
    read_review_verdicts,
    recommend_task_policy,
    shadow_comparison_report,
)

PRICE_BOOK = Path(__file__).parent / "fixtures" / "economics_policy" / "price_book.json"


def _record(**fields):
    row = {"runtime": "portable", "task_id": "t1", "outcome": "completed",
           "task_type": "implement", "agent": "provider-a"}
    row.update(fields)
    return correlate_task_economics([attribution_from_dict(row)], [])[0]


def _results_db(rows, *, columns="task_id TEXT, task_type TEXT, context TEXT, verdict TEXT, created_at REAL"):
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE tasks({columns})")
    conn.executemany(
        f"INSERT INTO tasks VALUES({','.join('?' * len(rows[0]))})", rows
    )
    conn.commit()
    conn.close()
    return path


def _review(task_id, target, verdict, created_at):
    return (task_id, "review", json.dumps({"prev_task_id": target}), verdict, created_at)


class ReviewVerdictDerivationTests(unittest.TestCase):
    def test_verdict_maps_to_review_correctness(self):
        db = _results_db([
            _review("r1", "impl-a", "approve", 1.0),
            _review("r2", "impl-b", "request_changes", 2.0),
        ])
        derived = derive_quality_evidence(db, ["impl-a", "impl-b"])
        self.assertIs(derived["impl-a"].evidence.independent_review_correct, True)
        self.assertIs(derived["impl-b"].evidence.independent_review_correct, False)
        self.assertIn("approve", derived["impl-a"].provenance["independent_review_correct"])

    def test_no_linked_review_or_no_verdict_stays_unknown_not_false(self):
        """⛔'nobody reviewed it' and 'the reviewer said no' are different facts."""
        db = _results_db([
            _review("r1", "impl-b", None, 1.0),
            ("r2", "review", json.dumps({}), "approve", 2.0),
        ])
        derived = derive_quality_evidence(db, ["impl-a", "impl-b"])
        self.assertIsNone(derived["impl-a"].evidence.independent_review_correct)
        self.assertIsNone(derived["impl-b"].evidence.independent_review_correct)
        self.assertEqual(
            derived["impl-a"].provenance["independent_review_correct"], "no_linked_review_task")

    def test_latest_review_wins_on_a_fix_loop_and_is_deterministic(self):
        rows = [
            _review("r1", "impl-a", "request_changes", 1.0),
            _review("r2", "impl-a", "request_changes", 2.0),
            _review("r3", "impl-a", "approve", 3.0),
        ]
        db = _results_db(rows)
        first = derive_quality_evidence(db, ["impl-a"])
        self.assertIs(first["impl-a"].evidence.independent_review_correct, True)
        self.assertIs(
            derive_quality_evidence(db, ["impl-a"])["impl-a"].evidence.independent_review_correct,
            True)

    def test_unrecognized_verdict_is_unknown_never_guessed(self):
        db = _results_db([_review("r1", "impl-a", "teleported", 1.0)])
        derived = derive_quality_evidence(db, ["impl-a"])
        self.assertIsNone(derived["impl-a"].evidence.independent_review_correct)

    def test_a_self_linked_review_cannot_supply_its_own_quality_evidence(self):
        db = _results_db([_review("review-a", "review-a", "approve", 1.0)])
        self.assertEqual(read_review_verdicts(db), {})
        derived = derive_quality_evidence(db, ["review-a"])
        self.assertIsNone(derived["review-a"].evidence.independent_review_correct)

    def test_a_verdict_on_a_non_review_row_is_ignored(self):
        """⛔Review of PR #84, P1: carrying a verdict and a linked task does not
        make a row a review. On real local data 12 `test` rows carry both, and
        their verdicts were being read as review correctness for the tasks they
        pointed at -- fabricating the one piece of evidence this module derives."""
        db = _results_db([
            ("t1", "test", json.dumps({"prev_task_id": "impl-a"}), "approve", 1.0),
            ("t2", "implement", json.dumps({"prev_task_id": "impl-b"}), "approve", 2.0),
            ("t3", "discuss", json.dumps({"prev_task_id": "impl-c"}), "request_changes", 3.0),
        ])
        self.assertEqual(read_review_verdicts(db), {})
        derived = derive_quality_evidence(db, ["impl-a", "impl-b", "impl-c"])
        for task_id in ("impl-a", "impl-b", "impl-c"):
            self.assertIsNone(
                derived[task_id].evidence.independent_review_correct, task_id)
            self.assertEqual(
                derived[task_id].provenance["independent_review_correct"],
                "no_linked_review_task", task_id)

    def test_a_review_row_beside_a_non_review_row_still_counts(self):
        """The restriction must exclude the impostor, not the real reviewer."""
        db = _results_db([
            ("t1", "test", json.dumps({"prev_task_id": "impl-a"}), "request_changes", 1.0),
            ("r1", "review", json.dumps({"prev_task_id": "impl-a"}), "approve", 2.0),
        ])
        derived = derive_quality_evidence(db, ["impl-a"])
        self.assertIs(derived["impl-a"].evidence.independent_review_correct, True)
        self.assertIn("r1", derived["impl-a"].provenance["independent_review_correct"])

    def test_a_caller_may_widen_the_accepted_review_types_explicitly(self):
        """Counting a tester's verdict must be a visible decision, not a default."""
        db = _results_db([
            ("t1", "test", json.dumps({"prev_task_id": "impl-a"}), "approve", 1.0),
        ])
        self.assertEqual(read_review_verdicts(db), {})
        widened = read_review_verdicts(db, "tasks", frozenset({"review", "test"}))
        self.assertEqual(widened["impl-a"][0], "approve")
        self.assertNotIn("test", REVIEW_TASK_TYPES)

    def test_a_table_without_task_type_cannot_confirm_a_review_and_yields_nothing(self):
        """⛔"cannot confirm" must not become "assume yes" for gating evidence."""
        db = _results_db(
            [("r1", json.dumps({"prev_task_id": "impl-a"}), "approve", 1.0)],
            columns="task_id TEXT, context TEXT, verdict TEXT, created_at REAL")
        self.assertEqual(read_review_verdicts(db), {})

    def test_unreadable_or_incompatible_database_yields_nothing(self):
        self.assertEqual(read_review_verdicts("/nonexistent/x.db"), {})
        self.assertEqual(read_review_verdicts(_results_db(
            [("r1", "review", "{}", "approve", 1.0)], columns="task_id TEXT, task_type TEXT, context TEXT, verdict TEXT, created_at REAL")), {})
        db = _results_db([("t", "review", "{}", "approve", 1.0)])
        self.assertEqual(read_review_verdicts(db, "not a table"), {})

    def test_unrecorded_fields_are_reported_as_unrecorded_not_false(self):
        """The honest core: recall and all four risk facts are not recorded."""
        db = _results_db([_review("r1", "impl-a", "approve", 1.0)])
        item = derive_quality_evidence(db, ["impl-a"])["impl-a"]
        self.assertIsNone(item.evidence.required_context_recalled)
        for name in ("required_context_recalled", "safety_or_live_change",
                     "broad_architecture_change", "bounded_routine_fix",
                     "human_gate_required"):
            self.assertEqual(item.provenance[name], NOT_RECORDED, name)
            self.assertIsNone(getattr(item.evidence, name), name)


class RiskDeclarationTests(unittest.TestCase):
    def test_without_a_declaration_risk_stays_unknown_and_fails_safe(self):
        db = _results_db([_review("r1", "impl-a", "approve", 1.0)])
        derived = derive_quality_evidence(db, ["impl-a"])
        decision = recommend_task_policy(
            _record(task_id="impl-a"), derived["impl-a"].evidence)
        self.assertEqual(decision.risk_tier, "safety_or_live")
        self.assertTrue(decision.human_gate_required)

    def test_declaration_is_opt_in_and_recorded_as_declared_not_measured(self):
        db = _results_db([_review("r1", "rev-a", "approve", 1.0)])
        derived = derive_quality_evidence(db, ["rev-a"])
        updated = apply_risk_declaration(
            derived, {"rev-a": "review"},
            TaskTypeRiskDeclaration(non_production_types=frozenset({"review"})))
        decision = recommend_task_policy(
            _record(task_id="rev-a", task_type="review"), updated["rev-a"].evidence)
        self.assertEqual(decision.risk_tier, "review_or_test")
        self.assertFalse(decision.human_gate_required)
        self.assertTrue(
            updated["rev-a"].provenance["safety_or_live_change"].startswith(OPERATOR_DECLARED))

    def test_an_undeclared_task_type_keeps_the_fail_safe(self):
        db = _results_db([_review("r1", "impl-a", "approve", 1.0)])
        derived = derive_quality_evidence(db, ["impl-a"])
        updated = apply_risk_declaration(
            derived, {"impl-a": "implement"},
            TaskTypeRiskDeclaration(non_production_types=frozenset({"review"})))
        self.assertIsNone(updated["impl-a"].evidence.safety_or_live_change)
        self.assertEqual(updated["impl-a"].provenance["safety_or_live_change"], NOT_RECORDED)

    def test_a_declaration_never_overwrites_measured_review_evidence(self):
        db = _results_db([_review("r1", "rev-a", "request_changes", 1.0)])
        derived = derive_quality_evidence(db, ["rev-a"])
        updated = apply_risk_declaration(
            derived, {"rev-a": "review"},
            TaskTypeRiskDeclaration(non_production_types=frozenset({"review"})))
        self.assertIs(updated["rev-a"].evidence.independent_review_correct, False)
        self.assertIn("request_changes",
                      updated["rev-a"].provenance["independent_review_correct"])

    def test_safety_declaration_has_precedence_over_overlapping_categories(self):
        declaration = TaskTypeRiskDeclaration(
            safety_or_live_types=frozenset({"deploy"}),
            architecture_types=frozenset({"deploy"}),
            routine_types=frozenset({"deploy"}),
            non_production_types=frozenset({"deploy"}),
        )
        evidence = declaration.declare("deploy")
        self.assertTrue(evidence.safety_or_live_change)
        self.assertFalse(evidence.broad_architecture_change)
        self.assertFalse(evidence.bounded_routine_fix)
        self.assertEqual(
            recommend_task_policy(_record(task_type="deploy"), evidence).risk_tier,
            "safety_or_live",
        )

    def test_single_architecture_declaration_does_not_fall_back_to_unknown_risk(self):
        evidence = TaskTypeRiskDeclaration(
            architecture_types=frozenset({"architecture"}),
        ).declare("architecture")
        self.assertEqual(
            recommend_task_policy(_record(task_type="architecture"), evidence).risk_tier,
            "architecture",
        )

    def test_human_gate_is_retained_with_routine_or_architecture_tiers(self):
        for task_type, declaration in (
            ("routine", TaskTypeRiskDeclaration(
                routine_types=frozenset({"routine"}),
                human_gate_types=frozenset({"routine"}),
            )),
            ("architecture", TaskTypeRiskDeclaration(
                architecture_types=frozenset({"architecture"}),
                human_gate_types=frozenset({"architecture"}),
            )),
        ):
            evidence = declaration.declare(task_type)
            decision = recommend_task_policy(_record(task_type=task_type), evidence)
            self.assertTrue(evidence.human_gate_required, task_type)
            self.assertTrue(decision.human_gate_required, task_type)
            self.assertEqual(decision.risk_tier, "safety_or_live", task_type)
            self.assertEqual(decision.recommended_max_review_fix_rounds, 3, task_type)

    def test_risk_declaration_preserves_all_non_risk_policy_evidence(self):
        original = QualityEvidence(
            independent_review_correct=True,
            required_context_recalled=True,
            new_evidence_or_progress=True,
            repeated_unchanged_state=False,
            context_growth_tokens=7,
            stale_waste_tokens=11,
            misrouted_waste_tokens=13,
            duplicate_waste_tokens=17,
        )
        derived = {"review-a": TaskEvidence("review-a", original, {})}
        updated = apply_risk_declaration(
            derived, {"review-a": "review"},
            TaskTypeRiskDeclaration(non_production_types=frozenset({"review"})),
        )
        evidence = updated["review-a"].evidence
        self.assertTrue(evidence.independent_review_correct)
        self.assertTrue(evidence.required_context_recalled)
        self.assertTrue(evidence.new_evidence_or_progress)
        self.assertFalse(evidence.repeated_unchanged_state)
        self.assertEqual(evidence.context_growth_tokens, 7)
        self.assertEqual(evidence.stale_waste_tokens, 11)
        self.assertEqual(evidence.misrouted_waste_tokens, 13)
        self.assertEqual(evidence.duplicate_waste_tokens, 17)
        decision = recommend_task_policy(
            _record(task_id="review-a", task_type="review"), evidence)
        self.assertEqual(decision.recommended_max_review_fix_rounds, 2)
        self.assertEqual(decision.recommended_session_treatment, "renew")


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.book = PricingBook.from_mapping(json.loads(PRICE_BOOK.read_text()))

    def test_exact_model_version_beats_the_provider_default(self):
        exact = price_task(_record(model="model-x-2", uncached_input_tokens=100), self.book)
        fallback = price_task(_record(model="model-other", uncached_input_tokens=100), self.book)
        self.assertAlmostEqual(exact.components["uncached_input"], 5.0)
        self.assertAlmostEqual(fallback.components["uncached_input"], 10.0)

    def test_unpriced_component_and_unknown_tokens_stay_none_never_zero(self):
        priced = price_task(
            _record(agent="provider-b", uncached_input_tokens=100, cache_read_tokens=50), self.book)
        self.assertAlmostEqual(priced.components["uncached_input"], 2.0)
        self.assertIsNone(priced.components["cache_read"], "provider-b prices no cache read")
        self.assertIsNone(priced.components["reasoning"], "token count unknown")

    def test_a_measured_zero_costs_zero_and_is_not_unknown(self):
        priced = price_task(_record(cache_write_tokens=0), self.book)
        self.assertEqual(priced.components["cache_write"], 0.0)

    def test_an_unknown_provider_is_never_costed_at_another_providers_rates(self):
        priced = price_task(_record(agent="provider-z", uncached_input_tokens=100), self.book)
        self.assertFalse(priced.rates_found)
        self.assertFalse(priced.priced)
        self.assertTrue(all(value is None for value in priced.components.values()))

    def test_rates_without_measurements_are_not_reported_as_priced(self):
        """⛔Review of PR #84, P1: an all-unknown task advertised priced=True
        merely because a rate entry existed, so unknown wore the shape of a
        value. `rates_found` and `priced` are now separate facts."""
        payload = price_task(_record(), self.book).to_dict()
        self.assertTrue(payload["rates_found"], "a provider-a entry does exist")
        self.assertFalse(payload["priced"], "but nothing was actually costed")
        self.assertTrue(all(v is None for v in payload["productive"].values()))

    def test_a_measured_zero_still_counts_as_priced(self):
        """0 tokens at a known rate is a real cost of 0.0, not an unknown."""
        payload = price_task(_record(cache_write_tokens=0), self.book).to_dict()
        self.assertTrue(payload["priced"])
        self.assertEqual(payload["productive"]["cache_write"], 0.0)

    def test_no_rate_entry_is_distinguishable_from_no_measurements(self):
        no_rates = price_task(
            _record(agent="provider-z", uncached_input_tokens=100), self.book).to_dict()
        no_tokens = price_task(_record(), self.book).to_dict()
        self.assertEqual((no_rates["rates_found"], no_rates["priced"]), (False, False))
        self.assertEqual((no_tokens["rates_found"], no_tokens["priced"]), (True, False))

    def test_no_universal_total_anywhere(self):
        payload = price_task(_record(uncached_input_tokens=1, output_tokens=1), self.book).to_dict()
        self.assertNotIn("total", json.dumps(payload))
        self.assertEqual(payload["non_additive_components"], ["reasoning"])
        self.assertEqual(set(payload["productive"]), set(PRICED_COMPONENTS))

    def test_retry_and_failover_cost_is_reported_on_its_own_side(self):
        productive = price_task(_record(uncached_input_tokens=100), self.book).to_dict()
        retried = price_task(
            _record(retry_of="t0", uncached_input_tokens=100), self.book).to_dict()
        failed_over = price_task(
            _record(fallback_of="t0", uncached_input_tokens=100), self.book).to_dict()
        self.assertEqual(productive["cost_attribution"], "productive")
        self.assertAlmostEqual(productive["productive"]["uncached_input"], 10.0)
        self.assertIsNone(productive["retry_or_failover"]["uncached_input"])
        for payload, label in ((retried, "retry_of"), (failed_over, "fallback_of")):
            self.assertEqual(payload["cost_attribution"], "retry_or_failover")
            self.assertAlmostEqual(payload["retry_or_failover"]["uncached_input"], 10.0)
            self.assertIsNone(payload["productive"]["uncached_input"])
            self.assertIn(label, payload["lineage"])

    def test_malformed_rates_are_skipped_rather_than_coerced(self):
        book = PricingBook.from_mapping({
            "provider-a": {"default": {"uncached_input": True, "output": -1, "cache_read": "free"}},
            "bad": "not-a-mapping",
        })
        self.assertEqual(book.entries, ())
        self.assertFalse(price_task(_record(uncached_input_tokens=10), book).rates_found)
        self.assertFalse(price_task(_record(uncached_input_tokens=10), book).priced)


class BaselineVersusRecommendedTests(unittest.TestCase):
    def test_report_compares_each_component_and_never_totals_them(self):
        report = shadow_comparison_report(
            [_record(uncached_input_tokens=100, cache_read_tokens=None)],
            {"t1": QualityEvidence(
                safety_or_live_change=False, broad_architecture_change=False,
                bounded_routine_fix=True, human_gate_required=False,
                independent_review_correct=True, required_context_recalled=True)},
        )
        row = report["artifacts"][0]["baseline_vs_recommended"]
        self.assertEqual(row["uncached_input_tokens"]["observed"], 100)
        self.assertEqual(row["uncached_input_tokens"]["recommended"], 120)
        self.assertEqual(row["uncached_input_tokens"]["headroom"], 20)
        # An unknown observation cannot produce a difference.
        self.assertIsNone(row["cache_read_tokens"]["observed"])
        self.assertIsNone(row["cache_read_tokens"]["headroom"])
        self.assertNotIn("total", json.dumps(row))

    def test_a_task_with_no_budget_anchor_reports_observed_without_headroom(self):
        report = shadow_comparison_report([_record(outcome="failed", uncached_input_tokens=80)])
        row = report["artifacts"][0]["baseline_vs_recommended"]
        self.assertEqual(row["uncached_input_tokens"]["observed"], 80)
        self.assertIsNone(row["uncached_input_tokens"]["recommended"])
        self.assertIsNone(row["uncached_input_tokens"]["headroom"])

    def test_report_carries_cost_and_evidence_provenance(self):
        book = PricingBook.from_mapping(json.loads(PRICE_BOOK.read_text()))
        report = shadow_comparison_report(
            [_record(uncached_input_tokens=100)], None, book,
            {"t1": {"required_context_recalled": NOT_RECORDED}})
        artifact = report["artifacts"][0]
        self.assertAlmostEqual(artifact["cost"]["productive"]["uncached_input"], 10.0)
        self.assertEqual(
            artifact["evidence_provenance"]["required_context_recalled"], NOT_RECORDED)

    def test_report_is_still_shadow_only_and_deterministic(self):
        records = [_record(task_id="b"), _record(task_id="a")]
        first = shadow_comparison_report(records)
        self.assertEqual(json.dumps(first), json.dumps(shadow_comparison_report(records)))
        self.assertEqual(first["mode"], "shadow")


class DerivedEvidenceChangesTheReportTests(unittest.TestCase):
    def test_derived_verdicts_differentiate_artifacts_that_were_uniform(self):
        """The regression this slice exists for: without evidence every task
        looked identical, which is what PR #82 saw over 447 live artifacts."""
        db = _results_db([
            _review("r1", "impl-a", "approve", 1.0),
            _review("r2", "impl-b", "request_changes", 2.0),
        ])
        records = [_record(task_id="impl-a"), _record(task_id="impl-b"), _record(task_id="impl-c")]
        derived = derive_quality_evidence(db, [r.task_id for r in records])
        before = shadow_comparison_report(records)
        after = shadow_comparison_report(
            records, evidence_map(derived), None,
            {k: v.provenance for k, v in derived.items()})
        self.assertEqual(
            {a["policy_decision"]["evidence"]["independent_review_correct"]
             for a in before["artifacts"]}, {None})
        self.assertEqual(
            [a["policy_decision"]["evidence"]["independent_review_correct"]
             for a in after["artifacts"]], [True, False, None])


if __name__ == "__main__":
    unittest.main()
