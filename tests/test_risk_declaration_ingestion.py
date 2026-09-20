"""Trusted attribution risk/recall ingestion is conservative."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from quota_core.context_economics.quality_evidence import ingest_attribution_quality_evidence


class RiskDeclarationIngestionTests(unittest.TestCase):
    def test_trust_boundary_and_all_recall_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE task_attribution (task_id TEXT, safety_or_live_change INTEGER, broad_architecture_change INTEGER, bounded_routine_fix INTEGER, human_gate_required INTEGER, risk_declaration_source TEXT, risk_declaration_confidence TEXT, required_context_recalled INTEGER, context_pack_hash TEXT)")
            rows = [
                ("trusted", 0, 0, 1, 0, "explicit", "high", 1, "pack"),
                ("heuristic", 0, 0, 1, 0, "heuristic", "low", 0, "pack"),
                ("missing", None, None, None, None, None, None, None, "pack"),
                ("na", None, None, None, None, None, None, None, None),
            ]
            conn.executemany("INSERT INTO task_attribution VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows); conn.commit(); conn.close()
            evidence = ingest_attribution_quality_evidence(path, [row[0] for row in rows])
        self.assertTrue(evidence["trusted"].evidence.bounded_routine_fix)
        self.assertEqual(evidence["trusted"].provenance["bounded_routine_fix"], "producer_declared:explicit/high")
        self.assertIsNone(evidence["heuristic"].evidence.bounded_routine_fix)
        self.assertIn("not_trusted", evidence["heuristic"].provenance["bounded_routine_fix"])
        self.assertEqual(evidence["trusted"].provenance["recall_applicability"], "observed_true")
        self.assertEqual(evidence["heuristic"].provenance["recall_applicability"], "observed_false")
        self.assertEqual(evidence["missing"].provenance["recall_applicability"], "applicable_but_missing")
        self.assertIsNone(evidence["na"].evidence.recall_not_applicable)
        self.assertEqual(evidence["na"].provenance["recall_applicability"], "applicability_unknown")

    def test_legacy_schema_is_absent_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.sqlite"
            conn = sqlite3.connect(path); conn.execute("CREATE TABLE task_attribution (task_id TEXT)"); conn.execute("INSERT INTO task_attribution VALUES ('old')"); conn.commit(); conn.close()
            evidence = ingest_attribution_quality_evidence(path, ["old"])["old"]
        self.assertIsNone(evidence.evidence.safety_or_live_change)
        self.assertEqual(evidence.provenance["safety_or_live_change"], "not_recorded_by_producer")
        self.assertEqual(evidence.provenance["recall_applicability"], "applicability_unknown")
