from __future__ import annotations

import unittest
from pathlib import Path

from quota_core.context_economics import (
    RuntimeAttribution,
    TaskEconomicsRecord,
    TokenComponents,
    attribution_from_dict,
    attribution_to_dict,
    before_after_compact,
    cache_read_per_task,
    claude_token_components,
    codex_token_components,
    compare_context_policies,
    context_age_vs_failure_rate,
    context_age_vs_token_usage,
    correlate_task_economics,
    failed_retry_token_waste,
    fresh_input_per_successful_task,
    gemini_token_components,
    lifecycle_event_from_dict,
    token_components_from_dict,
    token_components_to_dict,
    token_components_total,
    tokens_per_outcome,
    tokens_per_successful_task,
    validate_attribution_dict,
    ProviderUsageRecord,
)
from quota_core.context_economics.agent_crew_adapter import (
    filter_by_context,
    filter_by_provider,
    read_attribution_jsonl,
    read_lifecycle_events_jsonl,
)

FIXTURES = Path(__file__).parent / "fixtures" / "agent_crew"


class TokenComponentsTests(unittest.TestCase):
    def test_components_stay_separate_for_claude(self):
        usage = {
            "input_tokens": 100,
            "output_tokens": 40,
            "cache_read_input_tokens": 5000,
            "cache_creation_input_tokens": 800,
        }
        components = claude_token_components(usage)
        self.assertEqual(components.fresh_input, 100)
        self.assertEqual(components.output, 40)
        self.assertEqual(components.cache_read, 5000)
        self.assertEqual(components.cache_creation, 800)
        # provider_total is derived (sum), not fabricated as one bucket collapsing the others.
        self.assertEqual(components.provider_total, 5940)
        self.assertTrue(components.has_full_breakdown)

    def test_partial_provider_keeps_unknown_components_unknown(self):
        components = gemini_token_components({"total_token_count": 1200})
        self.assertIsNone(components.fresh_input)
        self.assertIsNone(components.cache_creation)
        self.assertEqual(components.provider_total, 1200)
        self.assertFalse(components.has_full_breakdown)
        # Unknown must not be fabricated as zero.
        self.assertNotEqual(components.fresh_input, 0)

    def test_codex_partial_breakdown(self):
        # cached_input_tokens is a SUBSET of input_tokens per OpenAI's usage
        # semantics -- fresh_input must be the non-cached remainder (200), not
        # the raw input_tokens value, or cached tokens get double-counted.
        components = codex_token_components({"input_tokens": 500, "output_tokens": 120, "cached_input_tokens": 300})
        self.assertEqual(components.fresh_input, 200)
        self.assertEqual(components.output, 120)
        self.assertEqual(components.cache_read, 300)
        self.assertIsNone(components.cache_creation)
        # provider_total must not add cached_input on top of input_tokens (which already includes it).
        self.assertEqual(components.provider_total, 620)

    def test_token_components_round_trip(self):
        components = TokenComponents(fresh_input=1, output=2, cache_read=3, cache_creation=4, tool_tokens=5, provider_total=15)
        restored = token_components_from_dict(token_components_to_dict(components))
        self.assertEqual(components, restored)

    def test_token_components_total_none_when_fully_unknown(self):
        self.assertIsNone(token_components_total(TokenComponents()))


