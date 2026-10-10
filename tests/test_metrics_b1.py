"""B1.3 def_version 1 metrics, using source-shaped read-only fixtures."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quota_core.context_economics.metrics_b1 import compute_metrics, format_table, _time


FIXTURES = Path(__file__).parent / "fixtures" / "context_economics"


class B1MetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.crew_root = Path(self.tmp.name) / "crews"
        bot = self.crew_root / "alpha"
        bot.mkdir(parents=True)
        conn = sqlite3.connect(bot / "tasks.db")
        conn.execute("CREATE TABLE tokenomics_shadow_receipts (task_id TEXT, economics_json TEXT, created_at REAL)")
        conn.execute("CREATE TABLE tasks (task_id TEXT, created_at REAL)")
        conn.execute("INSERT INTO tokenomics_shadow_receipts VALUES (?, ?, ?)",
                     ("a", json.dumps({"uncached_input_tokens": 10, "cache_write_tokens": 10,
                                       "cache_read_tokens": 80}), 1791586800.0))
        conn.execute("INSERT INTO tokenomics_shadow_receipts VALUES (?, ?, ?)",
                     ("b", json.dumps({"uncached_input_tokens": None, "cache_write_tokens": None,
                                       "cache_read_tokens": 0}), 1791586801.0))
        conn.commit()
        conn.close()

    def test_m1_and_hook_metrics_use_known_rows_and_exclude_headless(self):
        rows = compute_metrics(1791580000, 1791594000, self.crew_root,
                               FIXTURES / "b1_hook_events.jsonl")
        by_metric = {row["metric"]: row for row in rows if row["bot"] == "alpha"}
        self.assertEqual(by_metric["M1"]["value"], 0.8)
        self.assertEqual(by_metric["M1"]["n"], 1)
        self.assertIsNone(by_metric["M1"]["bot_session_value"])
        self.assertIn("lacks", by_metric["M1"]["bot_session_reason"])
        self.assertEqual(by_metric["M2"]["value"], 1)
        self.assertEqual(by_metric["M2"]["n"], 2)
        self.assertEqual(by_metric["M4"]["value"]["max_bytes"], 8800)
        self.assertEqual(by_metric["M4"]["value"]["over_8900_count"], 0)
        self.assertEqual(by_metric["M4"]["n"], 2)
        self.assertEqual(by_metric["M5"]["value"]["trimmed_bytes_total"], 5)
        self.assertEqual(by_metric["M5"]["n"], 2)
        self.assertTrue(all(row["def_version"] == 1 for row in rows))
        self.assertTrue(all(row["value"] is not None or row.get("reason") for row in rows))
        self.assertIn("M1", format_table(rows))
        self.assertEqual({row["metric"] for row in rows if row["bot"] == "alpha"},
                         {"M1", "M2", "M3a", "M3b", "M3c", "M4", "M5", "M6", "M7", "M8"})

    def test_unavailable_axes_are_null_with_reasons_not_fabricated_zeros(self):
        rows = compute_metrics(1791580000, 1791594000, self.crew_root,
                               FIXTURES / "b1_hook_events.jsonl")
        by_metric = {row["metric"]: row for row in rows if row["bot"] == "alpha"}
        for metric in ("M3a", "M3b", "M3c", "M7", "M8"):
            self.assertIsNone(by_metric[metric]["value"])
            self.assertTrue(by_metric[metric]["reason"])

    def test_time_offsets_are_preserved_and_m3c_needs_all_tasks(self):
        self.assertEqual(_time("2026-10-09T09:00:00+09:00"),
                         _time("2026-10-09T00:00:00Z"))
        bot = self.crew_root / "alpha"
        conn = sqlite3.connect(bot / "tasks.db")
        conn.execute("INSERT INTO tasks VALUES ('a', 1791586800.0)")
        conn.commit()
        conn.close()
        (bot / "context_events.jsonl").write_text(json.dumps({
            "schema_version": 1, "event_type": "context_pack_built",
            "ts": "2026-10-09T23:00:00Z", "task_id": "a", "project": "alpha",
            "tokens_by_category": {"lineage": 12},
        }) + "\n")
        rows = compute_metrics(1791580000, 1791594000, self.crew_root,
                               FIXTURES / "b1_hook_events.jsonl")
        m3c = next(row for row in rows if row["bot"] == "alpha" and row["metric"] == "M3c")
        self.assertEqual(m3c["value"], 1.0)
        conn = sqlite3.connect(bot / "tasks.db")
        conn.execute("INSERT INTO tasks VALUES ('b', 1791586801.0)")
        conn.commit()
        conn.close()
        rows = compute_metrics(1791580000, 1791594000, self.crew_root,
                               FIXTURES / "b1_hook_events.jsonl")
        m3c = next(row for row in rows if row["bot"] == "alpha" and row["metric"] == "M3c")
        self.assertIsNone(m3c["value"])
        self.assertIn("not_all", m3c["reason"])
