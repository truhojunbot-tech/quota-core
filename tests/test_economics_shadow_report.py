"""Tests for quota-core#80's read-only SQLite shadow-report slice."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from quota_core.cli import main
from quota_core.context_economics import (
    read_task_attribution_sqlite,
    shadow_comparison_report,
    stratified_failure_rates,
)


def _make_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE task_attribution (
                task_id TEXT, status TEXT, task_type TEXT, provider TEXT,
                model TEXT, context_policy TEXT, retry_of TEXT, fallback_of TEXT,
                uncached_input_tokens INTEGER, cache_write_tokens INTEGER,
                cache_read_tokens INTEGER, output_tokens INTEGER,
                reasoning_tokens INTEGER, context_window_tokens INTEGER,
                stable_prefix_hash TEXT, context_pack_hash TEXT
            )"""
        )
        conn.executemany(
            "INSERT INTO task_attribution VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "task-b", "completed", "implement", "provider-a", "model-a", "resume",
                    None, None, 0, None, 25, 5, None, 25, "prefix-redacted", None,
                ),
                (
                    "task-a", "failed", "review", "provider-b", "model-b", "fresh",
                    "task-before", "task-fallback", None, 3, None, 2, 1, None,
                    None, "pack-redacted",
                ),
            ],
        )


def _make_status_database(path: Path, statuses: list[tuple[str, str]]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE task_attribution (task_id TEXT, status TEXT)")
        conn.executemany("INSERT INTO task_attribution VALUES (?, ?)", statuses)


class EconomicsShadowReportTests(unittest.TestCase):
    def test_readonly_loader_preserves_nulls_zeroes_and_corrected_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "synthetic.sqlite"
            _make_database(db_path)
            with patch("quota_core.context_economics.sqlite_attribution.sqlite3.connect", wraps=sqlite3.connect) as connect:
                records = read_task_attribution_sqlite(db_path)
            self.assertIn("mode=ro", connect.call_args.args[0])
        self.assertEqual([record.task_id for record in records], ["task-a", "task-b"])
        completed = records[1]
        self.assertEqual(completed.outcome, "success")
        self.assertEqual(completed.task_telemetry.uncached_input_tokens, 0)
        self.assertIsNone(completed.task_telemetry.cache_write_tokens)
        self.assertEqual(completed.task_telemetry.cache_read_tokens, 25)
        self.assertEqual(completed.stable_prefix_hash, "prefix-redacted")
        self.assertIsNone(completed.context_pack_hash)

    def test_loader_is_tolerant_of_missing_or_unsafe_table_names(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "synthetic.sqlite"
            _make_database(db_path)
            self.assertEqual(read_task_attribution_sqlite(db_path, "missing_table"), [])
            self.assertEqual(read_task_attribution_sqlite(db_path, 'task_attribution"; DROP TABLE task_attribution; --'), [])

    def test_null_outcome_falls_back_to_a_terminal_status(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "synthetic.sqlite"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE task_attribution (task_id TEXT, outcome TEXT, status TEXT)")
                conn.execute("INSERT INTO task_attribution VALUES (?, ?, ?)", ("terminal", None, "completed"))
            records = read_task_attribution_sqlite(db_path)
        self.assertEqual(records[0].outcome, "success")

    def test_status_only_nonterminal_rows_are_not_failure_observations(self):
        unfinished = ["in_progress", "pending", "blocked", "needs_human", "timed_out"]
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "synthetic.sqlite"
            _make_status_database(
                db_path,
                [(f"unfinished-{index}", status) for index, status in enumerate(unfinished)]
                + [("failed", "failed"), ("completed", "completed")],
            )
            records = read_task_attribution_sqlite(db_path)
        by_id = {record.task_id: record for record in records}
        for index in range(len(unfinished)):
            self.assertIsNone(by_id[f"unfinished-{index}"].outcome)
        rates = stratified_failure_rates(records)
        self.assertEqual(rates["observed_count"], 2)
        self.assertEqual(rates["raw_failure_count"], 1)

    def test_report_compares_actual_provenance_without_inventing_rounds_or_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "synthetic.sqlite"
            _make_database(db_path)
            report = shadow_comparison_report(read_task_attribution_sqlite(db_path))
        self.assertEqual(report["mode"], "shadow")
        self.assertEqual(report["artifact_count"], 2)
        artifact = report["artifacts"][0]
        actual = artifact["shadow_comparison"]["actual"]
        decision = artifact["policy_decision"]
        self.assertEqual(actual["session_treatment"], "renew")
        self.assertEqual(actual["retry_of"], "task-before")
        self.assertIsNone(actual["review_fix_rounds"])
        self.assertIn("evidence", decision)
        self.assertIn("provenance", decision)
        self.assertIn("rationale", decision)
        self.assertIn("confidence", decision)
        self.assertIn("override_reasons", decision)
        self.assertNotIn("total", decision["recommended_soft_budget"] or {})
        self.assertNotIn("cache_read", decision["orchestration_waste"])

    def test_cli_emits_deterministic_json_and_optional_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "synthetic.sqlite"
            output_path = Path(directory) / "report.json"
            _make_database(db_path)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main([
                    "context-economics-shadow-report", "--database", str(db_path),
                    "--output", str(output_path),
                ]), 0)
            emitted = json.loads(stdout.getvalue())
            persisted = json.loads(output_path.read_text())
        self.assertEqual(emitted, persisted)
        self.assertEqual(emitted["artifact_count"], 2)
