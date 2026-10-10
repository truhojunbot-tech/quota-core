"""B1.3 def_version 1 metrics, using source-shaped read-only fixtures."""

from __future__ import annotations

import json
import hashlib
import importlib
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch

from quota_core.context_economics import metrics_b1
from quota_core.context_economics.metrics_b1 import compute_metrics, format_table, _time


FIXTURES = Path(__file__).parent / "fixtures" / "context_economics"


@contextmanager
def isolated_agent_crew_modules():
    """Exercise imports from a fake checkout without leaking into other tests."""
    saved = {name: module for name, module in sys.modules.items()
             if name == "agent_crew" or name.startswith("agent_crew.")}
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name == "agent_crew" or name.startswith("agent_crew."):
                del sys.modules[name]
        sys.modules.update(saved)


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
                             "expected": [f"owner-key-{i}", "other-expected-key"]
                             if i == 0 else [f"owner-key-{i}"]} for i in range(17)],
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
                if query == "task 1":
                    return {"head": [], "middle": [{"key": "x", "value": {"text": "PR #999"}}]}
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

    def test_replay_reads_source_ro_and_storage_writes_only_to_snapshot(self):
        source = Path(self.tmp.name) / "memory-source.db"
        with sqlite3.connect(source) as db:
            db.execute("CREATE TABLE source_only (id INTEGER)")
            db.execute("INSERT INTO source_only VALUES (1)")
        before_bytes, before_mtime = source.read_bytes(), source.stat().st_mtime_ns
        package = Path(self.tmp.name) / "fake-crew" / "agent_crew"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "memory_runtime.py").write_text(
            "class MemoryScope:\n"
            "    def __init__(self, **fields): self.__dict__.update(fields)\n")
        (package / "memory_hybrid.py").write_text(
            "import sqlite3\n"
            "observed_path = None\n"
            "snapshot_write_seen = False\n"
            "class HybridMemoryStorage:\n"
            "    def __init__(self, path):\n"
            "        global observed_path, snapshot_write_seen\n"
            "        observed_path = path\n"
            "        with sqlite3.connect(path) as db:\n"
            "            db.execute('CREATE TABLE snapshot_only (id INTEGER)')\n"
            "            db.execute('INSERT INTO snapshot_only VALUES (1)')\n"
            "            snapshot_write_seen = db.execute('SELECT count(*) FROM snapshot_only').fetchone()[0] == 1\n"
            "    def retrieve_ranked(self, scope, query, role, k, byte_budget):\n"
            "        return {'head': [], 'middle': []}\n")
        real_connect = sqlite3.connect
        observed_ro = []

        def checked_connect(database, *args, **kwargs):
            if str(database).startswith(source.resolve().as_uri()):
                observed_ro.append((str(database), kwargs.get("uri")))
                self.assertIn("?mode=ro", str(database))
                self.assertTrue(kwargs.get("uri"))
            return real_connect(database, *args, **kwargs)

        cases = {"owner_cases": [{"project": "alpha", "query": "owner",
                                   "expected": ["key"]}], "task_cases": []}
        with isolated_agent_crew_modules(), patch.dict(os.environ, {
                "LEMMALOG_AGENT_CREW_SRC": str(package.parent)}), patch.object(
                metrics_b1.sqlite3, "connect", side_effect=checked_connect):
            self.assertEqual(metrics_b1._replay_frozen_eval(cases, source),
                             {"M3a": (0, 1), "M3b": (0, 0)})
            hybrid = sys.modules["agent_crew.memory_hybrid"]
            self.assertNotEqual(Path(hybrid.observed_path), source)
            self.assertTrue(hybrid.snapshot_write_seen)
        self.assertEqual(len(observed_ro), 1)
        self.assertEqual(source.read_bytes(), before_bytes)
        self.assertEqual(source.stat().st_mtime_ns, before_mtime)
        with sqlite3.connect(source) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='snapshot_only'").fetchone(), None)

    def test_cached_agent_crew_module_from_other_checkout_is_refused(self):
        source = Path(self.tmp.name) / "memory-source.db"
        source.touch()
        first = Path(self.tmp.name) / "first" / "agent_crew"
        second = Path(self.tmp.name) / "second" / "agent_crew"
        for package in (first, second):
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            (package / "memory_hybrid.py").write_text("class HybridMemoryStorage: pass\n")
            (package / "memory_runtime.py").write_text("class MemoryScope: pass\n")
        with isolated_agent_crew_modules():
            sys.path.insert(0, str(first.parent))
            try:
                importlib.import_module("agent_crew.memory_hybrid")
            finally:
                sys.path.remove(str(first.parent))
            with patch.dict(os.environ, {"LEMMALOG_AGENT_CREW_SRC": str(second.parent)}):
                with self.assertRaisesRegex(ImportError, "agent_crew_src_mismatch"):
                    metrics_b1._replay_frozen_eval({"owner_cases": [], "task_cases": []}, source)
            self.assertFalse("agent_crew.memory_runtime" in sys.modules)
