"""Regression tests for quota-core issue #58: conform to Agent Crew's real contract.

Uses tests/fixtures/agent_crew/real_contract/ (sanitized golden fixtures
derived from real `agent_crew` production output), not the synthetic
fixtures in tests/fixtures/agent_crew/ -- those were authored against a
documented contract that turned out to differ from the real one. See that
fixture directory's README.md for the four confirmed mismatches this file
tests against.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from quota_core.context_economics import (
    ProviderUsageRecord,
    TokenComponents,
    correlate_task_economics,
    normalize_outcome,
    parse_flexible_timestamp,
    reconcile_attribution_by_task,
    token_components_total,
    tokens_per_successful_task,
)
from quota_core.context_economics.agent_crew_adapter import (
    read_attribution_jsonl,
    read_lifecycle_events_jsonl,
)

REAL_FIXTURES = Path(__file__).parent / "fixtures" / "agent_crew" / "real_contract"


class TimestampMismatchTests(unittest.TestCase):
    """Issue #58 point 1: real lifecycle events use `ts` (ISO-8601), not `timestamp` (int)."""

    def test_parse_flexible_timestamp_accepts_real_iso_format(self):
        # Naive (no explicit timezone) -- Agent Crew's real `ts` values look like this.
        epoch = parse_flexible_timestamp("2026-08-21T23:56:19.497696")
        self.assertIsInstance(epoch, int)
        self.assertGreater(epoch, 1_700_000_000)

    def test_parse_flexible_timestamp_still_accepts_unix_epoch(self):
        self.assertEqual(parse_flexible_timestamp(1787356579), 1787356579)
        self.assertEqual(parse_flexible_timestamp(1787356579.76), 1787356579)

    def test_parse_flexible_timestamp_rejects_garbage(self):
        self.assertIsNone(parse_flexible_timestamp("not-a-timestamp"))
        self.assertIsNone(parse_flexible_timestamp(None))
        self.assertIsNone(parse_flexible_timestamp(""))

    def test_real_lifecycle_events_are_not_dropped(self):
        events = read_lifecycle_events_jsonl(REAL_FIXTURES / "context_events.jsonl")
        # 14 lines in the real-shaped fixture, all with a valid `ts` -- none
        # should be silently discarded for lacking the old `timestamp` field.
        self.assertEqual(len(events), 14)
        first = events[0]
        self.assertGreater(first.timestamp, 1_700_000_000)


class OutcomeMismatchTests(unittest.TestCase):
    """Issue #58 point 2: real success is "completed", not "success"."""

    def test_completed_normalizes_to_success(self):
        self.assertEqual(normalize_outcome("completed"), "success")

    def test_original_success_value_still_normalizes(self):
        self.assertEqual(normalize_outcome("success"), "success")

    def test_colon_delimited_failure_reason_normalizes_to_failed(self):
        self.assertEqual(normalize_outcome("failed:dispatcher_timeout"), "failed")

    def test_empty_outcome_is_unknown_in_progress_not_failure(self):
        self.assertIsNone(normalize_outcome(""))
        self.assertIsNone(normalize_outcome(None))

    def test_unrecognized_outcome_is_explicitly_unknown_not_success(self):
        self.assertEqual(normalize_outcome("some_future_state"), "unknown")

    def test_real_attribution_raw_outcome_is_preserved_for_diagnostics(self):
        attributions = read_attribution_jsonl(REAL_FIXTURES / "attribution.jsonl")
        reconciled = reconcile_attribution_by_task(attributions)
        failed = next(a for a in reconciled if a.task_id == "impl-08da84a7")
        self.assertEqual(failed.outcome, "failed")
        self.assertEqual(failed.raw_outcome, "failed:dispatcher_timeout")