class AgentCrewAdapterTests(unittest.TestCase):
    def test_adapter_module_does_not_import_agent_crew(self):
        import quota_core.context_economics.agent_crew_adapter as mod

        assert mod.__file__ is not None
        source = Path(mod.__file__).read_text()
        self.assertNotIn("import agent_crew", source)
        self.assertNotIn("from agent_crew", source)

    def test_parses_documented_fixture(self):
        attributions = read_attribution_jsonl(FIXTURES / "attribution_sample.jsonl")
        # 7 lines in the fixture, all valid JSON -> all parse tolerantly.
        self.assertEqual(len(attributions), 7)
        by_task = {a.task_id: a for a in attributions}
        self.assertEqual(by_task["claude-e5f6a7b8"].context_policy, "resume")
        self.assertEqual(by_task["claude-e5f6a7b8"].session_task_index, 1)

    def test_forward_compatible_field_is_preserved(self):
        attributions = read_attribution_jsonl(FIXTURES / "attribution_sample.jsonl")
        record = next(a for a in attributions if a.task_id == "claude-forward-compat")
        self.assertEqual(record.schema_version, 2)
        self.assertEqual(record.extra.get("future_field_not_yet_documented"), "keep-me")

    def test_minimal_older_shaped_record_does_not_raise(self):
        attributions = read_attribution_jsonl(FIXTURES / "attribution_sample.jsonl")
        record = next(a for a in attributions if a.task_id == "codex-fallback-1")
        self.assertIsNone(record.model)
        self.assertIsNone(record.context_id)
        self.assertEqual(record.context_policy, "unknown")

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(read_attribution_jsonl(FIXTURES / "does_not_exist.jsonl"), [])
        self.assertEqual(read_lifecycle_events_jsonl(FIXTURES / "does_not_exist.jsonl"), [])

    def test_unknown_event_type_is_skipped_not_raised(self):
        events = read_lifecycle_events_jsonl(FIXTURES / "lifecycle_events_sample.jsonl")
        event_types = {e.event_type for e in events}
        self.assertNotIn("context_not_yet_invented", event_types)
        self.assertIn("context_compacted", event_types)

    def test_filter_helpers(self):
        attributions = read_attribution_jsonl(FIXTURES / "attribution_sample.jsonl")
        ctx = filter_by_context(attributions, "ctx-9f21")
        self.assertEqual(len(ctx), 5)
        codex_only = filter_by_provider(attributions, "codex")
        self.assertTrue(all(a.provider == "codex" for a in codex_only))

    def test_attribution_from_dict_ignores_bad_context_policy(self):
        record = attribution_from_dict({"runtime": "agent_crew", "task_id": "x", "context_policy": "not-a-real-policy"})
        self.assertEqual(record.context_policy, "unknown")

    def test_validate_attribution_dict(self):
        errors = validate_attribution_dict({"runtime": "agent_crew"})
        self.assertTrue(any("task_id" in e for e in errors))
        self.assertEqual(validate_attribution_dict({"runtime": "agent_crew", "task_id": "t1"}), ())

    def test_lifecycle_event_from_dict_rejects_missing_timestamp(self):
        self.assertIsNone(lifecycle_event_from_dict({"event_type": "task_started", "runtime": "agent_crew"}))

    def test_attribution_round_trip(self):
        original = RuntimeAttribution(runtime="agent_crew", task_id="t1", project="p", context_policy="resume")
        restored = attribution_from_dict(attribution_to_dict(original))
        self.assertEqual(original, restored)


