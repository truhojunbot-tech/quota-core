"""Coverage for the SEV-0 §12 feedback-loop generator.

Every test here runs against a synthetic SQLite database built in a temporary
directory.  None of them touch the live agent_crew ``tasks.db``: the generator's
whole claim is that it only ever reads, and a test that proves that against the
real file would still be a test that opened the real file.

What is pinned:

* the pass-1 branch (canary window open) reports ``PENDING``, not ``UNKNOWN``,
  and stores a BEFORE snapshot;
* the pass-2 branch (cascade terminal, canary receipt resolved) reloads that
  snapshot, emits a diff, and cites the canary row by task id;
* the connections opened on the source database are read-only;
* the only files written are the two evidence paths.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "tokenomics_feedback_record", REPO / "scripts" / "tokenomics_feedback_record.py"
)
fbr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fbr)

PINNED = "pinned-implement-task"
REVIEW = f"review-{PINNED}-r1"


def _seed(path: Path, *, cascade_terminal: bool) -> None:
    """A three-task lineage: the pin, its review, and one unrelated fleet task.

    ``cascade_terminal`` is the only axis: with it False the review is still
    running and no canary receipt has resolved (pass 1); with it True the review
    has completed and its canary evaluation is recorded (pass 2).
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, task_type TEXT, status TEXT, verdict TEXT,
            context TEXT, error_info TEXT, findings TEXT, created_at REAL
        );
        CREATE TABLE task_attribution (
            task_id TEXT PRIMARY KEY, created_at REAL, outcome TEXT,
            provider TEXT, model TEXT, uncached_input_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            output_tokens INTEGER, reasoning_tokens INTEGER
        );
        CREATE TABLE tokenomics_shadow_receipts (
            task_id TEXT PRIMARY KEY, canary_decision_source TEXT,
            canary_recommendation_json TEXT, canary_applied INTEGER,
            canary_counterfactual TEXT, canary_reason TEXT,
            canary_cea_receipt_id TEXT, canary_resolved_at REAL
        );
        CREATE TABLE authorization_receipts (
            row_id INTEGER PRIMARY KEY, receipt_id TEXT, task_id TEXT,
            decision TEXT, reason TEXT, state TEXT
        );
        """
    )
    review_status = "completed" if cascade_terminal else "in_progress"
    review_verdict = "request_changes" if cascade_terminal else None
    conn.executemany(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
        [
            (PINNED, "implement", "completed", None, json.dumps({}), "{}", "[]", 100.0),
            (REVIEW, "review", review_status, review_verdict,
             json.dumps({"prev_task_id": PINNED, "reviewed_sha": "886dda3", "fix_round": 1}),
             "{}", "[]", 200.0),
            ("unrelated-fleet-task", "implement", "completed", None, "{}", "{}", "[]", 50.0),
        ],
    )
    conn.executemany(
        "INSERT INTO task_attribution VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (PINNED, 100.0, "success", "codex", "gpt-5", 10, 20, 5, 7, 0),
            (REVIEW, 200.0, "success" if cascade_terminal else None,
             "claude", "claude-opus-5", 11, 21, 6, 8, 1),
            ("unrelated-fleet-task", 50.0, "success", "codex", "gpt-5", 1, 2, 3, 4, 0),
        ],
    )
    conn.execute(
        "INSERT INTO authorization_receipts VALUES (1,'receipt-abc',?,'BLOCK',?,'enqueue')",
        (PINNED, json.dumps("DECISION_BLOCK: receipt decision is BLOCK")),
    )
    if cascade_terminal:
        conn.execute(
            "INSERT INTO tokenomics_shadow_receipts VALUES (?,?,?,?,?,?,?,?)",
            (REVIEW, "quota_core_contract",
             json.dumps({"kind": fbr.KIND, "applied": False,
                         "reason": "standing_verdict_is_approve",
                         "standing_review_task_id": "some-standing-review",
                         "reviewed_sha": "886dda3", "target": "branch:sev0/cea-lineage"}),
             0, None, "standing_verdict_is_approve", "canary-receipt-xyz", 1790206397.0),
        )
    conn.commit()
    conn.close()


class FeedbackRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "evidence" / "sev0"
        self.addCleanup(self._tmp.cleanup)

    def _db(self, *, cascade_terminal: bool) -> Path:
        path = self.tmp / f"tasks-{cascade_terminal}.db"
        _seed(path, cascade_terminal=cascade_terminal)
        return path

    def _run(self, db: Path) -> dict:
        # the generator prints a one-line summary; keep it out of the suite output
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, fbr.main([PINNED, "--db", str(db), "--out-dir", str(self.out)]))
        return json.loads((self.out / f"{fbr.STEM}.json").read_text())

    # -- pass 1 ---------------------------------------------------------

    def test_pass_1_reports_pending_and_stores_a_before_snapshot(self) -> None:
        payload = self._run(self._db(cascade_terminal=False))
        self.assertEqual(1, payload["pass"])
        self.assertEqual(fbr.PENDING, payload["feedback_record"]["actual_action"]["state"])
        self.assertEqual([REVIEW], payload["feedback_record"]["actual_action"]["open_lineage_tasks"])
        decision = payload["decision_record"]
        self.assertIsNone(decision["after"])
        self.assertIsNone(decision["diff"])
        self.assertEqual("NOT_ESTABLISHED_YET", decision["consumption_claim"]["status"])
        # PENDING, never UNKNOWN: an outcome that can still arrive is not unknowable.
        self.assertIn(PINNED, decision["before"]["per_task"])
        self.assertNotIn(fbr.UNKNOWN, json.dumps(payload["feedback_record"]["actual_action"]))

    def test_pass_1_lineage_is_the_pin_and_its_cascade_only(self) -> None:
        payload = self._run(self._db(cascade_terminal=False))
        self.assertEqual([PINNED, REVIEW], sorted(payload["lineage"]["task_ids"]))
        self.assertNotIn("unrelated-fleet-task", payload["lineage"]["task_ids"])

    # -- pass 2 ---------------------------------------------------------

    def test_pass_2_reloads_before_emits_diff_and_cites_the_canary_row(self) -> None:
        self._run(self._db(cascade_terminal=False))
        before = json.loads(
            (self.out / f"{fbr.STEM}.json").read_text())["decision_record"]["before"]

        payload = self._run(self._db(cascade_terminal=True))
        self.assertEqual(2, payload["pass"])
        decision = payload["decision_record"]
        self.assertEqual(before, decision["before"], "pass 2 must reload, not recapture, BEFORE")
        self.assertIsNotNone(decision["after"])
        self.assertTrue(decision["diff"]["changed"])
        # the review's outcome arriving is a field the producer DOES read: it was
        # already in the BEFORE contract with a null outcome, and the cascade
        # completing is what moved it.
        self.assertEqual(
            {"before": None, "after": "success"},
            decision["diff"]["per_task"][REVIEW]["outcome"],
        )

        claim = decision["consumption_claim"]
        self.assertEqual("SEE_DIFF", claim["status"])
        self.assertEqual("no_producer", claim["mechanical_link"])
        cited = claim["outcome_rows_cited"]
        self.assertEqual([REVIEW], [row["task_id"] for row in cited])
        self.assertEqual("tokenomics_shadow_receipts", cited[0]["table"])
        self.assertFalse(cited[0]["canary_applied"])
        self.assertEqual("standing_verdict_is_approve", cited[0]["canary_reason"])

    def test_pass_2_records_the_outcome_as_observed(self) -> None:
        self._run(self._db(cascade_terminal=False))
        payload = self._run(self._db(cascade_terminal=True))
        record = payload["feedback_record"]
        self.assertEqual("OBSERVED", record["actual_action"]["state"])
        self.assertEqual(1, record["actual_action"]["canary_evaluations_recorded"])
        self.assertEqual(0, record["actual_action"]["suppressions_applied"])
        self.assertEqual([], record["actual_action"]["open_lineage_tasks"])
        self.assertEqual({REVIEW: "request_changes"}, record["quality_result"]["lineage_verdicts"])

    # -- the corrected read-path claim (Codex review of 513d7cb) --------

    def test_read_path_names_tasks_verdict_as_read_and_canary_as_unread(self) -> None:
        self._run(self._db(cascade_terminal=False))
        payload = self._run(self._db(cascade_terminal=True))
        read_path = payload["decision_record"]["producer_read_path"]
        self.assertTrue(read_path["tasks"]["read"])
        self.assertIn("verdict", read_path["tasks"]["columns"])
        self.assertTrue(read_path["task_attribution"]["read"])
        self.assertFalse(read_path["tokenomics_shadow_receipts"]["read"])

        blob = json.dumps(payload)
        self.assertNotIn("quota-core reads task_attribution only", blob)
        fields = {item["field"] for item in payload["feedback_record"]["unknown_fields"]}
        self.assertIn("required_context_recalled", fields)
        self.assertNotIn("independent_review_correct", fields)

    def test_no_producer_is_scoped_to_the_round_inputs(self) -> None:
        self._run(self._db(cascade_terminal=False))
        payload = self._run(self._db(cascade_terminal=True))
        explanation = payload["decision_record"]["consumption_claim"]["explanation"]
        for field in fbr.KIND_INPUT_FIELDS:
            self.assertIn(field, explanation)
        self.assertIn("tasks.verdict", explanation)

    # -- the two safety properties --------------------------------------

    def test_every_source_connection_is_read_only(self) -> None:
        db = self._db(cascade_terminal=True)
        opened: list[tuple[str, bool]] = []
        real_connect = sqlite3.connect

        def spy(target, *args, **kwargs):
            opened.append((str(target), bool(kwargs.get("uri"))))
            return real_connect(target, *args, **kwargs)

        with patch.object(sqlite3, "connect", spy):
            self._run(db)

        source_opens = [t for t in opened if db.name in t[0]]
        self.assertTrue(source_opens, "the generator must actually read the source db")
        for target, uri in source_opens:
            self.assertTrue(uri, f"{target} was opened without uri=True")
            self.assertIn("mode=ro", target)

    def test_the_source_database_is_left_byte_identical(self) -> None:
        db = self._db(cascade_terminal=True)
        before = db.read_bytes()
        self._run(db)
        self.assertEqual(before, db.read_bytes())

    def test_output_is_confined_to_the_two_evidence_paths(self) -> None:
        db = self._db(cascade_terminal=True)
        before = {p for p in self.tmp.rglob("*") if p.is_file()}
        self._run(db)
        after = {p for p in self.tmp.rglob("*") if p.is_file()}
        self.assertEqual(
            {self.out / f"{fbr.STEM}.json", self.out / f"{fbr.STEM}.md"},
            after - before,
        )

    def test_a_missing_pinned_task_fails_loudly(self) -> None:
        with self.assertRaises(SystemExit):
            fbr.main(["no-such-task", "--db", str(self._db(cascade_terminal=True)),
                      "--out-dir", str(self.out)])


if __name__ == "__main__":
    unittest.main()
