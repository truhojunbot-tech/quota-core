"""B1.3 def_version 1 metrics, using source-shaped read-only fixtures."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from quota_core.context_economics import metrics_b1
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

    def test_frozen_eval_replay_scores_head_and_first_five_middle_only(self):
        cases = {
            "owner_cases": [{"project": "alpha", "query": f"owner {i}",
                             "expected": [f"owner-key-{i}"]} for i in range(17)],
            "task_cases": [{"project": "alpha", "query": f"task {i}",
                            "expected": [{"ref": f"PR #{i}"}]} for i in range(24)],
        }
        class FakeStorage:
            def __init__(self):
                self.calls = []

            def retrieve_ranked(self, scope, query, role, k, byte_budget):
                self.calls.append((scope.fleet, scope.project, role, k, byte_budget))
                if query == "owner 0":
                    return {"head": [{"key": "owner-key-0"}], "middle": []}
                if query == "owner 1":
                    return {"head": [], "middle": [{"key": f"distractor-{i}"}
                                                    for i in range(5)]
                            + [{"key": "owner-key-1"}]}
                if query == "task 0":
                    return {"head": [], "middle": [{"key": "x", "value": {"text": "PR #0"}}]}
                return {"head": [], "middle": []}

        storage = FakeStorage()
        class Scope:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        scores = metrics_b1._score_eval_cases(cases, storage, Scope)
        self.assertEqual(scores, {"M3a": (1, 17), "M3b": (1, 24)})
        self.assertEqual(len(storage.calls), 41)
        self.assertTrue(all(call == ("fleet", "alpha", "implementer", 20, 16000)
                            for call in storage.calls))

    def test_replay_guards_and_fake_storage_flow_through_metric_rows(self):
        eval_path = Path(self.tmp.name) / "eval.json"
        payload = {"owner_cases": [{"project": "alpha", "query": "owner",
                                    "expected": ["key-a"]}],
                   "task_cases": [{"project": "alpha", "query": "task",
                                   "expected": [{"ref": "issue #42"}]}]}
        eval_path.write_text(json.dumps(payload))
        frozen_sha = hashlib.sha256(eval_path.read_bytes()).hexdigest()
        memory_db = Path(self.tmp.name) / "memory.db"
        with patch.object(metrics_b1, "EVAL_SHA256", frozen_sha):
            rows = compute_metrics(100, 200, self.crew_root,
                                   FIXTURES / "b1_hook_boundaries.jsonl",
                                   bots=["alpha"], eval_path=eval_path,
                                   memory_db=memory_db)
            m3a = next(row for row in rows if row["bot"] == "alpha" and row["metric"] == "M3a")
            self.assertIsNone(m3a["value"])
            self.assertIsNone(m3a["n"])
            self.assertIn(str(memory_db), m3a["reason"])
            memory_db.touch()
            with patch.dict(os.environ, {"LEMMALOG_AGENT_CREW_SRC": str(Path(self.tmp.name) / "missing-src")}):
                rows = compute_metrics(100, 200, self.crew_root,
                                       FIXTURES / "b1_hook_boundaries.jsonl",
                                       bots=["alpha"], eval_path=eval_path,
                                       memory_db=memory_db)
            m3a = next(row for row in rows if row["bot"] == "alpha" and row["metric"] == "M3a")
            self.assertIsNone(m3a["value"])
            self.assertIn("LEMMALOG_AGENT_CREW_SRC", m3a["reason"])
            with patch.object(metrics_b1, "_replay_frozen_eval", return_value={"M3a": (1, 1), "M3b": (0, 1)}) as replay:
                rows = compute_metrics(100, 200, self.crew_root,
                                       FIXTURES / "b1_hook_boundaries.jsonl",
                                       bots=["alpha"], eval_path=eval_path,
                                       memory_db=memory_db)
            self.assertEqual(replay.call_count, 1)
            by_metric = {row["metric"]: row for row in rows if row["bot"] == "alpha"}
            self.assertEqual((by_metric["M3a"]["value"], by_metric["M3a"]["n"]), (1.0, 1))
            self.assertEqual((by_metric["M3b"]["value"], by_metric["M3b"]["n"]), (0.0, 1))