class ReconciliationMismatchTests(unittest.TestCase):
    """Issue #58 point 3: attribution.jsonl is snapshot/event-like, not one-row-per-task."""

    def test_multiple_rows_for_same_task_id_collapse_to_one(self):
        attributions = read_attribution_jsonl(REAL_FIXTURES / "attribution.jsonl")
        # 9 raw lines in the fixture but only 4 distinct task_ids.
        self.assertEqual(len(attributions), 9)
        reconciled = reconcile_attribution_by_task(attributions)
        task_ids = [a.task_id for a in reconciled]
        self.assertEqual(len(task_ids), len(set(task_ids)), "reconciliation must dedupe by task_id")
        self.assertEqual(len(reconciled), 4)

    def test_reconciliation_picks_the_terminal_row_not_an_earlier_dispatch_row(self):
        attributions = read_attribution_jsonl(REAL_FIXTURES / "attribution.jsonl")
        reconciled = reconcile_attribution_by_task(attributions)
        tester_task = next(a for a in reconciled if a.task_id == "test-a4f50eb4")
        # The fixture's 3rd row for this task_id is the terminal one (outcome=completed,
        # session_task_index=28, completed_at set) -- not the 1st dispatch row
        # (outcome unset, session_task_index=27, completed_at=0).
        self.assertEqual(tester_task.outcome, "success")
        self.assertEqual(tester_task.session_task_index, 28)
        self.assertIsNotNone(tester_task.completed_at)

    def test_reconciliation_preserves_first_seen_task_order(self):
        attributions = read_attribution_jsonl(REAL_FIXTURES / "attribution.jsonl")
        reconciled = reconcile_attribution_by_task(attributions)
        self.assertEqual(
            [a.task_id for a in reconciled],
            ["test-a4f50eb4", "review-98011b5c", "impl-08da84a7", "impl-retry-1a2b3c4d"],
        )

    def test_retry_lineage_is_reconstructable(self):
        attributions = read_attribution_jsonl(REAL_FIXTURES / "attribution.jsonl")
        reconciled = reconcile_attribution_by_task(attributions)
        retry = next(a for a in reconciled if a.task_id == "impl-retry-1a2b3c4d")
        original = next(a for a in reconciled if a.task_id == "impl-08da84a7")
        self.assertEqual(retry.retry_of, original.task_id)
        self.assertEqual(original.outcome, "failed")
        self.assertEqual(retry.outcome, "success")


class FieldDerivationMismatchTests(unittest.TestCase):
    """Issue #58 point 4: real rows have no `runtime`/`provider` field, only `agent`."""

    def test_provider_is_derived_from_agent_when_not_explicit(self):
        attributions = read_attribution_jsonl(REAL_FIXTURES / "attribution.jsonl")
        reconciled = reconcile_attribution_by_task(attributions)
        by_id = {a.task_id: a for a in reconciled}
        self.assertEqual(by_id["test-a4f50eb4"].provider, "gemini")
        self.assertEqual(by_id["review-98011b5c"].provider, "codex")
        self.assertEqual(by_id["impl-08da84a7"].provider, "claude")

    def test_unrecognized_fields_are_preserved_not_dropped(self):
        attributions = read_attribution_jsonl(REAL_FIXTURES / "attribution.jsonl")
        record = attributions[0]
        # worktree_path/repo_url/git_branch/created_at/status aren't named fields
        # on RuntimeAttribution -- they must still be readable via `extra`.
        self.assertIn("worktree_path", record.extra)
        self.assertIn("repo_url", record.extra)
        self.assertEqual(record.extra.get("status"), "in_progress")

    def test_empty_string_provider_session_id_normalizes_to_none(self):
        attributions = read_attribution_jsonl(REAL_FIXTURES / "attribution.jsonl")
        # Real data always writes "" rather than omitting the key or using null.
        self.assertTrue(all(a.provider_session_id is None for a in attributions))


class EndToEndContractTest(unittest.TestCase):
    """real fixture -> adapter -> reconciliation -> correlation -> TaskEconomicsRecord.

    This is the full producer-to-consumer seam issue #58 asks for -- it
    should fail on schema drift (e.g. if a future Agent Crew release renames
    a field again) rather than silently producing an empty/wrong report.
    """

    def test_real_fixture_produces_a_reconcilable_task_level_report(self):
        attributions = read_attribution_jsonl(REAL_FIXTURES / "attribution.jsonl")
        reconciled = reconcile_attribution_by_task(attributions)
        self.assertEqual(len(reconciled), 4)

        usage = [
            ProviderUsageRecord(
                provider=a.provider or "unknown",
                project=a.project,
                started_at=a.started_at,
                completed_at=a.completed_at,
                tokens=TokenComponents(fresh_input=1000, output=200, provider_total=1200),
            )
            for a in reconciled
        ]
        records = correlate_task_economics(reconciled, usage)
        self.assertEqual(len(records), 4)

        # Every record resolves to a real provider (derived from agent) and a
        # non-fabricated confidence tier -- nothing should come back as
        # completely unattributed given every task has matching usage here.
        for record in records:
            self.assertIsNotNone(record.provider)
            self.assertIn(record.attribution_confidence, ("high", "medium", "low"))

        # The successful tasks (3 of 4: tester, reviewer, retry) should be
        # visible to the success-based analytics -- this is exactly what
        # would silently read as zero if "completed" weren't normalized to
        # "success".
        successful = [r for r in records if r.succeeded]
        self.assertEqual(len(successful), 3)
        self.assertIsNotNone(tokens_per_successful_task(records))

        failed_task = next(r for r in records if r.task_id == "impl-08da84a7")
        self.assertFalse(failed_task.succeeded)
        self.assertEqual(failed_task.raw_outcome, "failed:dispatcher_timeout")
        self.assertIsNotNone(token_components_total(failed_task.tokens))


if __name__ == "__main__":
    unittest.main()
