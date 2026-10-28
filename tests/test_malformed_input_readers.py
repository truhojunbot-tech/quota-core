"""quota-core#115 part B: every telemetry/config/receipt reader survives
empty, truncated, non-JSON, non-object and missing-key input.

Each reader keeps its own convention: line/row readers skip the bad record
(and keep the good ones), whole-file config readers return a typed error or
an explicit "unknown" value, and CLIs exit through ``parser.error``.
"""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from quota_core.adapters.claude import scan_claude_local
from quota_core.adapters.gemini import scan_gemini_local
from quota_core.cli import load_snapshot_file
from quota_core.cli import main as cli_main
from quota_core.config import ProviderConfig
from quota_core.context_economics import acceptance, metrics_b1, report_producer
from quota_core.context_economics.agent_crew_adapter import (
    read_attribution_jsonl,
    read_lifecycle_events_jsonl,
    read_task_cascade_signals,
)
from quota_core.context_economics.quality_evidence import _findings_set, _linked_target
from quota_core.session import analyze_claude_sessions
from quota_core.session.claude import subagent_type_from_transcript

# Bad lines carry the readers' cheap pre-filter tokens ("usage", "success",
# "RENEW", "compactMetadata") so they reach json.loads instead of being
# skipped before parsing.
BAD_LINES = {
    "empty": "",
    "truncated": '{"usage": {"input_tokens": 1',
    "non_json": "usage success RENEW compactMetadata not json",
    "non_object": '["usage", "success", "RENEW", "compactMetadata"]',
    "scalar": "42",
    "missing_keys": '{"usage": null, "success": null, "RENEW": null}',
}
BAD_FILES = {
    "empty": "",
    "truncated": '{"a": ',
    "non_json": "not json",
    "non_object": "[1, 2]",
    "missing_keys": "{}",
}


class _TmpDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class MetricsB1JsonlReaderTests(_TmpDir):
    GOOD_REQUEST = json.dumps({
        "type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
        "message": {"id": "m1", "usage": {
            "input_tokens": 3, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1}},
    })
    GOOD_TRANSITION = json.dumps({
        "timestamp": "2026-01-01T00:00:00Z", "event_type": "success",
        "action": "RENEW", "project": "quota_core",
    })

    def test_transcript_requests_skips_bad_lines_and_keeps_good_ones(self):
        for name, bad in BAD_LINES.items():
            with self.subTest(name):
                path = self.root / f"{name}.jsonl"
                path.write_text(f"{bad}\n{self.GOOD_REQUEST}\n")
                requests, boundaries = metrics_b1._transcript_requests(path, 0, 2e9)
                self.assertEqual([r[1] for r in requests], ["m1"])
                self.assertEqual(boundaries, [])

    def test_lifecycle_transitions_skips_bad_lines_and_keeps_good_ones(self):
        for name, bad in BAD_LINES.items():
            with self.subTest(name):
                path = self.root / f"{name}.log"
                path.write_text(f"{bad}\n{self.GOOD_TRANSITION}\n")
                by_project = metrics_b1._lifecycle_transitions(path, 0, 2e9)
                self.assertEqual(list(by_project), ["quota-core"])
                self.assertEqual(len(by_project["quota-core"]), 1)

    def test_receipt_economics_json_bad_rows_are_skipped(self):
        db = self.root / "tasks.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE tokenomics_shadow_receipts (economics_json TEXT, created_at REAL)")
            for raw in [None, *BAD_FILES.values(), '{"ok": 1}']:
                conn.execute("INSERT INTO tokenomics_shadow_receipts VALUES (?, 1.0)", (raw,))
        self.assertEqual(metrics_b1._receipts(db, 0, 2), [{}, {"ok": 1}])


# Shapes of `gh` JSON stdout that a CLI hiccup, rate-limit page or API change
# can produce. A whole-payload problem means the source is unavailable; a bad
# row inside a list is skipped and the good rows still count.
BAD_GH_PAYLOADS = {
    "empty": "",
    "truncated": '[{"number": 9, "title": "fix #42"',
    "object": '{"message": "API rate limit exceeded"}',
    "null": "null",
    "scalar": "5",
}
BAD_GH_ITEMS = {"int": 3, "string": "fix #42", "null": None, "missing_keys": {}}


