"""Progress / unchanged-state evidence from recorded review lineages (#80, alfred#51 P0-1 Step 1)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quota_core.context_economics import recommend_task_policy
from quota_core.context_economics.quality_evidence import derive_progress_evidence
from quota_core.context_economics.report_producer import _artifact_evidence, read_organic_task_records, _produce_shadow_report, build_contract

SHA_A, SHA_B = "a" * 40, "b1c2d3e" + "0" * 33


def _review(task_id, prev, sha, verdict, findings, created_at):
    ctx = {"prev_task_id": prev}
    if sha is not None:
        ctx["reviewed_sha"] = sha
    raw = findings if isinstance(findings, str) else json.dumps(findings)
    return (task_id, "review", json.dumps(ctx), verdict, raw, created_at)


def _fix(task_id, review_id, created_at):
    return (task_id, "implement", json.dumps({"prev_task_id": review_id}), None, None, created_at)


def _db(path: Path, rows, attribution: tuple[str, ...] = ()) -> Path:
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_type TEXT, context TEXT, verdict TEXT, findings TEXT, created_at REAL)")
    conn.execute("INSERT INTO tasks VALUES ('impl', 'implement', '{}', NULL, NULL, 1)")
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.execute("""CREATE TABLE task_attribution (task_id TEXT PRIMARY KEY, created_at REAL, outcome TEXT,
        provider TEXT, model TEXT, uncached_input_tokens INTEGER, cache_read_tokens INTEGER,
        cache_write_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER)""")
    for index, task_id in enumerate(attribution):
        conn.execute("INSERT INTO task_attribution VALUES (?, ?, 'success', 'claude', 'opus', 100, 0, 0, 50, 0)",
                     (task_id, 10 + index))
    conn.commit()
    conn.close()
    return path


UNCHANGED = [
    _review("rev1", "impl", SHA_A, "request_changes", ["null check missing"], 2),
    _fix("fix-rev1-r1", "rev1", 3),
    _review("rev2", "fix-rev1-r1", SHA_A, "request_changes", ["null check missing"], 4),
]
MOVED = [
    _review("rev1", "impl", SHA_A, "request_changes", ["null check missing"], 2),
    _fix("fix-rev1-r1", "rev1", 3),
    _review("rev2", "fix-rev1-r1", SHA_B, "request_changes", ["test for empty input missing"], 4),
]


class ProgressEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.dir = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def derive(self, rows, task_ids=("impl", "fix-rev1-r1")):
        return derive_progress_evidence(_db(self.dir / "t.sqlite", rows), list(task_ids))

    def rounds(self, task_id: str, rows) -> int:
        """Policy rounds for an attributed task, via the producer merge (fail-safe tier)."""
        db = _db(self.dir / "p.sqlite", rows, (task_id,))
        evidence, _ = _artifact_evidence(str(db), [task_id])
        record = read_organic_task_records([db])[task_id].record
        return recommend_task_policy(record, evidence[task_id]).recommended_max_review_fix_rounds

    def test_unchanged_sha_request_changes_is_repeated_state_and_cuts_rounds(self) -> None:
        derived = self.derive(UNCHANGED)
        for task_id in ("impl", "fix-rev1-r1"):
            self.assertIs(derived[task_id].evidence.repeated_unchanged_state, True)
            self.assertIsNone(derived[task_id].evidence.new_evidence_or_progress)
            self.assertEqual(derived[task_id].provenance["repeated_unchanged_state"],
                             f"review_sha_unchanged:{SHA_A}:rev2")
        self.assertEqual(self.rounds("impl", []), 3)  # safety_or_live baseline
        self.assertEqual(self.rounds("impl", UNCHANGED), 2)

    def test_moved_sha_with_new_findings_is_progress_and_extends_rounds(self) -> None:
        derived = self.derive(MOVED)
        self.assertIs(derived["impl"].evidence.new_evidence_or_progress, True)
        self.assertIsNone(derived["impl"].evidence.repeated_unchanged_state)
        self.assertEqual(derived["impl"].provenance["new_evidence_or_progress"],
                         f"review_sha_moved:{SHA_A}->{SHA_B}:findings_delta=2")
        self.assertEqual(self.rounds("impl", MOVED), 4)

    def test_undeterminable_lineages_stay_unknown(self) -> None:
        cases = {
            "fewer_than_two_verdicted_reviews": UNCHANGED[:1],
            "reviewed_sha_missing": [UNCHANGED[0], UNCHANGED[1],
                                     _review("rev2", "fix-rev1-r1", None, "request_changes", [], 4)],
            "findings_unparsable": [UNCHANGED[0], UNCHANGED[1],
                                    _review("rev2", "fix-rev1-r1", SHA_A, "request_changes", "{not json", 4)],
        }
        for why, rows in cases.items():
            with self.subTest(why=why):
                item = self.derive(rows)["impl"]
                self.assertIsNone(item.evidence.new_evidence_or_progress)
                self.assertIsNone(item.evidence.repeated_unchanged_state)
                self.assertEqual(item.provenance["new_evidence_or_progress"], f"progress_not_determinable:{why}")
                self.assertEqual(self.rounds("impl", rows), 3)

    def test_approve_on_same_sha_is_not_unchanged_state(self) -> None:
        rows = [UNCHANGED[0], UNCHANGED[1], _review("rev2", "fix-rev1-r1", SHA_A, "approve", [], 4)]
        item = self.derive(rows)["impl"]
        self.assertIsNone(item.evidence.repeated_unchanged_state)
        self.assertEqual(item.provenance["repeated_unchanged_state"],
                         "progress_not_determinable:standing_verdict_is_approve")

    def test_reviewed_sha_uses_the_canary_full_object_id_validator(self) -> None:
        def pair(first, second):
            return [_review("rev1", "impl", first, "request_changes", ["x"], 2), UNCHANGED[1],
                    _review("rev2", "fix-rev1-r1", second, "request_changes", ["x"], 4)]

        abbreviated = self.derive(pair("b1c2d3e", "b1c2d3e"))["impl"]
        self.assertIsNone(abbreviated.evidence.repeated_unchanged_state)
        self.assertEqual(abbreviated.provenance["repeated_unchanged_state"],
                         "progress_not_determinable:reviewed_sha_missing")
        for sha in ("ABCDEF0123" * 4, "c" * 64):
            with self.subTest(length=len(sha)):
                item = self.derive(pair(sha, sha.lower()))["impl"]
                self.assertIs(item.evidence.repeated_unchanged_state, True)
                self.assertEqual(item.provenance["repeated_unchanged_state"],
                                 f"review_sha_unchanged:{sha.lower()}:rev2")
        for bad in ("c" * 41, "g" * 40, "c" * 63):
            with self.subTest(bad=bad):
                item = self.derive(pair(bad, bad))["impl"]
                self.assertIsNone(item.evidence.repeated_unchanged_state)

    def test_prev_task_id_cycle_terminates_without_evidence(self) -> None:
        rows = [
            _review("rev1", "fix-rev2-r1", SHA_A, "request_changes", [], 2),
            _fix("fix-rev1-r1", "rev1", 3),
            _review("rev2", "fix-rev1-r1", SHA_A, "request_changes", [], 4),
            _fix("fix-rev2-r1", "rev2", 5),
        ]
        derived = self.derive(rows, ("rev1", "rev2", "fix-rev1-r1", "fix-rev2-r1"))
        self.assertEqual(derived, {})

    def test_emit_contract_cites_both_rationales(self) -> None:
        observed = {}
        for name, rows in (("unchanged", UNCHANGED), ("moved", MOVED)):
            db = _db(self.dir / f"{name}.sqlite", rows, ("impl",))
            report, records = _produce_shadow_report([db])
            contract = build_contract(report, records, [db], produced_at="2026-09-23T00:00:00+00:00")
            decision = next(item for item in contract["decisions"] if item["task_id"] == "impl")
            observed[name] = (decision["recommended_max_review_fix_rounds"], decision["rationale"])
        self.assertEqual(observed["unchanged"][0], 2)
        self.assertIn("unchanged_state_does_not_extend_review_fix_envelope", observed["unchanged"][1])
        self.assertEqual(observed["moved"][0], 4)
        self.assertIn("new_evidence_extends_review_fix_envelope", observed["moved"][1])


if __name__ == "__main__":
    unittest.main()
