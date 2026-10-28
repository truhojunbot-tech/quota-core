"""Hourly B1.3 snapshots recompute recent hours so late receipts settle (#99)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from unittest.mock import patch

from quota_core.context_economics import metrics_b1, metrics_b1_hourly
from quota_core.context_economics.metrics_b1 import compute_metrics

H = datetime(2026, 10, 28, 22, tzinfo=timezone.utc)
COMPONENTS = {"uncached_input_tokens": 10, "cache_write_tokens": 10, "cache_read_tokens": 80}


def _clock(start: datetime):
    ticks = count()
    return lambda: start + timedelta(seconds=next(ticks))


class HourlyRecomputeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.crew_root, self.out = root / "crews", root / "out"
        self.hook_log = root / "hooks.jsonl"
        self.hook_log.write_text("", encoding="utf-8")
        self.db = self.crew_root / "alpha" / "tasks.db"
        self.db.parent.mkdir(parents=True)
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE tokenomics_shadow_receipts "
                         "(task_id TEXT, economics_json TEXT, created_at REAL)")
            conn.execute("CREATE TABLE tasks (task_id TEXT, created_at REAL)")
            conn.execute("INSERT INTO tokenomics_shadow_receipts VALUES (?,?,?)",
                         ("early", json.dumps(COMPONENTS), (H + timedelta(minutes=5)).timestamp()))
            # Created at 22:55, economics_json not yet written at the 23:07 run.
            conn.execute("INSERT INTO tokenomics_shadow_receipts VALUES (?,?,?)",
                         ("late", None, (H + timedelta(minutes=55)).timestamp()))
        # M8 would otherwise shell out to gh for each window.
        patcher = patch.object(metrics_b1, "_review_followup_metric",
                               side_effect=lambda bot, *_: metrics_b1._row(bot, "M8", None, 0, None,
                                                                           "stubbed"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, now: datetime, clock_start: datetime) -> int:
        return metrics_b1_hourly.run(now, self.out, self.crew_root, self.hook_log,
                                     clock=_clock(clock_start))

    def _m1(self, stem: str) -> dict:
        rows = json.loads((self.out / f"{stem}.json").read_text(encoding="utf-8"))
        return next(row for row in rows if row["bot"] == "alpha" and row["metric"] == "M1")

    def test_late_receipt_is_counted_on_next_run_and_hour_file_rewritten(self):
        first = H + timedelta(hours=1, minutes=7)
        self.assertEqual(self._run(first, first), 0)
        before = self._m1("20261028T22Z")
        self.assertEqual((before["n"], before["value"]), (1, 0.8))

        late = dict(COMPONENTS, cache_read_tokens=20, uncached_input_tokens=70)
        with sqlite3.connect(self.db) as conn:  # economics_json completes at 23:25
            conn.execute("UPDATE tokenomics_shadow_receipts SET economics_json=? "
                         "WHERE task_id='late'", (json.dumps(late),))

        second = first + timedelta(hours=1)
        self.assertEqual(self._run(second, second), 0)
        after = self._m1("20261028T22Z")
        self.assertEqual((after["n"], after["value"]), (2, 100 / 200))
        self.assertGreater(after["computed_at"], before["computed_at"])
        table = (self.out / "20261028T22Z.txt").read_text(encoding="utf-8")
        self.assertTrue(table.startswith(f"computed_at: {after['computed_at']}\n"))
        self.assertTrue((self.out / "20261028T23Z.json").is_file())

    def test_each_run_writes_current_hour_and_recomputes_previous_three(self):
        self._run(H + timedelta(hours=1, minutes=7), H + timedelta(hours=1, minutes=7))
        self.assertEqual(sorted(path.name for path in self.out.glob("*.json")),
                         [f"20261028T{hour}Z.json" for hour in (19, 20, 21, 22)])
        self.assertEqual(list(self.out.glob(".*")), [])  # no staged temp files left

    def test_recompute_is_idempotent_except_computed_at(self):
        now = H + timedelta(hours=1, minutes=7)
        snapshots = []
        for clock_start in (now, now + timedelta(minutes=30)):
            self._run(now, clock_start)
            snapshots.append({path.name: path.read_text(encoding="utf-8")
                              for path in sorted(self.out.iterdir())})
        first, second = snapshots
        self.assertEqual(first.keys(), second.keys())
        changed = 0
        for name in first:
            stamp_a = json.loads(first[name])[0]["computed_at"] if name.endswith(".json") else \
                first[name].splitlines()[0].removeprefix("computed_at: ")
            stamp_b = json.loads(second[name])[0]["computed_at"] if name.endswith(".json") else \
                second[name].splitlines()[0].removeprefix("computed_at: ")
            self.assertNotEqual(stamp_a, stamp_b)
            changed += first[name] != second[name]
            self.assertEqual(first[name].replace(stamp_a, "<T>"), second[name].replace(stamp_b, "<T>"))
        self.assertEqual(changed, len(first))

    def test_failed_recompute_keeps_existing_snapshot(self):
        now = H + timedelta(hours=1, minutes=7)
        self._run(now, now)
        good = (self.out / "20261028T21Z.json").read_bytes()
        stamp = self._m1("20261028T22Z")["computed_at"]
        real = metrics_b1_hourly.compute_metrics

        def flaky(since, until, *args, **kwargs):
            if since == (H - timedelta(hours=1)).timestamp():
                raise OSError("disk hiccup")
            return real(since, until, *args, **kwargs)

        with patch.object(metrics_b1_hourly, "compute_metrics", side_effect=flaky):
            self.assertEqual(self._run(now, now + timedelta(hours=1)), 1)
        self.assertEqual((self.out / "20261028T21Z.json").read_bytes(), good)
        self.assertGreater(self._m1("20261028T22Z")["computed_at"], stamp)

    def _snapshot(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in sorted(self.out.iterdir())}

    def test_failed_table_publish_keeps_previous_consistent_pair(self):
        now = H + timedelta(hours=1, minutes=7)
        self._run(now, now)
        before = self._snapshot()
        real_replace = metrics_b1_hourly.os.replace

        def table_fails(src, dst):
            if str(dst).endswith("Z.txt"):
                raise OSError("injected table replace failure")
            return real_replace(src, dst)

        with patch.object(metrics_b1_hourly.os, "replace", side_effect=table_fails):
            self.assertEqual(self._run(now, now + timedelta(hours=1)), 1)
        self.assertEqual(self._snapshot(), before)  # no new JSON, no staged leftovers

    def test_failed_table_staging_keeps_previous_consistent_pair(self):
        now = H + timedelta(hours=1, minutes=7)
        self._run(now, now)
        before = self._snapshot()
        real_stage = metrics_b1_hourly._stage

        def table_fails(path, body):
            if path.suffix == ".txt":
                raise OSError("injected table write failure")
            return real_stage(path, body)

        with patch.object(metrics_b1_hourly, "_stage", side_effect=table_fails):
            self.assertEqual(self._run(now, now + timedelta(hours=1)), 1)
        self.assertEqual(self._snapshot(), before)

    def test_first_publish_failure_leaves_no_half_pair(self):
        now = H + timedelta(hours=1, minutes=7)
        real_replace = metrics_b1_hourly.os.replace

        def table_fails(src, dst):
            if str(dst).endswith("Z.txt"):
                raise OSError("injected table replace failure")
            return real_replace(src, dst)

        with patch.object(metrics_b1_hourly.os, "replace", side_effect=table_fails):
            self.assertEqual(self._run(now, now), 1)
        self.assertEqual(self._snapshot(), {})

    def test_m1_reason_is_no_tasks_db_when_bot_has_no_db(self):
        rows = compute_metrics(H.timestamp(), (H + timedelta(hours=1)).timestamp(),
                               self.crew_root, self.hook_log, bots=["ghost", "alpha"])
        m1 = {row["bot"]: row for row in rows if row["metric"] == "M1"}
        self.assertEqual(m1["ghost"]["reason"], "no_tasks_db")
        with sqlite3.connect(self.db) as conn:
            conn.execute("DELETE FROM tokenomics_shadow_receipts")
        rows = compute_metrics(H.timestamp(), (H + timedelta(hours=1)).timestamp(),
                               self.crew_root, self.hook_log, bots=["alpha"])
        self.assertEqual(rows[0]["reason"], "no_complete_positive_economics_receipts_in_window")


if __name__ == "__main__":
    unittest.main()