class MetricsB1GithubStdoutTests(_TmpDir):
    APPROVED_AT = 1767225600.0  # 2026-01-01T00:00:00Z
    END = APPROVED_AT + 72 * 3600
    FOLLOWUP = {"number": 9, "title": "Follow-up fix for #42", "body": "",
                "createdAt": "2026-01-02T00:00:00Z"}
    REOPENED = {"event": "reopened", "created_at": "2026-01-02T00:00:00Z"}

    def _followup(self, listing: str, events: str = "[]"):
        def respond(args, **_kwargs):
            out = listing if args[:3] == ["gh", "pr", "list"] else events
            return subprocess.CompletedProcess(args, 0, out, "")
        with patch.object(metrics_b1.subprocess, "run", side_effect=respond), \
                patch.dict("os.environ", {"QUOTA_CORE_LOCAL_REPOS": ""}):
            return metrics_b1._github_followup("alpha", 42, 7, self.APPROVED_AT, self.END,
                                               cache={}, search_from=self.APPROVED_AT)

    def test_pr_listing_bad_payload_is_unavailable(self):
        for name, payload in BAD_GH_PAYLOADS.items():
            with self.subTest(name):
                self.assertIsNone(self._followup(payload))

    def test_pr_listing_bad_items_are_skipped_and_good_rows_still_count(self):
        for name, item in BAD_GH_ITEMS.items():
            with self.subTest(name):
                self.assertIs(self._followup(json.dumps([item, self.FOLLOWUP])), True)
                # Only bad rows: no follow-up seen, and nothing else proves a clean window.
                self.assertIsNone(self._followup(json.dumps([item])))

    def test_pr_events_bad_payload_is_unavailable(self):
        for name, payload in BAD_GH_PAYLOADS.items():
            with self.subTest(name):
                self.assertIsNone(self._followup("[]", payload))

    def test_pr_events_bad_items_are_skipped_and_good_rows_still_count(self):
        for name, item in BAD_GH_ITEMS.items():
            with self.subTest(name):
                self.assertIs(self._followup("[]", json.dumps([item, self.REOPENED])), True)

    def test_review_followup_metric_survives_bad_gh_rows(self):
        db = self.root / "tasks.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE tasks (task_id TEXT, task_type TEXT, context TEXT, verdict TEXT, "
                         "result_posted_at REAL, created_at REAL, receipt_id TEXT, pr_number INTEGER)")
            conn.execute("CREATE TABLE tokenomics_shadow_receipts (task_id TEXT, "
                         "shadow_recommendation_json TEXT, actual_execution_json TEXT)")
            conn.execute("INSERT INTO tasks VALUES ('impl-1', 'implement', '{\"issue_number\": 42}', "
                         "NULL, NULL, ?, 'cea-1', 7)", (self.APPROVED_AT - 10,))
            conn.execute("INSERT INTO tasks VALUES ('review-1', 'review', '{\"prev_task_id\": \"impl-1\"}', "
                         "'approve', ?, ?, NULL, NULL)", (self.APPROVED_AT, self.APPROVED_AT - 5))
            conn.execute("INSERT INTO tokenomics_shadow_receipts VALUES ('impl-1', '{\"a\": 1}', '{\"b\": 1}')")
        for name, (listing, events) in {
            "listing_items": (json.dumps([3, "x", None]), "[]"),
            "events_null": ("[]", "null"),
        }.items():
            with self.subTest(name):
                def respond(args, **_kwargs):
                    out = listing if args[:3] == ["gh", "pr", "list"] else events
                    return subprocess.CompletedProcess(args, 0, out, "")
                with patch.object(metrics_b1.subprocess, "run", side_effect=respond), \
                        patch.dict("os.environ", {"QUOTA_CORE_LOCAL_REPOS": ""}):
                    row = metrics_b1._review_followup_metric(
                        "alpha", db, self.APPROVED_AT + 72 * 3600 - 1, self.APPROVED_AT + 72 * 3600 + 1)
                self.assertEqual((row["metric"], row["n"], row["eligible"], row["unknown"]),
                                 ("M8", 0, 1, 1))
                self.assertIsNone(row["value"])