class CorrelateTests(unittest.TestCase):
    def setUp(self):
        self.attributions = read_attribution_jsonl(FIXTURES / "attribution_sample.jsonl")

    def test_high_confidence_when_session_and_window_match(self):
        usage = [
            ProviderUsageRecord(
                provider="claude",
                tokens=TokenComponents(fresh_input=100, output=20, cache_read=10, cache_creation=5),
                provider_session_id="sess-claude-001",
                started_at=1787000000,
                completed_at=1787000180,
            )
        ]
        target = [a for a in self.attributions if a.task_id == "claude-a1b2c3d4"]
        records = correlate_task_economics(target, usage)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].attribution_confidence, "high")
        self.assertEqual(token_components_total(records[0].tokens), 135)

    def test_medium_confidence_when_only_session_matches(self):
        usage = [
            ProviderUsageRecord(
                provider="claude",
                tokens=TokenComponents(provider_total=999),
                provider_session_id="sess-claude-001",
                started_at=1600000000,  # far outside any task window
                completed_at=1600000010,
            )
        ]
        target = [a for a in self.attributions if a.task_id == "claude-a1b2c3d4"]
        records = correlate_task_economics(target, usage)
        self.assertEqual(records[0].attribution_confidence, "medium")

    def test_low_confidence_project_heuristic_when_no_session_id(self):
        usage = [
            ProviderUsageRecord(
                provider="codex",
                tokens=TokenComponents(provider_total=42),
                provider_session_id=None,
                project="alpha-engine",
                started_at=1787001950,
                completed_at=1787001955,
            )
        ]
        target = [a for a in self.attributions if a.task_id == "codex-fallback-1"]
        records = correlate_task_economics(target, usage)
        self.assertEqual(records[0].attribution_confidence, "low")
        self.assertEqual(token_components_total(records[0].tokens), 42)

    def test_low_confidence_and_unknown_tokens_when_nothing_matches(self):
        target = [a for a in self.attributions if a.task_id == "claude-a1b2c3d4"]
        records = correlate_task_economics(target, [])
        self.assertEqual(records[0].attribution_confidence, "low")
        self.assertIsNone(token_components_total(records[0].tokens))
        self.assertIn("no matching usage telemetry found", records[0].attribution_notes[0])

    def test_one_usage_record_is_not_double_counted_across_overlapping_tasks(self):
        """Regression test: a single usage record whose (slop-widened) window touches
        several sequential tasks in the same provider session must be attributed to
        exactly one of them, not attributed in full to every task it overlaps."""

        attributions = [
            RuntimeAttribution(
                runtime="agent_crew", task_id="t1", provider="claude", provider_session_id="sess-x",
                started_at=1000, completed_at=1010,
            ),
            RuntimeAttribution(
                runtime="agent_crew", task_id="t2", provider="claude", provider_session_id="sess-x",
                started_at=1015, completed_at=1025,
            ),
            RuntimeAttribution(
                runtime="agent_crew", task_id="t3", provider="claude", provider_session_id="sess-x",
                started_at=1030, completed_at=1040,
            ),
        ]
        # One usage record sitting right in the middle -- with the +-30s slop this
        # single record's window touches all three tasks' windows above.
        usage = [
            ProviderUsageRecord(
                provider="claude",
                tokens=TokenComponents(provider_total=110),
                provider_session_id="sess-x",
                started_at=1020,
                completed_at=1020,
            )
        ]
        records = correlate_task_economics(attributions, usage)
        totals = [token_components_total(r.tokens) for r in records]
        counted = [t for t in totals if t]
        # The 110 tokens must show up for exactly one task, not all three (330).
        self.assertEqual(counted, [110])
        self.assertEqual(sum(t or 0 for t in totals), 110)
        # t2's window is closest to the record's own timestamp (1020) -- it should win.
        winner = next(r for r, t in zip(records, totals) if t)
        self.assertEqual(winner.task_id, "t2")

    def test_exclusive_assignment_does_not_leak_claimed_record_to_other_task(self):
        attributions = [
            RuntimeAttribution(
                runtime="agent_crew", task_id="winner", provider="claude", provider_session_id="sess-y",
                started_at=100, completed_at=100,
            ),
            RuntimeAttribution(
                runtime="agent_crew", task_id="loser", provider="claude", provider_session_id="sess-y",
                started_at=200, completed_at=200,
            ),
        ]
        usage = [ProviderUsageRecord(provider="claude", tokens=TokenComponents(provider_total=50), provider_session_id="sess-y", started_at=100, completed_at=105)]
        records = correlate_task_economics(attributions, usage)
        by_id = {r.task_id: r for r in records}
        self.assertEqual(by_id["winner"].attribution_confidence, "high")
        self.assertEqual(token_components_total(by_id["winner"].tokens), 50)
        # The losing task shares the session id but the only overlapping record was
        # exclusively claimed by "winner" -- it must not also get those 50 tokens.
        self.assertIsNone(token_components_total(by_id["loser"].tokens))


def _sample_economics_records() -> list[TaskEconomicsRecord]:
    attributions = read_attribution_jsonl(FIXTURES / "attribution_sample.jsonl")
    usage = [
        ProviderUsageRecord(
            provider=a.provider or "unknown",
            provider_session_id=a.provider_session_id,
            started_at=a.started_at,
            completed_at=a.completed_at,
            tokens=TokenComponents(fresh_input=50, output=10, cache_read=200, cache_creation=0, provider_total=None),
        )
        for a in attributions
        if a.provider_session_id
    ]
    return correlate_task_economics(attributions, usage)


