"""B1.3 def_version 1 metrics, using source-shaped read-only fixtures."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
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

    def test_hook_boundaries_pairing_percentile_and_boolean_rejection(self):
        rows = compute_metrics(100, 200, self.crew_root,
                               FIXTURES / "b1_hook_boundaries.jsonl", bots=["alpha"])
        metrics = {row["metric"]: row for row in rows if row["bot"] == "alpha"}
        self.assertEqual(metrics["M4"]["n"], 3)
        self.assertEqual(metrics["M4"]["value"],
                         {"max_bytes": 8901, "over_8900_count": 1})
        self.assertEqual(metrics["M5"]["n"], 2)
        self.assertEqual(metrics["M5"]["value"],
                         {"trimmed_bytes_total": 4, "standing_trimmed_total": 6})
        self.assertEqual(metrics["M6"]["n"], 3)
        self.assertEqual(metrics["M6"]["value"], 30)

    def test_zero_and_boolean_receipts_do_not_enter_m1_denominator(self):
        conn = sqlite3.connect(self.crew_root / "alpha" / "tasks.db")
        conn.executemany("INSERT INTO tokenomics_shadow_receipts VALUES (?, ?, ?)", [
            ("zero", json.dumps({"uncached_input_tokens": 0, "cache_write_tokens": 0,
                                  "cache_read_tokens": 0}), 1791586802.0),
            ("bool", json.dumps({"uncached_input_tokens": True, "cache_write_tokens": 0,
                                  "cache_read_tokens": 0}), 1791586803.0),
        ])
        conn.commit()
        conn.close()
        rows = compute_metrics(1791580000, 1791594000, self.crew_root,
                               FIXTURES / "b1_hook_events.jsonl")
        m1 = next(row for row in rows if row["bot"] == "alpha" and row["metric"] == "M1")
        self.assertEqual((m1["n"], m1["value"]), (1, 0.8))

    def test_eval_path_missing_and_wrong_sha_are_distinct(self):
        absent = Path(self.tmp.name) / "absent-eval.json"
        wrong = Path(self.tmp.name) / "wrong-eval.json"
        wrong.write_text("not the frozen evaluation set")
        for path, reason in ((absent, "frozen_eval_set_missing"),
                             (wrong, "frozen_eval_set_sha256_mismatch")):
            with self.subTest(reason=reason):
                rows = compute_metrics(100, 200, self.crew_root,
                                       FIXTURES / "b1_hook_boundaries.jsonl",
                                       bots=["alpha"], eval_path=path)
                for metric in ("M3a", "M3b"):
                    row = next(row for row in rows if row["bot"] == "alpha"
                               and row["metric"] == metric)
                    self.assertEqual(row["source"], str(path))
                    self.assertEqual(row["reason"], reason)

    def test_naive_timestamp_is_utc_even_under_non_utc_local_timezone(self):
        if not hasattr(time, "tzset"):
            self.skipTest("requires time.tzset")
        previous = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Asia/Seoul"
            time.tzset()
            self.assertEqual(_time("2026-10-09T00:00:00"),
                             _time("2026-10-09T00:00:00Z"))
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

    def test_unimplemented_axes_do_not_claim_a_measured_zero_or_source(self):
        for hook in (FIXTURES / "b1_hook_boundaries.jsonl",
                     Path(self.tmp.name) / "absent-hooks.jsonl"):
            rows = compute_metrics(100, 200, self.crew_root, hook, bots=["alpha"])
            for metric in ("M7", "M8"):
                row = next(row for row in rows if row["bot"] == "alpha"
                           and row["metric"] == metric)
                self.assertIsNone(row["value"])
                self.assertIsNone(row["n"])
                self.assertIsNone(row["source"])
                self.assertTrue(row["reason"].startswith("not_measured:"))