class AdapterScanTests(_TmpDir):
    def test_claude_scan_skips_bad_lines_and_non_numeric_token_fields(self):
        good = json.dumps({"message": {"usage": {"input_tokens": 5, "output_tokens": 2}}})
        bad_values = {
            **BAD_LINES,
            "string_tokens": json.dumps({"message": {"usage": {"input_tokens": "abc", "output_tokens": 4}}}),
            "object_tokens": json.dumps({"message": {"usage": {"input_tokens": {"n": 1}}}}),
            **{
                f"{label}_tokens": json.dumps({"message": {"usage": {"input_tokens": value, "output_tokens": 4}}})
                for label, value in {
                    "empty_string": "", "empty_list": [], "empty_object": {}, "false": False,
                    "true": True, "negative": -3, "fractional": 1.9, "fractional_string": "1.9",
                }.items()
            },
            "infinite_tokens": '{"message": {"usage": {"input_tokens": Infinity, "output_tokens": 4}}}',
            # An integral float is a whole count and stays valid: 2 + 4 + good 7.
            "integral_float_ok": json.dumps({"message": {"usage": {"input_tokens": 2.0, "output_tokens": 4}}}),
        }
        for name, bad in bad_values.items():
            with self.subTest(name):
                projects = self.root / name / "proj"
                projects.mkdir(parents=True)
                (projects / "a.jsonl").write_text(f"{bad}\n{good}\n")
                snapshot = scan_claude_local(ProviderConfig(paths={"projects_dir": str(projects.parent)}), 1)
                # A record with an unreadable component is skipped whole and
                # reported, never counted with that component as zero.
                if name == "integral_float_ok":
                    self.assertEqual(snapshot.windows["local_all"].total_tokens, 13)
                    self.assertEqual(snapshot.windows["local_all"].requests, 2)
                    self.assertEqual(snapshot.warnings, ())
                    continue
                self.assertEqual(snapshot.windows["local_all"].total_tokens, 7)
                self.assertEqual(snapshot.windows["local_all"].requests, 1)
                malformed = name.endswith("_tokens")
                self.assertEqual(
                    snapshot.warnings,
                    ("claude skipped 1 records with malformed usage",) if malformed else (),
                )

    def test_gemini_jsonl_scan_tolerates_bad_lines_and_non_string_content(self):
        good = json.dumps({"id": "g1", "type": "gemini", "tokens": {"total": 9}})
        bad_values = {
            **BAD_LINES,
            "null_content": json.dumps({"type": "gemini", "content": None, "tokens": {"total": 1}}),
            "int_content": json.dumps({"type": "gemini", "content": 5, "tokens": {"total": 1}}),
            "object_content": json.dumps({"type": "gemini", "content": {"a": 1}, "tokens": {"total": 1}}),
        }
        for name, bad in bad_values.items():
            with self.subTest(name):
                project = self.root / name / "p"
                project.mkdir(parents=True)
                (project / "session-a.jsonl").write_text(f"{bad}\n{good}\n")
                snapshot = scan_gemini_local(ProviderConfig(paths={"tmp_dir": str(project.parent)}), 1)
                expected = 10 if name.endswith("_content") else 9
                self.assertEqual(snapshot.windows["local_all"].total_tokens, expected)
                # Unparseable / non-object rows are reported, never silent.
                malformed = name in ("truncated", "non_json", "non_object", "scalar")
                self.assertEqual(
                    snapshot.warnings,
                    ("gemini skipped 1 malformed session rows",) if malformed else (),
                )

    def test_gemini_jsonl_scan_with_only_malformed_rows_is_not_a_clean_zero(self):
        project = self.root / "all_bad" / "p"
        project.mkdir(parents=True)
        (project / "session-a.jsonl").write_text("{bad\n[1]\n\n")
        snapshot = scan_gemini_local(ProviderConfig(paths={"tmp_dir": str(project.parent)}), 1)
        self.assertEqual(snapshot.windows["local_all"].total_tokens, 0)
        self.assertEqual(snapshot.warnings, ("gemini skipped 2 malformed session rows",))

    def test_gemini_json_scan_tolerates_bad_files_and_bad_messages_field(self):
        good_message = {"id": "g1", "type": "gemini", "tokens": {"total": 9}}
        cases = {
            **BAD_FILES,
            "messages_scalar": json.dumps({"messages": 3}),
            "messages_object": json.dumps({"messages": {"a": 1}}),
            "null_content": json.dumps({"messages": [
                {"type": "gemini", "content": None, "tokens": {"total": 1}}, good_message]}),
            "object_content": json.dumps({"messages": [
                {"type": "gemini", "content": {"a": 1}, "tokens": {"total": 1}}, good_message]}),
        }
        for name, contents in cases.items():
            with self.subTest(name):
                project = self.root / name / "p"
                project.mkdir(parents=True)
                (project / "session-a.json").write_text(contents)
                snapshot = scan_gemini_local(ProviderConfig(paths={"tmp_dir": str(project.parent)}), 1)
                expected = 10 if name.endswith("_content") else 0
                self.assertEqual(snapshot.windows["local_all"].total_tokens, expected)
                # A file with no usable shape is reported, so it cannot pass
                # for a valid session with zero use; "{}" has no messages.
                malformed = name not in ("missing_keys", "null_content", "object_content")
                self.assertEqual(
                    snapshot.warnings,
                    ("gemini skipped 1 malformed session files",) if malformed else (),
                )