class AnalyticsTests(unittest.TestCase):
    def test_fresh_input_per_successful_task(self):
        records = _sample_economics_records()
        value = fresh_input_per_successful_task(records)
        self.assertIsNotNone(value)
        self.assertGreater(value, 0)

    def test_cache_read_per_task(self):
        records = _sample_economics_records()
        self.assertEqual(cache_read_per_task(records), 200.0)

    def test_failed_retry_token_waste_counts_failed_and_retry(self):
        records = _sample_economics_records()
        waste = failed_retry_token_waste(records)
        # Fixture has 2 failed tasks (claude-b1c2d3e4, codex-fallback-1) and 1 retry (claude-c1d2e3f4).
        self.assertEqual(waste["failed_task_count"], 2)
        self.assertEqual(waste["retry_task_count"], 1)

    def test_tokens_per_successful_task(self):
        records = _sample_economics_records()
        self.assertIsNotNone(tokens_per_successful_task(records))

    def test_context_age_vs_token_usage_grouped_by_session_task_index(self):
        records = _sample_economics_records()
        rows = context_age_vs_token_usage(records)
        indices = [row["session_task_index"] for row in rows]
        self.assertEqual(indices, sorted(indices))
        self.assertTrue(all(row["count"] >= 1 for row in rows))

    def test_context_age_vs_failure_rate(self):
        records = _sample_economics_records()
        rows = context_age_vs_failure_rate(records)
        failing_row = next(row for row in rows if row["session_task_index"] == 2)
        self.assertEqual(failing_row["failure_rate"], 1.0)

    def test_compare_context_policies_resume_vs_fresh(self):
        records = _sample_economics_records()
        comparison = compare_context_policies(records)
        self.assertIn("resume", comparison)
        self.assertIn("fresh", comparison)

    def test_averages_are_computed_from_distinct_per_task_values_not_a_repeated_constant(self):
        """Uses hand-built records with different token totals per task -- unlike
        _sample_economics_records() (same fixed usage for every task), this actually
        exercises the arithmetic instead of just checking the aggregate is non-None."""

        records = [
            TaskEconomicsRecord(task_id="a", runtime="agent_crew", outcome="success", tokens=TokenComponents(fresh_input=100)),
            TaskEconomicsRecord(task_id="b", runtime="agent_crew", outcome="success", tokens=TokenComponents(fresh_input=300)),
            TaskEconomicsRecord(task_id="c", runtime="agent_crew", outcome="failed", tokens=TokenComponents(fresh_input=9999)),
        ]
        # (100 + 300) / 2 successful tasks -- the failed task's 9999 must not pull this average.
        self.assertEqual(fresh_input_per_successful_task(records), 200.0)

        totals = tokens_per_outcome(records)
        self.assertEqual(totals["success"]["avg_tokens"], 200.0)
        self.assertEqual(totals["failed"]["avg_tokens"], 9999.0)


class CompactAnalysisTests(unittest.TestCase):
    def test_before_after_compact_splits_by_event_timestamp(self):
        records = _sample_economics_records()
        events = read_lifecycle_events_jsonl(FIXTURES / "lifecycle_events_sample.jsonl")
        comparisons = before_after_compact(records, events, n=5)
        self.assertEqual(len(comparisons), 1)  # one context_compacted event in the fixture
        comparison = comparisons[0]
        self.assertEqual(comparison["context_id"], "ctx-9f21")
        self.assertEqual(comparison["event_type"], "context_compacted")
        # 2 tasks completed before the compact event, at least 1 after.
        self.assertEqual(comparison["before"]["task_count"], 2)
        self.assertGreaterEqual(comparison["after"]["task_count"], 1)

    def test_ignores_events_without_context_id(self):
        records = _sample_economics_records()
        from quota_core.context_economics.schema import ContextLifecycleEvent

        events = [ContextLifecycleEvent(event_type="context_compacted", runtime="agent_crew", timestamp=1787000350, context_id=None)]
        self.assertEqual(before_after_compact(records, events), [])


class BackwardCompatibilityTests(unittest.TestCase):
    def test_existing_public_snapshot_api_still_imports(self):
        # This is the guard against issue #56 accidentally breaking existing
        # public quota snapshot APIs while adding the new subpackage.
        from quota_core import NormalizedSnapshot, QuotaCoreConfig, SnapshotWindow, load_config  # noqa: F401
        from quota_core.snapshot import snapshot_to_dict, validate_snapshot_dict  # noqa: F401

    def test_context_economics_is_a_separate_opt_in_subpackage(self):
        # quota_core/__init__.py's own source must not import context_economics
        # eagerly -- callers who only need the existing snapshot API should not
        # pay for (or accidentally depend on) the new subpackage. Checking the
        # source text (rather than sys.modules/vars(quota_core)) avoids a false
        # failure caused by *this test file itself* importing context_economics
        # earlier in the same process.
        import quota_core

        assert quota_core.__file__ is not None
        source = Path(quota_core.__file__).read_text()
        self.assertNotIn("context_economics", source)


if __name__ == "__main__":
    unittest.main()
