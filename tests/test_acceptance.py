"""Acceptance tests for #80's read-only organic report checker."""

from __future__ import annotations

import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from quota_core.context_economics.acceptance import FAIL, INSUFFICIENT_DATA, PASS, check_acceptance
from quota_core.context_economics.acceptance import main

FIXTURE = Path(__file__).parent / "fixtures" / "economics_policy" / "acceptance-current-insufficient.json"
RISK = ("safety_or_live_change", "broad_architecture_change", "bounded_routine_fix", "human_gate_required")


def _row(index: int) -> dict[str, object]:
    decision = {"risk_tier": "routine" if index % 2 else "review_or_test", "quality_preserving": True, "human_gate_required": False, "recommended_session_treatment": "preserve", "recommended_cache_treatment": "preserve", "provenance": {"provider": "provider-a" if index % 2 else "provider-b"}, "evidence": {"required_context_recalled": True, "independent_review_correct": True, "token_observations": {"uncached_input_tokens": 1, "cache_write_tokens": None, "cache_read_tokens": 0, "output_tokens": 1, "reasoning_tokens": None}}, "component_costs": {"components": {}, "non_additive_components": ["reasoning"], "aggregation": "prohibited_overlapping_components"}}
    return {"task_id": f"task-{index:03}", "created_at": 1000 + index, "runtime": "runtime-a" if index % 2 else "runtime-b", "evidence_provenance": {"required_context_recalled": "observed_true", "recall_applicability": "observed_true", **{field: "producer_declared:explicit/high" for field in RISK}}, "risk_declaration": {"kind": "bounded_routine" if index % 2 else "non_production"}, "policy_decision": decision}


def _passing_report() -> dict[str, object]:
    rows = [_row(index) for index in range(50)]
    rows[0]["evidence_provenance"] = {"required_context_recalled": "observed_true", "recall_applicability": "observed_true", **{field: "not_recorded_by_producer" for field in RISK}}
    rows[0]["policy_decision"].update({"risk_tier": "safety_or_live", "human_gate_required": True, "recommended_session_treatment": "insufficient_evidence"})
    rows[1]["policy_decision"].update({"quality_preserving": False, "recommended_session_treatment": "insufficient_evidence", "recommended_cache_treatment": "insufficient_evidence"})
    rows[1]["policy_decision"]["evidence"]["independent_review_correct"] = False
    return {"mode": "shadow", "policy_contract": {"id": "policy-v1", "sha256": "a" * 64}, "decisions": {row["task_id"]: row for row in rows}}