class SessionReaderTests(_TmpDir):
    def test_session_analyzer_counts_malformed_lines_and_keeps_good_ones(self):
        good = json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
                           "message": {"id": "m1", "usage": {"input_tokens": 5}}})
        for name, bad in BAD_LINES.items():
            with self.subTest(name):
                project = self.root / name / "proj"
                project.mkdir(parents=True)
                (project / "s.jsonl").write_text(f"{bad}\n{good}\n")
                result = analyze_claude_sessions([project.parent], since="all", redaction="preview")
                self.assertEqual(result["totals"]["input_tokens"], 5)

    def test_subagent_metadata_bad_files_are_unknown(self):
        subagents = self.root / "subagents"
        subagents.mkdir()
        transcript = subagents / "agent-1.jsonl"
        transcript.write_text("")
        for name, contents in {**BAD_FILES, "wrong_type": '{"agentType": 3}'}.items():
            with self.subTest(name):
                (subagents / "agent-1.meta.json").write_text(contents)
                self.assertEqual(subagent_type_from_transcript(transcript), "unknown")


class AgentCrewAdapterTests(_TmpDir):
    def test_attribution_and_lifecycle_jsonl_skip_bad_lines(self):
        good_event = json.dumps({"event_type": "context_reset", "ts": "2026-01-01T00:00:00"})
        for name, bad in BAD_LINES.items():
            with self.subTest(name):
                path = self.root / f"{name}.jsonl"
                path.write_text(f"{bad}\n{good_event}\n")
                self.assertEqual(len(read_lifecycle_events_jsonl(path)), 1)
                read_attribution_jsonl(path)  # must not raise

    def test_task_context_column_bad_values_become_empty_context(self):
        db = self.root / "tasks.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE tasks (task_id TEXT, task_type TEXT, branch TEXT, context TEXT)")
            for index, raw in enumerate([None, *BAD_FILES.values()]):
                conn.execute("INSERT INTO tasks VALUES (?, 'review', 'b', ?)", (f"t{index}", raw))
        signals = read_task_cascade_signals(db)
        self.assertEqual(len(signals), len(BAD_FILES) + 1)
        self.assertTrue(all(item["context"] == {} for item in signals.values()))


class QualityEvidenceColumnTests(unittest.TestCase):
    def test_bad_context_and_findings_values_are_unknown(self):
        for name, raw in BAD_FILES.items():
            with self.subTest(name):
                self.assertIsNone(_linked_target(raw))
        for raw in ['{"a": ', "not json", '{"a": 1}']:
            with self.subTest(raw):
                self.assertIsNone(_findings_set(raw))
        self.assertEqual(_findings_set(""), frozenset())


