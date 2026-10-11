"""One computation produces both the rolling report and reader contract."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.compare_contracts import differences
from quota_core.context_economics.report_producer import (
    build_contract, main, produce_shadow_report, write_contract_atomically,
    _produce_shadow_report, compare_contract_to_source,
)

FIXTURE = Path(__file__).parent / "fixtures/economics_policy/organic-report-rows.json"


class ContractOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "attribution.sqlite"
        self.rows = json.loads(FIXTURE.read_text())
        conn = sqlite3.connect(self.db)
        conn.execute("""CREATE TABLE task_attribution (
            task_id TEXT PRIMARY KEY, created_at REAL, outcome TEXT,
            provider TEXT, model TEXT, uncached_input_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            output_tokens INTEGER, reasoning_tokens INTEGER)""")
        conn.executemany("""INSERT INTO task_attribution VALUES
            (:task_id, :created_at, :outcome, :provider, :model,
             :uncached_input_tokens, :cache_read_tokens, :cache_write_tokens,
             :output_tokens, :reasoning_tokens)""", self.rows)
        conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, receipt_id TEXT)")
        conn.execute("INSERT INTO tasks VALUES (?, ?)", (self.rows[0]["task_id"], "receipt-123"))
        conn.commit()
        conn.close()

    def test_cli_writes_both_shapes_from_identical_decisions(self):
        report_path = self.root / "report.json"
        contract_path = self.root / "contract.json"
        self.assertEqual(main(["--db", str(self.db), "--out", str(report_path),
                               "--contract-out", str(contract_path)]), 0)
        report = json.loads(report_path.read_text())
        contract = json.loads(contract_path.read_text())
        self.assertEqual(contract["contract_version"], "1.0")
        self.assertEqual(contract["mode"], "shadow")
        self.assertIsInstance(contract["decisions"], list)
        self.assertEqual(contract["decision_count"], len(self.rows))
        self.assertEqual(contract["provenance"]["decision_count"], len(self.rows))
        self.assertEqual(contract["produced_at"], contract["provenance"]["produced_at"])
        self.assertEqual(contract["provenance"]["producer"], "quota_core.context_economics.report_producer")
        self.assertEqual(contract["provenance"]["build_sha"], contract["provenance"]["producer_commit"])
        self.assertEqual(contract["provenance"]["source_receipt_ids"], {self.rows[0]["task_id"]: "receipt-123"})
        self.assertEqual(contract["provenance"]["snapshot_at"], contract["produced_at"])
        self.assertEqual({d["task_id"]: d for d in contract["decisions"]},
                         {key: value["policy_decision"] for key, value in report["decisions"].items()})
        self.assertEqual(report, produce_shadow_report([self.db]))

    def test_contract_builder_does_not_recompute_policy(self):
        report, records = _produce_shadow_report([self.db])
        with patch("quota_core.context_economics.policy.recommend_task_policy",
                   side_effect=AssertionError("second decision computation")):
            contract = build_contract(report, records, [self.db])
        self.assertEqual(contract["decision_count"], len(self.rows))

    def test_top_level_produced_at_matches_provenance_and_reader_shape(self):
        report, records = _produce_shadow_report([self.db])
        timestamp = "2026-10-01T12:34:56+00:00"
        contract = build_contract(report, records, [self.db], produced_at=timestamp)
        self.assertEqual(contract["produced_at"], timestamp)
        self.assertEqual(contract["produced_at"], contract["provenance"]["produced_at"])
        contract_path = self.root / "contract.json"
        write_contract_atomically(contract_path, contract)
        contract = json.loads(contract_path.read_text())
        # The consumer accepts extra top-level keys and resolves by task ID.
        self.assertEqual(contract["contract_version"], "1.0")
        self.assertEqual(contract["mode"], "shadow")
        decision = next(
            item for item in contract["decisions"]
            if isinstance(item, dict) and item.get("task_id") == self.rows[0]["task_id"]
        )
        self.assertEqual(decision["task_id"], self.rows[0]["task_id"])

    def test_default_clock_is_sampled_once_for_both_timestamps(self):
        report, records = _produce_shadow_report([self.db])
        instant = datetime(2026, 10, 5, 12, 34, 56, tzinfo=timezone.utc)
        with patch("quota_core.context_economics.report_producer.datetime") as clock:
            clock.now.return_value = instant
            contract = build_contract(report, records, [self.db])
        clock.now.assert_called_once_with(timezone.utc)
        self.assertEqual(contract["produced_at"], instant.isoformat(timespec="seconds"))
        self.assertEqual(contract["produced_at"], contract["provenance"]["produced_at"])

    def test_published_contract_without_top_level_timestamp_is_equivalent(self):
        published = {"contract_version": "1.0", "provenance": {"produced_at": "old"}}
        candidate = {**published, "produced_at": "new"}
        self.assertEqual(differences(published, candidate), [])
        self.assertEqual(differences(candidate, published), [])
        self.assertEqual(differences(published, {**candidate, "mode": "enforce"}),
                         [("mode", "<missing>", "enforce")])

    def test_atomic_write_keeps_previous_contract_on_failure(self):
        path = self.root / "contract.json"
        write_contract_atomically(path, {"decisions": []})
        original = path.read_bytes()
        with patch("quota_core.context_economics.report_producer.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_contract_atomically(path, {"decisions": [{}]})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["attribution.sqlite", "contract.json"])
        self.assertEqual(hashlib.sha256(original).hexdigest(),
                         write_contract_atomically(path, {"decisions": []}))

    def test_rejects_same_output_path(self):
        path = self.root / "same.json"
        with self.assertRaises(SystemExit):
            main(["--db", str(self.db), "--out", str(path), "--contract-out", str(path)])
        self.assertFalse(path.exists())

    def test_parity_reports_both_id_directions_and_field_differences(self):
        report, records = _produce_shadow_report([self.db])
        contract = build_contract(report, records, [self.db], produced_at="2026-10-11T00:00:00+00:00")
        self.assertEqual(compare_contract_to_source(contract, [self.db])["mismatch_count"], 0)
        modified = json.loads(json.dumps(contract))
        missing = modified["decisions"].pop()
        modified["decisions"][0]["recommended_max_review_fix_rounds"] = 999
        modified["decisions"].append({"task_id": "extra-task"})
        parity = compare_contract_to_source(modified, [self.db])
        self.assertEqual(parity["missing_task_ids"], [missing["task_id"]])
        self.assertEqual(parity["extra_task_ids"], ["extra-task"])
        self.assertEqual(parity["field_diff_count"], 1)
        self.assertEqual(parity["mismatch_count"], 3)

    def test_parity_cli_reads_contract_without_writing_and_honors_cutoff(self):
        report, records = _produce_shadow_report([self.db])
        contract = build_contract(report, records, [self.db])
        path = self.root / "contract.json"
        write_contract_atomically(path, contract)
        original = path.read_bytes()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--db", str(self.db), "--parity-check", str(path),
                                   "--at", "2100-01-01T00:00:00+00:00"]), 0)
        self.assertEqual(json.loads(output.getvalue())["mismatch_count"], 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
