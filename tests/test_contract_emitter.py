"""Coverage for the atomic organic -> reader-contract emitter (#80).

The reader that consumes this artifact lives in another repository
(agent_crew's ``tokenomics_shadow``).  These tests deliberately re-implement
its matching rule locally instead of importing it, so the contract is verified
against the consumer's *semantics* without coupling the two packages.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quota_core.context_economics import (
    TASK_DECISION_UNAVAILABLE,
    build_contract,
    emit_contract,
    policy_contract_sha256,
    write_contract_atomically,
)
from quota_core.context_economics.contract_emitter import main
from quota_core.context_economics.report_producer import REPORT_CONTRACT_ID
from quota_core.context_economics.report_producer import main as producer_main


FIXTURE = Path(__file__).parent / "fixtures" / "economics_policy" / "organic-report-rows.json"


def _db(path: Path, rows: list[dict[str, object]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE task_attribution (
        task_id TEXT PRIMARY KEY, created_at REAL, outcome TEXT,
        provider TEXT, model TEXT, uncached_input_tokens INTEGER,
        cache_read_tokens INTEGER, cache_write_tokens INTEGER,
        output_tokens INTEGER, reasoning_tokens INTEGER
    )""")
    for row in rows:
        conn.execute(
            "INSERT INTO task_attribution VALUES (:task_id, :created_at, :outcome, "
            ":provider, :model, :uncached_input_tokens, :cache_read_tokens, "
            ":cache_write_tokens, :output_tokens, :reasoning_tokens)", row,
        )
    conn.commit()
    conn.close()


def reader_shadow_recommendation(path: Path, task_id: str) -> dict[str, object]:
    """Re-implementation of the external reader's resolution, from its source.

    Mirrors ``agent_crew.tokenomics_shadow.shadow_recommendation_for_task_id``:
    hash the raw bytes, require top-level ``contract_version == "1.0"`` and
    ``mode == "shadow"``, then find the ``decisions`` list entry whose
    ``task_id`` matches.  Unknown top-level keys are never consulted.
    """
    baseline = {"decision_source": "baseline", "policy_version": None,
                "recommendation": None, "reason": "policy_unavailable",
                "contract_sha": None}
    contract_sha = None
    try:
        raw_contract = Path(path).read_bytes()
        contract_sha = hashlib.sha256(raw_contract).hexdigest()
        contract = json.loads(raw_contract)
        if not isinstance(contract, dict):
            return {**baseline, "contract_sha": contract_sha}
        if contract.get("contract_version") == "1.0" and contract.get("mode") == "shadow":
            decisions = contract.get("decisions")
            if not isinstance(decisions, list):
                return {**baseline, "contract_sha": contract_sha}
            recommendation = next(
                (item for item in decisions
                 if isinstance(item, dict) and item.get("task_id") == task_id),
                None,
            )
            if not isinstance(recommendation, dict):
                return {**baseline, "reason": "task_decision_unavailable",
                        "contract_sha": contract_sha}
            return {"decision_source": "quota_core_contract",
                    "policy_version": contract["contract_version"],
                    "recommendation": recommendation,
                    "reason": "shadow_only", "contract_sha": contract_sha}
        return {**baseline, "reason": "contract_version_unsupported",
                "contract_sha": contract_sha}
    except (OSError, ValueError, TypeError):
        return {**baseline, "contract_sha": contract_sha}


class ContractEmitterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "attribution.sqlite"
        self.rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
        _db(self.db_path, self.rows)
        self.out = self.root / "contract" / "tokenomics-policy.json"
        self.task_ids = [row["task_id"] for row in self.rows]

    def test_emitted_contract_matches_the_reader_shape(self) -> None:
        contract, _sha = emit_contract(self.out, [self.db_path])
        self.assertEqual(contract["contract_version"], "1.0")
        self.assertEqual(contract["mode"], "shadow")
        self.assertIsInstance(contract["decisions"], list)
        self.assertEqual(contract["decision_count"], len(contract["decisions"]))
        self.assertEqual(
            sorted(decision["task_id"] for decision in contract["decisions"]),
            sorted(self.task_ids),
        )
        # Every decision carries the shadow/recommend-only markers unchanged.
        for decision in contract["decisions"]:
            self.assertEqual(decision["mode"], "shadow")
            self.assertEqual(decision["contract_version"], "1.0")

    def test_reader_resolves_a_present_task_from_the_written_file(self) -> None:
        emit_contract(self.out, [self.db_path])
        resolved = reader_shadow_recommendation(self.out, self.task_ids[0])
        self.assertEqual(resolved["decision_source"], "quota_core_contract")
        self.assertEqual(resolved["policy_version"], "1.0")
        self.assertEqual(resolved["reason"], "shadow_only")
        self.assertEqual(resolved["recommendation"]["task_id"], self.task_ids[0])

    def test_reader_reports_an_absent_task_as_unavailable(self) -> None:
        emit_contract(self.out, [self.db_path])
        resolved = reader_shadow_recommendation(self.out, "never-observed-task")
        self.assertEqual(resolved["decision_source"], "baseline")
        self.assertEqual(resolved["reason"], TASK_DECISION_UNAVAILABLE)
        self.assertIsNone(resolved["recommendation"])

    def test_provenance_block_is_additive_and_ignored_by_the_reader(self) -> None:
        contract, _sha = emit_contract(self.out, [self.db_path], produced_at="2026-09-23T00:00:00+00:00")
        provenance = contract["provenance"]
        self.assertEqual(provenance["report_contract_id"], REPORT_CONTRACT_ID)
        self.assertEqual(provenance["policy_contract"]["sha256"], policy_contract_sha256())
        self.assertEqual(provenance["produced_at"], "2026-09-23T00:00:00+00:00")
        self.assertEqual(provenance["decision_count"], contract["decision_count"])
        # The extra key does not disturb resolution through the reader.
        resolved = reader_shadow_recommendation(self.out, self.task_ids[0])
        self.assertEqual(resolved["decision_source"], "quota_core_contract")

    def test_source_provenance_hashes_the_contributing_rows(self) -> None:
        contract = build_contract([self.db_path], produced_at="2026-09-23T00:00:00+00:00")
        entry, = contract["provenance"]["source_dbs"]
        self.assertEqual(entry["path_basename"], "attribution.sqlite")
        self.assertEqual(entry["row_count"], len(self.rows))
        self.assertRegex(entry["sha256_of_rows_or_rowcount"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            entry["watermark"],
            max(int(row["created_at"]) for row in self.rows if row["created_at"] is not None),
        )
        # Re-reading unchanged rows must reproduce the same row hash.
        again = build_contract([self.db_path], produced_at="2026-09-23T00:00:00+00:00")
        self.assertEqual(again["provenance"]["source_dbs"], contract["provenance"]["source_dbs"])

    def test_source_row_hash_changes_when_a_new_row_lands(self) -> None:
        before = build_contract([self.db_path], produced_at="2026-09-23T00:00:00+00:00")
        extra = dict(self.rows[0])
        extra["task_id"] = "organic-newly-observed"
        _db_conn = sqlite3.connect(self.db_path)
        _db_conn.execute(
            "INSERT INTO task_attribution VALUES (:task_id, :created_at, :outcome, "
            ":provider, :model, :uncached_input_tokens, :cache_read_tokens, "
            ":cache_write_tokens, :output_tokens, :reasoning_tokens)", extra,
        )
        _db_conn.commit()
        _db_conn.close()
        after = build_contract([self.db_path], produced_at="2026-09-23T00:00:00+00:00")
        self.assertNotEqual(
            after["provenance"]["source_dbs"][0]["sha256_of_rows_or_rowcount"],
            before["provenance"]["source_dbs"][0]["sha256_of_rows_or_rowcount"],
        )
        self.assertEqual(after["provenance"]["source_dbs"][0]["row_count"], len(self.rows) + 1)

    def test_returned_sha_matches_the_bytes_on_disk(self) -> None:
        _contract, contract_sha = emit_contract(self.out, [self.db_path])
        self.assertEqual(
            contract_sha, hashlib.sha256(self.out.read_bytes()).hexdigest()
        )
        self.assertEqual(
            contract_sha, reader_shadow_recommendation(self.out, self.task_ids[0])["contract_sha"]
        )

    def test_failed_write_leaves_no_partial_file_and_keeps_the_previous_one(self) -> None:
        emit_contract(self.out, [self.db_path])
        previous = self.out.read_bytes()
        with patch(
            "quota_core.context_economics.contract_emitter.os.replace",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                write_contract_atomically(self.out, {"contract_version": "1.0"})
        self.assertEqual(self.out.read_bytes(), previous)
        leftovers = [path.name for path in self.out.parent.iterdir() if path.name != self.out.name]
        self.assertEqual(leftovers, [])

    def test_reader_never_sees_a_partially_written_file(self) -> None:
        """A concurrent reader observes either the old artifact or the new one."""
        emit_contract(self.out, [self.db_path])
        observed: list[str] = []

        real_replace = __import__("os").replace

        def _observing_replace(src, dst):
            # Mid-swap, the destination still holds a complete previous artifact.
            observed.append(reader_shadow_recommendation(dst, self.task_ids[0])["decision_source"])
            return real_replace(src, dst)

        with patch(
            "quota_core.context_economics.contract_emitter.os.replace",
            side_effect=_observing_replace,
        ):
            emit_contract(self.out, [self.db_path])
        self.assertEqual(observed, ["quota_core_contract"])

    def test_cli_emits_the_contract_and_reports_the_lookup(self) -> None:
        exit_code = main([
            "--db", str(self.db_path), "--out", str(self.out),
            "--lookup-task", self.task_ids[0],
        ])
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            reader_shadow_recommendation(self.out, self.task_ids[0])["decision_source"],
            "quota_core_contract",
        )

    def test_rolling_producer_can_also_emit_the_contract(self) -> None:
        rolling = self.root / "rolling-report.json"
        exit_code = producer_main([
            "--db", str(self.db_path), "--out", str(rolling),
            "--contract-out", str(self.out),
        ])
        self.assertEqual(exit_code, 0)
        rolling_report = json.loads(rolling.read_text(encoding="utf-8"))
        contract = json.loads(self.out.read_text(encoding="utf-8"))
        # Same organic records, two artifacts: the rolling report keyed by task
        # ID, and the reader-shaped contract carrying the same decision count.
        self.assertEqual(rolling_report["decision_count"], contract["decision_count"])
        self.assertEqual(
            sorted(rolling_report["decisions"]),
            sorted(decision["task_id"] for decision in contract["decisions"]),
        )

    def test_emitting_does_not_write_the_live_contract_path(self) -> None:
        """The emitter only ever writes the path it was handed."""
        emit_contract(self.out, [self.db_path])
        self.assertTrue(self.out.is_file())
        self.assertEqual(
            sorted(path.name for path in self.out.parent.iterdir()),
            [self.out.name],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
