"""Coverage for the incremental, read-only organic shadow-report producer."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quota_core.context_economics import (
    COVERED,
    NOT_COVERED,
    decision_for,
    policy_contract_sha256,
    produce_shadow_report,
    report_contract_schema,
)
from quota_core.context_economics.acceptance import PASS, check_acceptance
from quota_core.context_economics.report_producer import main


FIXTURE = Path(__file__).parent / "fixtures" / "economics_policy" / "organic-report-rows.json"


def _db(path: Path, rows: list[dict[str, object]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE task_attribution (
        task_id TEXT PRIMARY KEY, created_at REAL, outcome TEXT,
        provider TEXT, model TEXT, uncached_input_tokens INTEGER,
        cache_read_tokens INTEGER, cache_write_tokens INTEGER,
        output_tokens INTEGER, reasoning_tokens INTEGER
    )""")
    for row in rows:
        conn.execute(
            "INSERT INTO task_attribution VALUES (:task_id, :created_at, :outcome, "
            ":provider, :model, :uncached_input_tokens, :cache_read_tokens, "
            ":cache_write_tokens, :output_tokens, :reasoning_tokens)", row,
        )
    conn.commit()
    conn.close()


def _risk_db(path: Path) -> None:
    """Create producer-shaped complete and partial explicit/high declarations."""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE task_attribution (
        task_id TEXT PRIMARY KEY, created_at REAL, outcome TEXT,
        provider TEXT, model TEXT, uncached_input_tokens INTEGER,
        cache_read_tokens INTEGER, cache_write_tokens INTEGER,
        output_tokens INTEGER, reasoning_tokens INTEGER,
        safety_or_live_change INTEGER, broad_architecture_change INTEGER,
        bounded_routine_fix INTEGER, human_gate_required INTEGER,
        risk_declaration_source TEXT, risk_declaration_confidence TEXT
    )""")
    conn.executemany("INSERT INTO task_attribution VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        ("declared-routine", 100, "completed", "example-provider", "example-model", 1, 0, 0, 1, 0, 0, 0, 1, 0, "explicit", "high"),
        ("declared-safety", 101, "completed", "example-provider", "example-model", 1, 0, 0, 1, 0, 1, 0, 0, 0, "explicit", "high"),
        ("partial-routine", 102, "completed", "example-provider", "example-model", 1, 0, 0, 1, 0, None, None, 1, None, "explicit", "high"),
    ])
    conn.commit()
    conn.close()


class OrganicReportProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "attribution.sqlite"
        self.rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
        _db(self.db_path, self.rows[:1])

    def _report_with_pre_watermark_row(self) -> dict[str, object]:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO task_attribution VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("later-task", 200, "completed", "example-provider", "example-model", 1, 0, 0, 1, 0),
        )
        conn.commit()
        conn.close()
        return produce_shadow_report([self.db_path])

    def test_policy_logic_revision_is_source_hash_in_additive_report_schema(self) -> None:
        from quota_core.context_economics import policy
        from quota_core.context_economics.report_producer import policy_logic_revision

        expected = hashlib.sha256(Path(policy.__file__).read_bytes()).hexdigest()
        report = produce_shadow_report([self.db_path])
        self.assertEqual(policy_logic_revision(), expected)
        self.assertEqual(report["policy_logic_revision"], expected)
        self.assertEqual(report_contract_schema()["properties"]["policy_logic_revision"]["pattern"], "^[0-9a-f]{64}$")
        self.assertNotIn("policy_logic_revision", report_contract_schema()["required"])

    def test_changed_or_missing_policy_logic_revision_refreshes_old_rows_and_v1(self) -> None:
        from quota_core.context_economics.acceptance import FAIL
        from quota_core.context_economics.report_producer import policy_logic_revision, shadow_comparison_report

        fresh = self._report_with_pre_watermark_row()
        for prior_revision in ("0" * 64, None):
            with self.subTest(prior_revision=prior_revision):
                prior = json.loads(json.dumps(fresh))
                if prior_revision is None:
                    prior.pop("policy_logic_revision", None)
                else:
                    prior["policy_logic_revision"] = prior_revision
                prior["decisions"]["organic-known-zero"]["policy_decision"]["recommended_session_treatment"] = "preserve"
                retired = json.loads(json.dumps(prior["decisions"]["later-task"]))
                retired["task_id"] = "retired-task"
                retired["policy_decision"]["task_id"] = "retired-task"
                prior["decisions"]["retired-task"] = retired
                prior["decision_count"] += 1
                self.assertEqual(check_acceptance(prior, since=100, rerun_bytes_equal=True)["criteria"]["V1"]["status"], FAIL)
                with patch("quota_core.context_economics.report_producer.shadow_comparison_report", wraps=shadow_comparison_report) as emit:
                    refreshed = produce_shadow_report([self.db_path], prior)
                self.assertEqual([record.task_id for record in emit.call_args.args[0]], ["later-task", "organic-known-zero"])
                self.assertEqual(refreshed["decisions"]["organic-known-zero"]["policy_decision"]["recommended_session_treatment"], "insufficient_evidence")
                self.assertEqual(refreshed["decisions"]["retired-task"], retired)
                self.assertEqual(check_acceptance(refreshed, since=100, rerun_bytes_equal=True)["criteria"]["V1"]["status"], PASS)
                self.assertEqual(refreshed["policy_logic_revision"], policy_logic_revision())
                self.assertEqual(refreshed["decision_count"], prior["decision_count"])

    def test_matching_revision_reuses_old_rows_and_retains_absent_source_rows(self) -> None:
        from quota_core.context_economics.report_producer import shadow_comparison_report

        prior = self._report_with_pre_watermark_row()
        prior["decisions"]["organic-known-zero"]["policy_decision"]["recommended_session_treatment"] = "preserve"
        prior["decisions"]["retired-task"] = {"task_id": "retired-task", "retained": True}
        prior["decision_count"] += 1
        with patch("quota_core.context_economics.report_producer.shadow_comparison_report", wraps=shadow_comparison_report) as emit:
            refreshed = produce_shadow_report([self.db_path], prior)
        self.assertEqual([record.task_id for record in emit.call_args.args[0]], ["later-task"])
        self.assertEqual(refreshed["decisions"]["organic-known-zero"], prior["decisions"]["organic-known-zero"])
        self.assertEqual(refreshed["decisions"]["retired-task"], prior["decisions"]["retired-task"])
        self.assertEqual(refreshed["decision_count"], 3)
        self.assertEqual(json.dumps(refreshed, sort_keys=True), json.dumps(produce_shadow_report([self.db_path], refreshed), sort_keys=True))

    def test_fractional_watermark_survives_json_round_trip_and_reuses_old_rows(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE task_attribution SET created_at = ?", (100.125,))
        conn.execute(
            "INSERT INTO task_attribution VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("older", 99.25, "completed", "example-provider", "example-model", 1, 0, 0, 1, 0),
        )
        conn.commit()
        conn.close()
        first = produce_shadow_report([self.db_path])
        published = json.loads(json.dumps(first))
        self.assertEqual(published["watermark"]["created_at"], 100.125)
        self.assertEqual(published["decisions"]["organic-known-zero"]["created_at"], 100.125)
        self.assertEqual(report_contract_schema()["properties"]["watermark"]["properties"]["created_at"]["type"], ["number", "null"])

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO task_attribution VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("newer", 101.5, "completed", "example-provider", "example-model", 1, 0, 0, 1, 0),
        )
        conn.commit()
        conn.close()
        from quota_core.context_economics.report_producer import shadow_comparison_report
        with patch("quota_core.context_economics.report_producer.shadow_comparison_report", wraps=shadow_comparison_report) as emit:
            second = produce_shadow_report([self.db_path], published)
        self.assertEqual(emit.call_count, 1)
        # The prior watermark is intentionally inclusive; only the older row is reused.
        self.assertEqual([record.task_id for record in emit.call_args.args[0]], ["newer", "organic-known-zero"])
        self.assertEqual(second["watermark"]["created_at"], 101.5)

    def test_report_is_keyed_by_task_id_and_explicitly_marks_missing_evidence(self) -> None:
        report = produce_shadow_report([self.db_path])
        artifact = report["decisions"]["organic-known-zero"]
        self.assertEqual(report["decision_count"], 1)
        self.assertEqual(report["mode"], "shadow")
        self.assertEqual(report_contract_schema()["$id"], report["report_contract_id"])
        self.assertEqual(report["policy_contract"]["sha256"], policy_contract_sha256())
        self.assertIsNone(artifact["policy_decision"]["evidence"]["required_context_recalled"])
        self.assertEqual(
            artifact["evidence_provenance"]["required_context_recalled"],
            "applicability_unknown",
        )
        self.assertEqual(
            artifact["policy_decision"]["recommended_session_treatment"],
            "insufficient_evidence",
        )
        # Measured zero survives; null cache telemetry remains unknown.
        observed = artifact["baseline_vs_recommended"]
        self.assertEqual(observed["uncached_input_tokens"]["observed"], 0)
        self.assertIsNone(observed["cache_read_tokens"]["observed"])

    def test_risk_facts_are_emitted_separately_from_policy_decision_evidence(self) -> None:
        risk_db = Path(self.temp.name) / "risk.sqlite"
        _risk_db(risk_db)
        report = produce_shadow_report([risk_db])
        artifact = report["decisions"]["declared-routine"]
        self.assertEqual(artifact["risk_facts"], {
            "safety_or_live_change": False,
            "broad_architecture_change": False,
            "bounded_routine_fix": True,
            "human_gate_required": False,
        })
        self.assertNotIn("bounded_routine_fix", artifact["policy_decision"]["evidence"])
        result = check_acceptance(report, since=100, rerun_bytes_equal=True)
        self.assertEqual(result["criteria"]["D3"]["status"], PASS)
        self.assertEqual(result["criteria"]["D3"]["declared_low_scrutiny_count"], 1)
        self.assertEqual(result["criteria"]["D3"]["unexpected_escalation_count"], 0)
        self.assertEqual(result["criteria"]["D1"]["declaration_distribution"], {
            "routine": 1, "safety_or_live": 1,
        })
        self.assertEqual(
            (result["criteria"]["V1"]["status"], result["criteria"]["V1"]["checked_count"]),
            (PASS, 1),
        )
        self.assertEqual(result["criteria"]["C2"]["coverage"]["safety_or_live_change"], 2 / 3)

        escalated = json.loads(json.dumps(report))
        escalated["decisions"]["declared-routine"]["policy_decision"].update({
            "risk_tier": "safety_or_live", "human_gate_required": True,
        })
        self.assertEqual(
            check_acceptance(escalated, since=100, rerun_bytes_equal=True)["criteria"]["D3"]["status"],
            "FAIL",
        )

    def test_rolling_report_refreshes_artifacts_that_predate_risk_facts(self) -> None:
        risk_db = Path(self.temp.name) / "risk.sqlite"
        _risk_db(risk_db)
        legacy = produce_shadow_report([risk_db])
        for artifact in legacy["decisions"].values():
            artifact.pop("risk_facts")
        refreshed = produce_shadow_report([risk_db], legacy)
        self.assertTrue(all("risk_facts" in artifact for artifact in refreshed["decisions"].values()))
        criteria = check_acceptance(refreshed, since=100, rerun_bytes_equal=True)["criteria"]
        self.assertEqual(criteria["D1"]["missing_risk_facts_count"], 0)
        self.assertEqual(criteria["D3"]["declared_low_scrutiny_count"], 1)

    def test_rerun_upserts_new_tied_and_null_timestamp_rows_deterministically(self) -> None:
        first = produce_shadow_report([self.db_path])
        conn = sqlite3.connect(self.db_path)
        for row in self.rows[1:]:
            conn.execute(
                "INSERT INTO task_attribution VALUES (:task_id, :created_at, :outcome, "
                ":provider, :model, :uncached_input_tokens, :cache_read_tokens, "
                ":cache_write_tokens, :output_tokens, :reasoning_tokens)", row,
            )
        conn.execute(
            "INSERT INTO task_attribution VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("organic-tied-watermark", 100, "completed", "example-provider", "example-model", 2, 0, 0, 1, 0),
        )
        conn.commit()
        conn.close()
        second = produce_shadow_report([self.db_path], first)
        third = produce_shadow_report([self.db_path], second)
        self.assertEqual(sorted(second["decisions"]), [
            "organic-known-zero", "organic-null-created-at", "organic-tied-watermark",
        ])
        self.assertEqual(second, third)
        self.assertEqual(second["watermark"]["task_ids_at_created_at"], [
            "organic-known-zero", "organic-tied-watermark",
        ])

    def test_late_review_verdict_refreshes_an_old_task_below_task_watermark(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO task_attribution VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            "later-task", 200, "completed", "example-provider", "example-model", 1, 0, 0, 1, 0,
        ))
        conn.execute("CREATE TABLE tasks (task_id TEXT, task_type TEXT, context TEXT, verdict TEXT, created_at REAL)")
        conn.commit()
        conn.close()
        first = produce_shadow_report([self.db_path])
        self.assertIsNone(first["decisions"]["organic-known-zero"]["policy_decision"]["evidence"]["independent_review_correct"])

        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", (
            "review-late", "review", '{"prev_task_id":"organic-known-zero"}', "approve", 300,
        ))
        conn.commit()
        conn.close()
        second = produce_shadow_report([self.db_path], first)
        artifact = second["decisions"]["organic-known-zero"]
        self.assertTrue(artifact["policy_decision"]["evidence"]["independent_review_correct"])
        self.assertEqual(artifact["evidence_provenance"]["independent_review_correct"], "review_verdict:approve:review-late")
        self.assertNotEqual(
            first["decisions"]["organic-known-zero"]["evidence_fingerprint"],
            artifact["evidence_fingerprint"],
        )

    def test_lookup_never_silently_misses(self) -> None:
        report = produce_shadow_report([self.db_path])
        self.assertEqual(decision_for(report, "organic-known-zero")["status"], COVERED)
        missing = decision_for(report, "not-in-report")
        self.assertEqual(missing, {"status": NOT_COVERED, "task_id": "not-in-report"})

    def test_cli_reuses_out_as_rolling_state_and_source_is_opened_readonly(self) -> None:
        out = Path(self.temp.name) / "report.json"
        with patch("quota_core.context_economics.report_producer.sqlite3.connect", wraps=sqlite3.connect) as connect:
            self.assertEqual(main(["--db", str(self.db_path), "--out", str(out)]), 0)
        self.assertTrue(any("mode=ro" in str(call.args[0]) for call in connect.call_args_list))
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(written, produce_shadow_report([self.db_path], written))

    def test_cli_warns_when_db_list_is_unreadable_without_hiding_direct_sources(self) -> None:
        out = Path(self.temp.name) / "report.json"
        with self.assertLogs("quota_core.context_economics.report_producer", level="WARNING") as logs:
            self.assertEqual(main([
                "--db", str(self.db_path), "--db-list", str(Path(self.temp.name) / "missing.json"), "--out", str(out),
            ]), 0)
        self.assertIn("could not read --db-list", "\n".join(logs.output))
        self.assertIn("organic-known-zero", json.loads(out.read_text(encoding="utf-8"))["decisions"])
