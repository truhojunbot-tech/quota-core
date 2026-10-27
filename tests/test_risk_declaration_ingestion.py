"""Trusted attribution risk/recall ingestion is conservative."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from quota_core.context_economics import (
    attribution_from_dict, correlate_task_economics, recommend_task_policy,
)
from quota_core.context_economics.quality_evidence import ingest_attribution_quality_evidence
from quota_core.context_economics.report_producer import _artifact_evidence


class RiskDeclarationIngestionTests(unittest.TestCase):
    def test_explicit_recall_nonapplicability_preserves_quality_only_when_recall_unobserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "recall.sqlite"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE task_attribution (task_id TEXT, "
                             "safety_or_live_change INTEGER, broad_architecture_change INTEGER, "
                             "bounded_routine_fix INTEGER, human_gate_required INTEGER, "
                             "risk_declaration_source TEXT, risk_declaration_confidence TEXT, "
                             "required_context_recalled INTEGER, recall_not_applicable INTEGER, "
                             "context_pack_hash TEXT)")
                conn.executemany("INSERT INTO task_attribution VALUES (?,?,?,?,?,?,?,?,?,?)", [
                    ("root", 0, 0, 1, 0, "explicit", "high", None, 1, None),
                    ("string", 0, 0, 1, 0, "explicit", "high", None, "true", None),
                    ("negative", 0, 0, 1, 0, "explicit", "high", 0, 1, None),
                    ("positive", 0, 0, 1, 0, "explicit", "high", 1, 1, None),
                    ("false_na", 0, 0, 1, 0, "explicit", "high", None, 0, "pack"),
                    ("null_na", 0, 0, 1, 0, "explicit", "high", None, None, None),
                ])
                conn.execute("CREATE TABLE tasks (task_id TEXT, task_type TEXT, context TEXT, "
                             "verdict TEXT, created_at REAL)")
                conn.executemany("INSERT INTO tasks VALUES (?,?,?,?,?)", [
                    ("root", "implement", "{}", None, 100),
                    ("review-root", "review", '{"prev_task_id":"root"}', "approve", 110),
                ])
            evidence = ingest_attribution_quality_evidence(
                path, ["root", "string", "negative", "positive", "false_na", "null_na"])
            combined, provenance = _artifact_evidence(str(path), ["root"])

        record = correlate_task_economics([attribution_from_dict({
            "runtime": "portable", "task_id": "root", "outcome": "completed",
            "task_type": "implement", "agent": "provider-a",
        })], [])[0]
        self.assertTrue(combined["root"].independent_review_correct)
        self.assertTrue(combined["root"].recall_not_applicable)
        self.assertEqual(provenance["root"]["recall_applicability"], "producer_not_applicable")
        self.assertEqual(recommend_task_policy(record, combined["root"]).quality_evidence_state,
                         "preserving")
        for task_id, expected_state, expected_recall, expected_na, expected_provenance in (
            ("root", "preserving", None, True, "producer_not_applicable"),
            ("string", "unknown", None, None, "applicability_unknown"),
            ("negative", "not_preserving", False, None, "observed_false"),
            ("positive", "preserving", True, None, "observed_true"),
            ("false_na", "unknown", None, None, "applicable_but_missing"),
            ("null_na", "unknown", None, None, "applicability_unknown"),
        ):
            with self.subTest(task_id=task_id):
                item = evidence[task_id]
                self.assertIs(item.evidence.required_context_recalled, expected_recall)
                self.assertIs(item.evidence.recall_not_applicable, expected_na)
                self.assertEqual(item.provenance["required_context_recalled"], expected_provenance)
                self.assertEqual(item.provenance["recall_applicability"], expected_provenance)
                decision = recommend_task_policy(
                    record, replace(item.evidence, independent_review_correct=True))
                self.assertEqual(decision.quality_evidence_state, expected_state)

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