class AcceptanceCheckTests(unittest.TestCase):
    def test_all_criteria_pass_for_complete_sample(self) -> None:
        report = _passing_report()
        # Real SQLite rows identify the loader uniformly; execution diversity
        # comes from the policy provider provenance, not that loader label.
        for row in report["decisions"].values():
            row["runtime"] = "task_attribution_sqlite"
        result = check_acceptance(report, since=1000, rerun_bytes_equal=True)
        self.assertEqual(result["overall_verdict"], "READY_TO_CLOSE")
        self.assertTrue(all(item["status"] == PASS for item in result["criteria"].values()))

    def test_each_observable_criterion_can_fail(self) -> None:
        def c1_missing(report):
            for row in list(report["decisions"].values())[10:]:
                row["evidence_provenance"]["required_context_recalled"] = "not_recorded_by_producer"
                row["evidence_provenance"]["recall_applicability"] = "applicable_but_missing"
                row["policy_decision"]["evidence"]["required_context_recalled"] = None

        def c2_missing(report):
            for row in list(report["decisions"].values())[10:]:
                row["evidence_provenance"]["bounded_routine_fix"] = "not_recorded_by_producer"
                row["policy_decision"].update({"risk_tier": "safety_or_live", "recommended_session_treatment": "insufficient_evidence"})

        def d1_single_tier(report):
            for row in list(report["decisions"].values())[1:]:
                row["policy_decision"]["risk_tier"] = "routine"

        mutations = {
            "C1": c1_missing,
            "C2": c2_missing,
            "D1": d1_single_tier,
            "D2": lambda r: [x["policy_decision"].update({"recommended_session_treatment": "insufficient_evidence", "recommended_cache_treatment": "insufficient_evidence"}) for x in r["decisions"].values()],
            "D3": lambda r: [x["policy_decision"].update({"human_gate_required": True}) for x in r["decisions"].values()],
            "V1": lambda r: list(r["decisions"].values())[0]["policy_decision"].update({"risk_tier": "routine"}),
            "V2": lambda r: list(r["decisions"].values())[1]["policy_decision"].update({"recommended_cache_treatment": "preserve"}),
            "V3": lambda r: list(r["decisions"].values())[2]["policy_decision"]["component_costs"].update({"total": 1}),
        }
        for criterion, mutate in mutations.items():
            with self.subTest(criterion=criterion):
                report = copy.deepcopy(_passing_report()); mutate(report)
                result = check_acceptance(report, since=1000, rerun_bytes_equal=True)
                self.assertEqual(result["criteria"][criterion]["status"], FAIL)
                expected = "DO_NOT_CLOSE" if criterion.startswith("V") else "NOT_YET"
                self.assertEqual(result["overall_verdict"], expected)

    def test_coverage_or_differentiation_failures_wait_but_safety_failures_stop(self) -> None:
        coverage = _passing_report()
        for row in coverage["decisions"].values():
            row["evidence_provenance"]["required_context_recalled"] = "not_recorded_by_producer"
            row["evidence_provenance"]["recall_applicability"] = "applicable_but_missing"
            row["policy_decision"]["evidence"]["required_context_recalled"] = None
        waiting = check_acceptance(coverage, since=1000, rerun_bytes_equal=True)
        self.assertEqual(waiting["criteria"]["C1"]["status"], FAIL)
        self.assertEqual(waiting["overall_verdict"], "NOT_YET")
        self.assertEqual(waiting["recommended_action"], "wait_for_coverage_or_differentiation_and_rerun")

        unsafe = _passing_report()
        list(unsafe["decisions"].values())[0]["policy_decision"]["risk_tier"] = "routine"
        stopped = check_acceptance(unsafe, since=1000, rerun_bytes_equal=True)
        self.assertEqual(stopped["overall_verdict"], "DO_NOT_CLOSE")
        self.assertEqual(stopped["safety_failure_criteria"], ["V1"])

    def test_current_unknown_shape_and_short_sample_are_not_yet(self) -> None:
        self.assertEqual(check_acceptance(_passing_report(), since=1001)["criteria"]["S1"]["status"], INSUFFICIENT_DATA)
        current = check_acceptance(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self.assertEqual(current["overall_verdict"], "NOT_YET")
        self.assertEqual(current["criteria"]["S1"]["status"], INSUFFICIENT_DATA)

    def test_auto_cutoff_ignores_unknown_applicability_and_legacy_rows_for_c2(self) -> None:
        legacy = [_row(index) for index in range(50)]
        trusted = [_row(index) for index in range(50, 100)]
        for row in legacy:
            row["evidence_provenance"] = {
                "required_context_recalled": "applicability_unknown",
                "recall_applicability": "applicability_unknown",
                **{field: "not_recorded_by_producer" for field in RISK},
            }
            row["policy_decision"].update({"risk_tier": "safety_or_live", "human_gate_required": True, "recommended_session_treatment": "insufficient_evidence"})
            row["policy_decision"]["evidence"]["required_context_recalled"] = None
        report = {"mode": "shadow", "policy_contract": {"id": "policy-v1", "sha256": "a" * 64}, "decisions": {row["task_id"]: row for row in (*legacy, *trusted)}}
        result = check_acceptance(report, rerun_bytes_equal=True)
        self.assertEqual(result["cutoff_created_at"], 1050)
        self.assertEqual(result["criteria"]["C2"]["status"], PASS)
        self.assertEqual(result["criteria"]["C2"]["coverage"]["bounded_routine_fix"], 1.0)

    def test_cli_prints_machine_readable_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.json"
            rerun = Path(temp) / "rerun.json"
            path.write_text(json.dumps(_passing_report()), encoding="utf-8")
            rerun.write_bytes(path.read_bytes())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--report", str(path), "--rerun-report", str(rerun), "--since", "1000"]), 0)
        self.assertEqual(json.loads(output.getvalue())["overall_verdict"], "READY_TO_CLOSE")

    def test_cli_rejects_report_as_its_own_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.json"
            path.write_text(json.dumps(_passing_report()), encoding="utf-8")
            with self.assertRaises(SystemExit) as error:
                main(["--report", str(path), "--rerun-report", str(path), "--since", "1000"])
        self.assertEqual(error.exception.code, 2)

    def test_uniform_declarations_are_d1_exception_and_are_reported(self) -> None:
        report = _passing_report()
        for row in report["decisions"].values():
            row["risk_declaration"] = {"kind": "bounded_routine"}
            row["policy_decision"]["risk_tier"] = "routine"
        result = check_acceptance(report, since=1000, rerun_bytes_equal=True)
        self.assertEqual(result["criteria"]["D1"]["status"], PASS)
        self.assertEqual(result["criteria"]["D1"]["declaration_distribution"], {"bounded_routine": 49})

    def test_untimestamped_safety_violation_is_not_dropped(self) -> None:
        report = _passing_report()
        violation = _row(99)
        violation["created_at"] = None
        violation["evidence_provenance"] = {field: "not_recorded_by_producer" for field in RISK}
        violation["policy_decision"].update({"risk_tier": "routine", "recommended_session_treatment": "preserve"})
        report["decisions"]["untimestamped-violation"] = violation
        result = check_acceptance(report, since=1000, rerun_bytes_equal=True)
        self.assertEqual(result["criteria"]["S1"]["untimestamped_artifact_count"], 1)
        self.assertEqual(result["criteria"]["V1"]["status"], FAIL)

    def test_v3_requires_explicit_byte_identical_rerun(self) -> None:
        report = _passing_report()
        self.assertEqual(check_acceptance(report, since=1000)["criteria"]["V3"]["status"], INSUFFICIENT_DATA)
        self.assertEqual(check_acceptance(report, since=1000, rerun_bytes_equal=False)["criteria"]["V3"]["status"], FAIL)

    def test_c1_uses_known_applicability_and_requires_thirty_rows(self) -> None:
        def with_recall_shape(known_count: int, recorded_count: int) -> dict[str, object]:
            report = _passing_report()
            for index, row in enumerate(report["decisions"].values()):
                state = "observed_true" if index < recorded_count else "applicable_but_missing" if index < known_count else "applicability_unknown"
                row["evidence_provenance"]["required_context_recalled"] = state
                row["evidence_provenance"]["recall_applicability"] = state
                row["policy_decision"]["evidence"]["required_context_recalled"] = True if state == "observed_true" else None
            return report

        insufficient = check_acceptance(with_recall_shape(29, 29), since=1000, rerun_bytes_equal=True)["criteria"]["C1"]
        passing = check_acceptance(with_recall_shape(30, 24), since=1000, rerun_bytes_equal=True)["criteria"]["C1"]
        failing = check_acceptance(with_recall_shape(30, 23), since=1000, rerun_bytes_equal=True)["criteria"]["C1"]
        self.assertEqual((insufficient["status"], insufficient["total_count"]), (INSUFFICIENT_DATA, 29))
        self.assertEqual(insufficient["denominator_basis"], "known_recall_applicability_rows")
        self.assertEqual(insufficient["excluded_unknown_applicability_count"], 21)
        self.assertEqual((passing["status"], passing["total_count"]), (PASS, 30))
        self.assertEqual((failing["status"], failing["total_count"]), (FAIL, 30))

    def test_overall_verdict_defers_recall_evidence_but_never_safety_failures(self) -> None:
        def deferred_recall_report() -> dict[str, object]:
            report = _passing_report()
            for row in report["decisions"].values():
                row["evidence_provenance"]["required_context_recalled"] = "applicability_unknown"
                row["evidence_provenance"]["recall_applicability"] = "applicability_unknown"
                row["policy_decision"]["evidence"]["required_context_recalled"] = None
            return report

        ready = check_acceptance(deferred_recall_report(), since=1000, rerun_bytes_equal=True)
        self.assertEqual(ready["overall_verdict"], "READY_TO_CLOSE")
        self.assertEqual(ready["recall_axis"], "evidence_only_deferred")
        self.assertEqual(ready["criteria"]["C1"]["status"], INSUFFICIENT_DATA)
        self.assertEqual(ready["criteria"]["C1"]["total_count"], 0)

        waiting_report = deferred_recall_report()
        for index, row in enumerate(waiting_report["decisions"].values()):
            if index < 30:
                state = "observed_true" if index < 23 else "applicable_but_missing"
                row["evidence_provenance"]["required_context_recalled"] = state
                row["evidence_provenance"]["recall_applicability"] = state
                row["policy_decision"]["evidence"]["required_context_recalled"] = True if state == "observed_true" else None
        waiting = check_acceptance(waiting_report, since=1000, rerun_bytes_equal=True)
        self.assertEqual(waiting["criteria"]["C1"]["status"], FAIL)
        self.assertEqual(waiting["overall_verdict"], "NOT_YET")

        unsafe_report = deferred_recall_report()
        list(unsafe_report["decisions"].values())[0]["policy_decision"].update({"risk_tier": "routine", "recommended_session_treatment": "preserve"})
        unsafe = check_acceptance(unsafe_report, since=1000, rerun_bytes_equal=True)
        self.assertEqual(unsafe["overall_verdict"], "DO_NOT_CLOSE")
