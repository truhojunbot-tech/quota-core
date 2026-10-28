"""quota-core#80 accounting correctness: unresolved outcomes and unset timestamps.

1. An explicit unresolved outcome (``timed_out``/``needs_human``/``blocked``)
   used to normalize to ``"unknown"``, which ``analytics._not_succeeded``
   counts as a failure -- overstating raw failure rates with work that never
   reached a terminal result.
2. A producer writes ``0`` for an unset timestamp; ``parse_flexible_timestamp``
   used to read that as 1970-01-01, fabricating durations.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from quota_core.context_economics import (
    TaskEconomicsRecord,
    normalize_outcome,
    parse_flexible_timestamp,
    stratified_failure_rates,
)
from quota_core.context_economics.compact_analysis import _window_stats
from quota_core.context_economics.schema import attribution_from_dict, validate_attribution_dict
from quota_core.context_economics.sqlite_attribution import read_task_attribution_sqlite

UNRESOLVED = ("timed_out", "needs_human", "blocked")


class UnresolvedOutcomeTest(unittest.TestCase):
    def test_unresolved_outcomes_normalize_to_not_terminal(self):
        for raw in UNRESOLVED + ("timed_out:dispatcher", "NEEDS_HUMAN"):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_outcome(raw))

    def test_terminal_outcomes_unchanged(self):
        self.assertEqual(normalize_outcome("completed"), "success")
        self.assertEqual(normalize_outcome("failed:x"), "failed")
        self.assertEqual(normalize_outcome("timeout"), "failed")
        self.assertEqual(normalize_outcome("something_else"), "unknown")

    def test_attribution_keeps_raw_outcome_for_provenance(self):
        a = attribution_from_dict({"runtime": "r", "task_id": "t", "outcome": "needs_human"})
        self.assertIsNone(a.outcome)
        self.assertEqual(a.raw_outcome, "needs_human")

    def test_sqlite_outcome_column_unresolved_not_counted_as_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "attr.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE task_attribution (task_id TEXT, outcome TEXT)")
            rows = [("ok", "completed"), ("bad", "failed")] + [(f"u{i}", o) for i, o in enumerate(UNRESOLVED)]
            conn.executemany("INSERT INTO task_attribution VALUES (?, ?)", rows)
            conn.commit()
            conn.close()
            records = read_task_attribution_sqlite(db)
        by_id = {r.task_id: r for r in records}
        for i in range(len(UNRESOLVED)):
            self.assertIsNone(by_id[f"u{i}"].outcome)
        rates = stratified_failure_rates(records)
        self.assertEqual(rates["observed_count"], 2)
        self.assertEqual(rates["raw_failure_count"], 1)
        self.assertEqual(rates["raw_failure_rate"], 0.5)


class UnsetTimestampTest(unittest.TestCase):
    def test_zero_and_negative_are_unset(self):
        for value in (0, 0.0, "0", "0.0", -1, "-5"):
            with self.subTest(value=value):
                self.assertIsNone(parse_flexible_timestamp(value))

    def test_real_timestamps_still_parse(self):
        self.assertEqual(parse_flexible_timestamp(1_700_000_000), 1_700_000_000.0)
        self.assertEqual(parse_flexible_timestamp("1700000000.5"), 1_700_000_000.5)
        self.assertIsNotNone(parse_flexible_timestamp("2026-08-21T23:56:19.497696"))

    def test_zero_timestamp_yields_no_duration(self):
        for started, completed in ((0, 1_700_000_100), (1_700_000_000, 0)):
            with self.subTest(started=started, completed=completed):
                a = attribution_from_dict(
                    {"runtime": "r", "task_id": "t", "outcome": "completed",
                     "started_at": started, "completed_at": completed}
                )
                self.assertIsNone(a.started_at if started == 0 else a.completed_at)

    def test_compact_analysis_duration_not_fabricated_from_zero(self):
        rec = TaskEconomicsRecord(
            task_id="t", runtime="r", outcome="success", context_policy="compact",
            started_at=parse_flexible_timestamp(1_700_000_000),
            completed_at=parse_flexible_timestamp(0),
        )
        self.assertIsNone(rec.duration_seconds)
        self.assertIsNone(_window_stats([rec])["avg_duration_seconds"])


class TimestampParserValidatorAgreementTest(unittest.TestCase):
    """The validator must not reject what the parser accepts as unset (#80)."""

    FIELDS = ("started_at", "completed_at", "updated_at")

    def _errors(self, key, value):
        return validate_attribution_dict({"runtime": "r", "task_id": "t", key: value})

    def test_unset_sentinel_is_valid_and_parses_to_none(self):
        for key in self.FIELDS:
            for value in (0, 0.0, "0", "0.0", -1, "-5"):
                with self.subTest(key=key, value=value):
                    self.assertEqual(self._errors(key, value), ())
                    a = attribution_from_dict({"runtime": "r", "task_id": "t", key: value})
                    self.assertIsNone(getattr(a, key))

    def test_real_timestamps_valid(self):
        for key in self.FIELDS:
            for value in (1_700_000_000, "1700000000.5", "2026-08-21T23:56:19.497696", None):
                with self.subTest(key=key, value=value):
                    self.assertEqual(self._errors(key, value), ())

    def test_malformed_timestamps_still_rejected(self):
        for key in self.FIELDS:
            for value in ("garbage", "nan", float("nan"), float("inf"), "-inf", True, False, [], {}):
                with self.subTest(key=key, value=value):
                    errors = self._errors(key, value)
                    self.assertEqual(len(errors), 1)
                    self.assertIn(key, errors[0])


if __name__ == "__main__":
    unittest.main()
