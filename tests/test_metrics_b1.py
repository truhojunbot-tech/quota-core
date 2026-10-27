"""B1.3 def_version 1 metrics, using source-shaped read-only fixtures."""

from __future__ import annotations

import json
import hashlib
import io
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from contextlib import redirect_stdout
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
        self.assertEqual(by_metric["M1"]["bot_session_reason"],
                         "bot_session_sources_not_supplied")
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
        for metric in ("M3a", "M3b", "M3c"):
            self.assertIsNone(by_metric[metric]["value"])
            self.assertTrue(by_metric[metric]["reason"])
        self.assertEqual(by_metric["M7"]["n"], 0)
        self.assertIsNone(by_metric["M7"]["value"])
        self.assertTrue(by_metric["M7"]["reason"])
        self.assertIsNone(by_metric["M8"]["value"])
        self.assertTrue(by_metric["M8"]["reason"])

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

    def test_served_item_join_uses_memory_rows_and_detects_violations(self):
        memory_db = Path(self.tmp.name) / "memory.db"
        with sqlite3.connect(memory_db) as conn:
            conn.execute("CREATE TABLE adr001_memory (layer TEXT, key TEXT, value TEXT, scope TEXT, "
                         "version INTEGER, created REAL, superseded_by TEXT, invalidated_at REAL)")
            conn.execute("INSERT INTO adr001_memory VALUES (?,?,?,?,?,?,?,?)",
                         ("episodic", "served-1", "safe text", '{"project":"alpha"}', 1, 100, None, None))
            conn.execute("INSERT INTO adr001_memory VALUES (?,?,?,?,?,?,?,?)",
                         ("episodic", "served-2", "sk-ant-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                          '{"project":"other"}', 1, 100, "newer", None))
        events = self.crew_root / "alpha" / "context_events.jsonl"
        events.write_text(json.dumps({"event_type": "context_pack_built", "ts": 150,
                                      "project": "alpha", "result_ids": ["served-1", "served-2"],
                                      "selected_count": 2, "shadow": False,
                                      "superseded": [False, True], "dropped_cross_project": 0}) + "\n")
        rows = compute_metrics(100, 200, self.crew_root,
                               FIXTURES / "b1_hook_boundaries.jsonl", bots=["alpha"],
                               memory_db=memory_db)
        m7 = next(row for row in rows if row["metric"] == "M7")
        self.assertEqual(m7["n"], 2)
        self.assertEqual(m7["value"], {"superseded": 1, "cross_project": 1,
                                       "secret_matches": 1, "total": 3})

    def test_malformed_and_incomplete_served_ids_are_unknown_not_fatal(self):
        memory_db = Path(self.tmp.name) / "memory.db"
        with sqlite3.connect(memory_db) as conn:
            conn.execute("CREATE TABLE adr001_memory (key TEXT, value TEXT, scope TEXT, "
                         "superseded_by TEXT, invalidated_at REAL)")
            conn.execute("INSERT INTO adr001_memory VALUES (?,?,?,?,?)",
                         ("served-1", "safe text", '{"project":"alpha"}', None, None))
        events = self.crew_root / "alpha" / "context_events.jsonl"
        events.write_text("\n".join(json.dumps({"event_type": "context_pack_built",
            "ts": 150, "project": "alpha", "result_ids": ids,
            "selected_count": count, "shadow": False}) for ids, count in
            ((5, 1), (["served-1"], 2))) + "\n")
        rows = compute_metrics(100, 200, self.crew_root,
                               FIXTURES / "b1_hook_boundaries.jsonl", bots=["alpha"],
                               memory_db=memory_db)
        m7 = next(row for row in rows if row["metric"] == "M7")
        self.assertIsNone(m7["value"])
        self.assertEqual(m7["n"], 0)
        self.assertEqual(m7["join_coverage"], 0.0)
        self.assertEqual(m7["reason"], "unresolved_served_item_receipts:2")

    def test_shadow_retrieval_is_not_counted_as_served(self):
        events = self.crew_root / "alpha" / "context_events.jsonl"
        events.write_text(json.dumps({"event_type": "shadow_memory_retrieval", "ts": 150,
                                      "project": "alpha", "result_ids": ["sensitive"],
                                      "superseded": [True]}) + "\n")
        rows = compute_metrics(100, 200, self.crew_root,
                               FIXTURES / "b1_hook_boundaries.jsonl", bots=["alpha"])
        m7 = next(row for row in rows if row["metric"] == "M7")
        self.assertEqual(m7["n"], 0)
        self.assertIsNone(m7["value"])
        self.assertEqual(m7["reason"], "no_nonshadow_served_item_receipts_in_window")

    def test_m1_generation_join_uses_renewal_project_and_time_window(self):
        projects = Path(self.tmp.name) / "transcripts"
        folder = projects / "-home-truhojun-alfred-instances-alpha"
        folder.mkdir(parents=True)
        transcript = folder / "session-1.jsonl"
        transcript.write_text("\n".join(json.dumps({"type": "assistant", "timestamp": ts,
            "message": {"id": request_id, "usage": {"input_tokens": 10,
            "cache_creation_input_tokens": 10, "cache_read_input_tokens": cached,
            "output_tokens": 0}}}) for ts, request_id, cached in [
                ("1970-01-01T00:02:20Z", "before", 0),
                ("1970-01-01T00:02:40Z", "after", 80),
                ("1970-01-01T00:02:41Z", "after", 80)]) + "\n")
        lifecycle = Path(self.tmp.name) / "compact.jsonl"
        lifecycle.write_text(json.dumps({"runtime": "alfred_watchdog", "event_type": "success",
            "action": "RENEW", "session": "alpha", "project": "alpha",
            "timestamp": "1970-01-01T00:02:30Z"}) + "\n")
        rows = compute_metrics(100, 200, self.crew_root,
                               FIXTURES / "b1_hook_boundaries.jsonl", bots=["alpha"],
                               transcript_root=projects, lifecycle_log=lifecycle)
        m1 = next(row for row in rows if row["metric"] == "M1")
        self.assertEqual(m1["bot_session_value"], 0.8)
        self.assertEqual(m1["bot_session_n"], 1)

    def test_m1_uses_transcript_compact_boundary_without_lifecycle_success(self):
        projects = Path(self.tmp.name) / "transcripts"
        folder = projects / "-home-truhojun-alfred-instances-alpha"
        folder.mkdir(parents=True)
        (folder / "session-2.jsonl").write_text("\n".join([
            json.dumps({"subtype": "compact_boundary", "timestamp": "1970-01-01T00:02:30Z",
                        "compactMetadata": {"trigger": "auto"}}),
            json.dumps({"type": "assistant", "timestamp": "1970-01-01T00:02:40Z",
                        "message": {"id": "request-1", "usage": {"input_tokens": 10,
                        "cache_creation_input_tokens": 10, "cache_read_input_tokens": 80}}}),
        ]) + "\n")
        lifecycle = Path(self.tmp.name) / "empty.jsonl"
        lifecycle.touch()
        rows = compute_metrics(100, 200, self.crew_root,
                               FIXTURES / "b1_hook_boundaries.jsonl", bots=["alpha"],
                               transcript_root=projects, lifecycle_log=lifecycle)
        m1 = next(row for row in rows if row["metric"] == "M1")
        self.assertEqual((m1["bot_session_n"], m1["bot_session_value"]), (1, 0.8))

    def test_m1_identical_post_renew_offsets_remain_ambiguous(self):
        projects = Path(self.tmp.name) / "transcripts"
        folder = projects / "-home-truhojun-alfred-instances-alpha"
        folder.mkdir(parents=True)
        for index in (1, 2):
            (folder / f"session-{index}.jsonl").write_text(json.dumps({
                "type": "assistant", "timestamp": "1970-01-01T00:02:40Z",
                "message": {"id": f"request-{index}", "usage": {
                    "input_tokens": 10, "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 80}}}) + "\n")
        lifecycle = Path(self.tmp.name) / "compact.jsonl"
        lifecycle.write_text(json.dumps({"runtime": "alfred_watchdog",
            "event_type": "success", "action": "RENEW", "project": "alpha",
            "timestamp": "1970-01-01T00:02:30Z"}) + "\n")
        rows = compute_metrics(100, 200, self.crew_root,
                               FIXTURES / "b1_hook_boundaries.jsonl", bots=["alpha"],
                               transcript_root=projects, lifecycle_log=lifecycle)
        m1 = next(row for row in rows if row["metric"] == "M1")
        self.assertIsNone(m1["bot_session_value"])
        self.assertEqual(m1["bot_session_n"], 0)
        self.assertEqual(m1["bot_session_join_coverage"], 0.0)
        self.assertEqual(m1["bot_session_reason"],
                         "no_unambiguous_positive_transcript_generation_join")

    def test_review_followup_join_flags_observed_fix_after_approval(self):
        db = self.crew_root / "alpha" / "tasks.db"
        with sqlite3.connect(db) as conn:
            conn.execute("ALTER TABLE tokenomics_shadow_receipts ADD COLUMN shadow_recommendation_json TEXT")
            conn.execute("ALTER TABLE tokenomics_shadow_receipts ADD COLUMN actual_execution_json TEXT")
            conn.execute("ALTER TABLE tokenomics_shadow_receipts ADD COLUMN outcome TEXT")
            conn.execute("ALTER TABLE tasks ADD COLUMN task_type TEXT")
            conn.execute("ALTER TABLE tasks ADD COLUMN context TEXT")
            conn.execute("ALTER TABLE tasks ADD COLUMN verdict TEXT")
            conn.execute("ALTER TABLE tasks ADD COLUMN result_posted_at REAL")
            conn.execute("ALTER TABLE tasks ADD COLUMN receipt_id TEXT")
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                         ("impl-1", 110, "implement", json.dumps({"issue_number": 42}), None, None,
                          "cea-1"))
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                         ("review-1", 120, "review", json.dumps({"prev_task_id": "impl-1"}),
                          "approve", 125, None))
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                         ("fix-1", 130, "implement", json.dumps({"prev_task_id": "review-1"}),
                          None, None, "cea-2"))
            conn.executemany("INSERT INTO tokenomics_shadow_receipts "
                             "(task_id, economics_json, created_at, shadow_recommendation_json, "
                             "actual_execution_json, outcome) VALUES (?,?,?,?,?,?)", [
                (task_id, None, at, '{"recommended_max_review_fix_rounds":2}',
                 '{"cascade":"baseline"}', "completed")
                for task_id, at in (("impl-1", 110), ("fix-1", 130))])
        rows = compute_metrics(125 + 72 * 3600, 200 + 72 * 3600, self.crew_root,
                               FIXTURES / "b1_hook_boundaries.jsonl", bots=["alpha"])
        m8 = next(row for row in rows if row["metric"] == "M8")
        self.assertEqual((m8["n"], m8["value"]), (1, 0.0))

    def test_review_followup_positive_unknown_and_empty_are_distinct(self):
        db = self.crew_root / "alpha" / "tasks.db"
        with sqlite3.connect(db) as conn:
            conn.execute("ALTER TABLE tokenomics_shadow_receipts ADD COLUMN shadow_recommendation_json TEXT")
            conn.execute("ALTER TABLE tokenomics_shadow_receipts ADD COLUMN actual_execution_json TEXT")
            for column, kind in (("task_type", "TEXT"), ("context", "TEXT"),
                                 ("verdict", "TEXT"), ("result_posted_at", "REAL"),
                                 ("receipt_id", "TEXT"), ("pr_number", "INTEGER")):
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {kind}")
            conn.execute("INSERT INTO tasks (task_id,created_at,task_type,context,receipt_id,pr_number) "
                         "VALUES (?,?,?,?,?,?)",
                         ("impl-1", 110, "implement", '{"issue_number":42}', "cea-1", 7))
            conn.execute("INSERT INTO tasks (task_id,created_at,task_type,context,verdict,result_posted_at) "
                         "VALUES (?,?,?,?,?,?)",
                         ("review-1", 120, "review", '{"prev_task_id":"impl-1"}', "approve", 125))
            conn.execute("INSERT INTO tokenomics_shadow_receipts "
                         "(task_id,created_at,shadow_recommendation_json,actual_execution_json) "
                         "VALUES (?,?,?,?)",
                         ("impl-1", 110, '{"recommended_max_review_fix_rounds":2}',
                          '{"cascade":"baseline"}'))
        with patch.object(metrics_b1, "_github_followup", return_value=False):
            m8 = metrics_b1._review_followup_metric("alpha", db, 125 + 72 * 3600,
                                                     200 + 72 * 3600)
        self.assertEqual((m8["n"], m8["value"], m8["unknown"]), (1, 1.0, 0))
        with patch.object(metrics_b1, "_github_followup", return_value=None):
            m8 = metrics_b1._review_followup_metric("alpha", db, 125 + 72 * 3600,
                                                     200 + 72 * 3600)
        self.assertEqual((m8["n"], m8["unknown"]), (0, 1))
        self.assertIsNone(m8["value"])
        self.assertEqual(m8["reason"], "no_complete_matured_review_followup_join_in_window")
        missing = metrics_b1._review_followup_metric("alpha", db.parent / "absent.db", 100, 200)
        self.assertEqual((missing["n"], missing["value"], missing["reason"]),
                         (0, None, "no_tasks_db"))

    def test_github_pr_listing_is_memoized_per_repo(self):
        cache = {}
        def respond(args, **_kwargs):
            if args[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(args, 0, "[]", "")
            return subprocess.CompletedProcess(args, 0, "[]", "")
        with patch.object(metrics_b1.subprocess, "run", side_effect=respond) as run:
            for pr_number in (7, 8, 9):
                self.assertIsNone(metrics_b1._github_followup(
                    "alpha", 42, pr_number, 100, 200, cache=cache, search_from=100))
        listings = [call for call in run.call_args_list
                    if call.args[0][:3] == ["gh", "pr", "list"]]
        self.assertEqual(len(listings), 1)
        self.assertIn("--search", listings[0].args[0])

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

    def test_missing_join_sources_have_specific_reasons(self):
        for hook in (FIXTURES / "b1_hook_boundaries.jsonl",
                     Path(self.tmp.name) / "absent-hooks.jsonl"):
            rows = compute_metrics(100, 200, self.crew_root, hook, bots=["alpha"])
            for metric in ("M7", "M8"):
                row = next(row for row in rows if row["bot"] == "alpha"
                           and row["metric"] == metric)
                if metric == "M7":
                    self.assertEqual(row["n"], 0)
                    self.assertIsNone(row["value"])
                    self.assertIn("no_nonshadow_served_item_receipts", row["reason"])
                else:
                    self.assertEqual((row["n"], row["value"]), (None, None))
                    self.assertEqual(row["reason"],
                                     "review_followup_columns_unavailable")

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
                                   memory_db=memory_db,
                                   replay_adapter=lambda cases, snapshot: {})
            m3a = next(row for row in rows if row["bot"] == "alpha" and row["metric"] == "M3a")
            self.assertIsNone(m3a["value"])
            self.assertIsNone(m3a["n"])
            self.assertIn(str(memory_db), m3a["reason"])
            memory_db.touch()
            rows = compute_metrics(100, 200, self.crew_root,
                                   FIXTURES / "b1_hook_boundaries.jsonl",
                                   bots=["alpha"], eval_path=eval_path,
                                   memory_db=memory_db)
            m3a = next(row for row in rows if row["bot"] == "alpha" and row["metric"] == "M3a")
            self.assertIsNone(m3a["value"])
            self.assertEqual(m3a["reason"], "replay_adapter_not_supplied")
            with patch.object(metrics_b1, "_replay_frozen_eval", return_value={"M3a": (1, 1), "M3b": (0, 1)}) as replay:
                rows = compute_metrics(100, 200, self.crew_root,
                                       FIXTURES / "b1_hook_boundaries.jsonl",
                                       bots=["alpha"], eval_path=eval_path,
                                       memory_db=memory_db,
                                       replay_adapter=lambda cases, snapshot: {})
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
        observed_snapshot = []
        def replay(cases, snapshot):
            observed_snapshot.append(snapshot)
            with sqlite3.connect(snapshot) as db:
                db.execute("CREATE TABLE snapshot_only (id INTEGER)")
                db.execute("INSERT INTO snapshot_only VALUES (1)")
                self.assertEqual(db.execute("SELECT count(*) FROM snapshot_only").fetchone()[0], 1)
            return {"M3a": (0, 1), "M3b": (0, 0)}
        with patch.object(metrics_b1.sqlite3, "connect", side_effect=checked_connect):
            self.assertEqual(metrics_b1._replay_frozen_eval(cases, source, replay),
                             {"M3a": (0, 1), "M3b": (0, 0)})
        self.assertEqual(len(observed_snapshot), 1)
        self.assertNotEqual(observed_snapshot[0], source)
        self.assertEqual(len(observed_ro), 1)
        self.assertEqual(source.read_bytes(), before_bytes)
        self.assertEqual(source.stat().st_mtime_ns, before_mtime)
        with sqlite3.connect(source) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='snapshot_only'").fetchone(), None)

    def test_replay_adapter_is_required_even_with_valid_eval_and_memory_db(self):
        source = Path(self.tmp.name) / "memory-source.db"
        source.touch()
        eval_path = Path(self.tmp.name) / "eval.json"
        eval_path.write_text('{"owner_cases":[],"task_cases":[]}')
        with patch.object(metrics_b1, "EVAL_SHA256", hashlib.sha256(eval_path.read_bytes()).hexdigest()):
            rows = compute_metrics(100, 200, self.crew_root,
                                   FIXTURES / "b1_hook_boundaries.jsonl", bots=["alpha"],
                                   eval_path=eval_path, memory_db=source)
        self.assertEqual(next(row for row in rows if row["metric"] == "M3a")["reason"],
                         "replay_adapter_not_supplied")

    def test_bad_replay_adapters_degrade_only_m3_with_specific_reason(self):
        eval_path = Path(self.tmp.name) / "eval.json"
        eval_path.write_text('{"owner_cases":[],"task_cases":[]}')
        source = Path(self.tmp.name) / "memory-source.db"
        with sqlite3.connect(source) as db:
            db.execute("CREATE TABLE sample (id INTEGER)")

        def raises(_cases, _snapshot):
            raise RuntimeError("private adapter detail")

        cases = (
            ("empty", lambda _cases, _snapshot: {}, "replay_adapter_invalid_result"),
            ("none", lambda _cases, _snapshot: None, "replay_adapter_invalid_result"),
            ("short_tuple", lambda _cases, _snapshot: {"M3a": (1,), "M3b": (0, 1)},
             "replay_adapter_invalid_result"),
            ("missing_metric", lambda _cases, _snapshot: {"M3a": (1, 1)},
             "replay_adapter_invalid_result"),
            ("bad_counts", lambda _cases, _snapshot: {"M3a": (2, 1), "M3b": (0, 1)},
             "replay_adapter_invalid_result"),
            ("raises", raises, "replay_adapter_error:RuntimeError"),
        )
        with patch.object(metrics_b1, "EVAL_SHA256", hashlib.sha256(eval_path.read_bytes()).hexdigest()):
            for name, adapter, reason in cases:
                with self.subTest(name=name):
                    rows = compute_metrics(100, 200, self.crew_root,
                                           FIXTURES / "b1_hook_boundaries.jsonl",
                                           bots=["alpha"], eval_path=eval_path,
                                           memory_db=source, replay_adapter=adapter)
                    self.assertEqual({row["metric"] for row in rows}, set(metrics_b1.METRICS))
                    for metric in ("M3a", "M3b"):
                        row = next(row for row in rows if row["metric"] == metric)
                        self.assertIsNone(row["value"])
                        self.assertIsNone(row["n"])
                        self.assertEqual(row["reason"], reason)
                        self.assertNotIn("private adapter detail", row["reason"])

    def test_cli_reports_adapter_not_supplied_for_sha_valid_eval(self):
        eval_path = Path(self.tmp.name) / "eval.json"
        eval_path.write_text('{"owner_cases":[],"task_cases":[]}')
        source = Path(self.tmp.name) / "memory-source.db"
        with sqlite3.connect(source) as db:
            db.execute("CREATE TABLE sample (id INTEGER)")
        output = io.StringIO()
        with patch.object(metrics_b1, "EVAL_SHA256", hashlib.sha256(eval_path.read_bytes()).hexdigest()), redirect_stdout(output):
            self.assertEqual(metrics_b1.main([
                "--since", "1970-01-01T00:01:40+00:00", "--until", "1970-01-01T00:03:20+00:00", "--crew-root", str(self.crew_root),
                "--hook-log", str(FIXTURES / "b1_hook_boundaries.jsonl"),
                "--bot", "alpha", "--eval-path", str(eval_path), "--memory-db", str(source),
            ]), 0)
        rows = json.loads(output.getvalue())
        for metric in ("M3a", "M3b"):
            row = next(row for row in rows if row["metric"] == metric)
            self.assertIsNone(row["value"])
            self.assertEqual(row["reason"], "replay_adapter_not_supplied")
