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
    return {"task_id": f"task-{index:03}", "created_at": 1000 + index, "runtime": "runtime-a" if index % 2 else "runtime-b", "evidence_provenance": {"required_context_recalled": "recorded_by_producer", **{field: "recorded_by_producer" for field in RISK}}, "risk_declaration": {"kind": "bounded_routine" if index % 2 else "non_production"}, "policy_decision": decision}


def _passing_report() -> dict[str, object]:
    rows = [_row(index) for index in range(50)]
    rows[0]["evidence_provenance"] = {"required_context_recalled": "recorded_by_producer", **{field: "not_recorded_by_producer" for field in RISK}}
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
        mutations = {
            "C1": lambda r: [x["evidence_provenance"].update({"required_context_recalled": "not_recorded_by_producer"}) for x in list(r["decisions"].values())[10:]],
            "C2": lambda r: [x["evidence_provenance"].update({"bounded_routine_fix": "not_recorded_by_producer"}) for x in list(r["decisions"].values())[10:]],
            "D1": lambda r: [x["policy_decision"].update({"risk_tier": "routine"}) for x in r["decisions"].values()],
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
                self.assertEqual(result["overall_verdict"], "DO_NOT_CLOSE")

    def test_current_unknown_shape_and_short_sample_are_not_yet(self) -> None:
        self.assertEqual(check_acceptance(_passing_report(), since=1001)["criteria"]["S1"]["status"], INSUFFICIENT_DATA)
        current = check_acceptance(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self.assertEqual(current["overall_verdict"], "NOT_YET")
        self.assertEqual(current["criteria"]["S1"]["status"], INSUFFICIENT_DATA)

    def test_auto_cutoff_uses_first_recorded_provenance(self) -> None:
        report = _passing_report()
        report["decisions"]["task-000"]["evidence_provenance"] = {field: "not_recorded_by_producer" for field in (*RISK, "required_context_recalled")}
        self.assertEqual(check_acceptance(report)["cutoff_created_at"], 1001)

    def test_cli_prints_machine_readable_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.json"
            path.write_text(json.dumps(_passing_report()), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--report", str(path), "--rerun-report", str(path), "--since", "1000"]), 0)
        self.assertEqual(json.loads(output.getvalue())["overall_verdict"], "READY_TO_CLOSE")

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