class ConfigAndReportFileTests(_TmpDir):
    def _write(self, contents: str) -> Path:
        path = self.root / "input.json"
        path.write_text(contents)
        return path

    def test_snapshot_file_raises_typed_value_error(self):
        for name, contents in BAD_FILES.items():
            with self.subTest(name), self.assertRaises(ValueError):
                load_snapshot_file(self._write(contents))

    def test_report_producer_existing_state_and_db_list_degrade(self):
        for name, contents in BAD_FILES.items():
            with self.subTest(name):
                path = self._write(contents)
                state = report_producer._load_existing(path)
                self.assertEqual(state, {} if name == "missing_keys" else None)
                # A JSON array keeps its string entries; anything else is logged.
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(report_producer._db_paths_from_args(["x.db"], str(path)), ["x.db"])

    def test_report_producer_parity_check_bad_contract_is_a_usage_error(self):
        db = self.root / "attribution.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE unrelated (x INTEGER)")
        cases = {name: BAD_FILES[name] for name in ("empty", "truncated", "non_json", "non_object")}
        cases.update({
            "missing_decisions": "{}",
            "decisions_scalar": '{"decisions": 3}',
            "decision_without_task_id": '{"decisions": [{}]}',
        })
        for name, contents in cases.items():
            with self.subTest(name):
                path = self._write(contents)
                stderr = io.StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    report_producer.main(["--db", str(db), "--parity-check", str(path)])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("parity", stderr.getvalue())

    def test_shadow_report_bad_pricing_file_leaves_costs_unknown(self):
        db = self.root / "attribution.db"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE task_attribution (task_id TEXT, status TEXT, task_type TEXT, provider TEXT, "
                "model TEXT, uncached_input_tokens INTEGER, cache_write_tokens INTEGER, "
                "cache_read_tokens INTEGER, output_tokens INTEGER)"
            )
            conn.execute(
                "INSERT INTO task_attribution VALUES "
                "('t1', 'completed', 'implement', 'provider-a', 'model-y', 100, 0, 0, 10)"
            )

        def cost(*extra: str) -> dict:
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                self.assertEqual(cli_main(["context-economics-shadow-report", "--database", str(db), *extra]), 0)
            artifacts = json.loads(stdout.getvalue())["artifacts"]
            self.assertEqual([item["task_id"] for item in artifacts], ["t1"])
            return artifacts[0]["cost"]

        unknown = {"uncached_input": None, "cache_write": None, "cache_read": None,
                   "output": None, "reasoning": None}
        pricing = self.root / "pricing.json"
        # Control: a valid price book does cost this decision.
        pricing.write_text(json.dumps({"provider-a": {"default": {"uncached_input": 0.1, "output": 0.2}}}))
        priced = cost("--pricing", str(pricing))
        self.assertTrue(priced["priced"])
        self.assertAlmostEqual(priced["productive"]["uncached_input"], 10.0)
        self.assertAlmostEqual(priced["productive"]["output"], 2.0)

        cases = {
            **BAD_FILES,
            "provider_not_object": '{"provider-a": 3}',
            "rate_not_number": '{"provider-a": {"default": {"uncached_input": "x"}}}',
        }
        for name, contents in cases.items():
            with self.subTest(name):
                pricing.write_text(contents)
                payload = cost("--pricing", str(pricing))
                self.assertFalse(payload["rates_found"])
                self.assertFalse(payload["priced"])
                self.assertEqual(payload["productive"], unknown)
        with self.subTest("missing_file"):
            payload = cost("--pricing", str(self.root / "absent.json"))
            self.assertFalse(payload["priced"])
            self.assertEqual(payload["productive"], unknown)

    def test_acceptance_bad_report_is_a_usage_error(self):
        for name in ("empty", "truncated", "non_json", "non_object"):
            with self.subTest(name):
                path = self._write(BAD_FILES[name])
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    acceptance.main(["--report", str(path)])
                self.assertEqual(raised.exception.code, 2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(acceptance.main(["--report", str(self._write("{}"))]), 0)


if __name__ == "__main__":
    unittest.main()
