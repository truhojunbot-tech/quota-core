"""Tests for quota-core issue #60: failure cause/classification.

Preserves raw failure reason alongside a small public failure-category set
(context_or_policy / provider_or_transport / runtime_or_dispatcher /
work_product_or_test / cancelled / unknown) and stratifies context-age and
resume/compact/fresh failure-rate reporting by it, so a provider outage or
dispatcher bug (the real agent_crew#205/#206 AGY "subscriber fell behind"
incident) does not get silently read as evidence that older/compacted
context caused a task to fail.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from quota_core.context_economics import (
    ProviderUsageRecord,
    RuntimeAttribution,
    TaskEconomicsRecord,
    TokenComponents,
    UNKNOWN_FAILURE_CAUSE_WARNING,
    attribution_from_dict,
    attribution_to_dict,
    classify_failure_category,
    compare_context_policies,
    context_age_vs_failure_rate,
    correlate_task_economics,
    extract_failure_reason,
    infer_retryable,
    normalize_outcome,
    stratified_failure_rates,
    validate_attribution_dict,
)
from quota_core.context_economics.agent_crew_adapter import read_attribution_jsonl

FIXTURES = Path(__file__).parent / "fixtures" / "agent_crew"
AGY_FIXTURE = FIXTURES / "agy_incident" / "attribution.jsonl"


class ExtractFailureReasonTests(unittest.TestCase):
    def test_colon_delimited_reason_is_extracted(self):
        self.assertEqual(extract_failure_reason("failed:dispatcher_timeout"), "dispatcher_timeout")

    def test_bare_failed_has_no_reason(self):
        self.assertIsNone(extract_failure_reason("failed"))

    def test_missing_raw_outcome_has_no_reason(self):
        self.assertIsNone(extract_failure_reason(None))
        self.assertIsNone(extract_failure_reason(""))


class ClassifyFailureCategoryTests(unittest.TestCase):
    """Grounded in reason strings actually observed locally in ~/.agent_crew/*/
    tasks.db error_info / attribution.jsonl outcome (dispatcher_timeout, exit_1,
    agy_quota_exhausted, no_result_submitted) plus agent_crew's own dispatcher
    transient-error tags (agy_timeout, claude_429, gemini_capacity, ...)."""

    def test_non_failed_outcome_has_no_category(self):
        self.assertIsNone(classify_failure_category("success", "completed"))
        self.assertIsNone(classify_failure_category(None, ""))
        self.assertIsNone(classify_failure_category("unknown", "some_future_state"))

    def test_bare_failed_with_no_reason_is_unknown(self):
        self.assertEqual(classify_failure_category("failed", "failed"), "unknown")

    def test_unrecognized_reason_is_unknown_not_guessed(self):
        self.assertEqual(classify_failure_category("failed", "failed:totally_novel_thing"), "unknown")

    def test_dispatcher_timeout_is_runtime_or_dispatcher(self):
        # Real observed value: ~/.agent_crew/agent_crew/tasks.db error_info
        # {"reason": "dispatcher_timeout"}, and the matching real
        # attribution.jsonl outcome "failed:dispatcher_timeout" (see
        # tests/fixtures/agent_crew/real_contract/).
        self.assertEqual(classify_failure_category("failed", "failed:dispatcher_timeout"), "runtime_or_dispatcher")

    def test_no_result_submitted_is_runtime_or_dispatcher(self):
        # Real observed value: ~/.agent_crew/alpha_engine/tasks.db error_info.
        self.assertEqual(classify_failure_category("failed", "failed:no_result_submitted"), "runtime_or_dispatcher")

    def test_exit_1_is_work_product_or_test(self):
        # Real observed value: ~/.agent_crew/alpha_engine/tasks.db error_info
        # (by far the most common failure reason there).
        self.assertEqual(classify_failure_category("failed", "failed:exit_1"), "work_product_or_test")

    def test_agy_quota_exhausted_is_provider_or_transport(self):
        # Real observed value: ~/.agent_crew/alpha_engine/tasks.db error_info.
        self.assertEqual(classify_failure_category("failed", "failed:agy_quota_exhausted"), "provider_or_transport")

    def test_agy_timeout_is_provider_or_transport(self):
        # Not observed in local task-level error_info, but this is exactly
        # one of agent_crew's own dispatcher transient-error tags (see
        # agent_crew's tests/unit/test_transient_error_detection.py,
        # _detect_transient_error_in_log) -- grouped with the other
        # upstream-throttle signatures agent_crew already requeues rather
        # than failing permanently.
        self.assertEqual(classify_failure_category("failed", "failed:agy_timeout"), "provider_or_transport")

    def test_provider_throttle_markers_are_provider_or_transport(self):
        for reason in (
            "failed:claude_429",
            "failed:claude_throttle",
            "failed:gemini_capacity",
            "failed:gemini_resource_exhausted",
            "failed:codex_capacity",
        ):
            with self.subTest(reason=reason):
                self.assertEqual(classify_failure_category("failed", reason), "provider_or_transport")

    def test_agy_subscriber_lag_is_provider_or_transport_not_context_or_policy(self):
        """Regression for the agent_crew#205/#206 AGY incident (issue #60's
        motivating case): a transient streaming/backpressure error must never
        classify as context/policy evidence."""

        category = classify_failure_category("failed", "failed:agy_subscriber_lag")
        self.assertEqual(category, "provider_or_transport")
        self.assertNotEqual(category, "context_or_policy")

    def test_cancelled_outcome_is_cancelled_category(self):
        self.assertEqual(classify_failure_category("failed", "cancelled"), "cancelled")
        self.assertEqual(classify_failure_category("failed", "failed:job_cancelled_by_user"), "cancelled")


class InferRetryableTests(unittest.TestCase):
    def test_provider_or_transport_is_retryable(self):
        self.assertTrue(infer_retryable("provider_or_transport"))

    def test_unknown_is_neither_true_nor_false(self):
        self.assertIsNone(infer_retryable("unknown"))
        self.assertIsNone(infer_retryable(None))

    def test_other_known_categories_are_not_retryable(self):
        for category in ("runtime_or_dispatcher", "work_product_or_test", "cancelled", "context_or_policy"):
            with self.subTest(category=category):
                self.assertFalse(infer_retryable(category))  # type: ignore[arg-type]


class AttributionDerivationTests(unittest.TestCase):
    """attribution_from_dict derives failure_reason/failure_category/retryable
    from outcome when the source dict doesn't supply them explicitly."""

    def test_real_shaped_failure_is_classified_on_parse(self):
        record = attribution_from_dict(
            {"runtime": "agent_crew", "task_id": "t1", "outcome": "failed:dispatcher_timeout"}
        )
        self.assertEqual(record.failure_reason, "dispatcher_timeout")
        self.assertEqual(record.failure_category, "runtime_or_dispatcher")
        self.assertFalse(record.retryable)

    def test_explicit_fields_win_over_derived_ones(self):
        record = attribution_from_dict(
            {
                "runtime": "agent_crew",
                "task_id": "t1",
                "outcome": "failed:exit_1",
                "failure_category": "context_or_policy",
                "retryable": True,
                "terminal_source": "provider",
            }
        )
        # A future producer's explicit classification is trusted over the
        # locally-derived one from the raw reason string.
        self.assertEqual(record.failure_category, "context_or_policy")
        self.assertTrue(record.retryable)
        self.assertEqual(record.terminal_source, "provider")

    def test_terminal_source_stays_none_when_not_supplied(self):
        # Agent Crew's current real contract has no terminal_source field at
        # all -- must not be fabricated from the failure category.
        record = attribution_from_dict({"runtime": "agent_crew", "task_id": "t1", "outcome": "failed:agy_quota_exhausted"})
        self.assertIsNone(record.terminal_source)

    def test_invalid_explicit_failure_category_falls_back_to_derived(self):
        record = attribution_from_dict(
            {"runtime": "agent_crew", "task_id": "t1", "outcome": "failed:exit_1", "failure_category": "not_a_real_category"}
        )
        self.assertEqual(record.failure_category, "work_product_or_test")


class BackwardCompatibilityTests(unittest.TestCase):
    """Older telemetry with only outcome=failed (no reason) must still parse
    and classify as unknown, not disappear (issue #60 acceptance criteria)."""

    def test_bare_outcome_failed_still_parses(self):
        record = attribution_from_dict({"runtime": "agent_crew", "task_id": "old-task", "outcome": "failed"})
        self.assertEqual(record.outcome, "failed")
        self.assertEqual(record.raw_outcome, "failed")
        self.assertIsNone(record.failure_reason)
        self.assertEqual(record.failure_category, "unknown")
        self.assertIsNone(record.retryable)

    def test_record_with_no_outcome_at_all_has_no_category(self):
        record = attribution_from_dict({"runtime": "agent_crew", "task_id": "in-progress-task"})
        self.assertIsNone(record.outcome)
        self.assertIsNone(record.failure_category)

    def test_attribution_round_trip_preserves_new_fields(self):
        original = RuntimeAttribution(
            runtime="agent_crew",
            task_id="t1",
            outcome="failed",
            raw_outcome="failed:exit_1",
            failure_reason="exit_1",
            failure_category="work_product_or_test",
            retryable=False,
            terminal_source="agent_reported",
        )
        restored = attribution_from_dict(attribution_to_dict(original))
        self.assertEqual(original, restored)

    def test_validate_attribution_dict_rejects_bad_failure_category(self):
        errors = validate_attribution_dict(
            {"runtime": "agent_crew", "task_id": "t1", "failure_category": "not_a_real_category"}
        )
        self.assertTrue(any("failure_category" in e for e in errors))

    def test_validate_attribution_dict_rejects_non_bool_retryable(self):
        errors = validate_attribution_dict({"runtime": "agent_crew", "task_id": "t1", "retryable": "yes"})
        self.assertTrue(any("retryable" in e for e in errors))


def _agy_incident_records() -> list[TaskEconomicsRecord]:
    attributions = read_attribution_jsonl(AGY_FIXTURE)
    usage = [
        ProviderUsageRecord(
            provider=a.provider or "unknown",
            provider_session_id=a.provider_session_id,
            started_at=a.started_at,
            completed_at=a.completed_at,
            tokens=TokenComponents(fresh_input=100, output=20, provider_total=120),
        )
        for a in attributions
    ]
    return correlate_task_economics(attributions, usage)


class AgyIncidentRegressionTests(unittest.TestCase):
    """tests/fixtures/agent_crew/agy_incident/attribution.jsonl models the
    real agent_crew#205/#206 incident: transient AGY streaming/backpressure
    failures must not be interpreted as context-policy evidence."""

    def test_fixture_reads_all_twelve_tasks(self):
        records = _agy_incident_records()
        self.assertEqual(len(records), 12)

    def test_agy_subscriber_lag_tasks_classify_as_provider_or_transport(self):
        records = _agy_incident_records()
        agy_failures = [r for r in records if r.raw_outcome == "failed:agy_subscriber_lag"]
        self.assertEqual(len(agy_failures), 4)
        for r in agy_failures:
            self.assertEqual(r.failure_category, "provider_or_transport")
            self.assertTrue(r.retryable)

    def test_stratified_rates_exclude_agy_incident_from_policy_relevant(self):
        records = _agy_incident_records()
        rates = stratified_failure_rates(records)
        # Raw: 6/12 failed (4 agy_subscriber_lag + 1 exit_1 + 1 bare failed).
        self.assertEqual(rates["raw_failure_count"], 6)
        self.assertAlmostEqual(rates["raw_failure_rate"], 0.5)
        # The 4 AGY incident failures land in provider_or_runtime_operational,
        # not policy_relevant -- this is the core regression this fixture
        # exists to catch.
        self.assertEqual(rates["provider_or_runtime_operational_count"], 4)
        # Only the exit_1 failure is policy-relevant (a real work-product failure).
        self.assertEqual(rates["policy_relevant_count"], 1)
        # The bare "failed" (no reason) task is unknown, not silently dropped.
        self.assertEqual(rates["unknown_count"], 1)
        self.assertEqual(
            rates["provider_or_runtime_operational_count"] + rates["policy_relevant_count"] + rates["unknown_count"] + rates["cancelled_count"],
            rates["raw_failure_count"],
        )

    def test_context_age_vs_failure_rate_does_not_read_as_context_aging_evidence(self):
        records = _agy_incident_records()
        rows = context_age_vs_failure_rate(records)
        by_age = {row["session_task_index"]: row for row in rows}
        # session_task_index 4, 5, 7, 9 are the AGY-incident failures: raw
        # failure_rate is 1.0 (single observation each), but
        # policy_relevant_failure_rate must be 0.0 -- a naive reader of raw
        # failure_rate alone would wrongly conclude context aging got worse
        # at these indices.
        for age in (4, 5, 7, 9):
            with self.subTest(session_task_index=age):
                self.assertEqual(by_age[age]["failure_rate"], 1.0)
                self.assertEqual(by_age[age]["policy_relevant_failure_rate"], 0.0)
                self.assertEqual(by_age[age]["provider_or_runtime_operational_failure_rate"], 1.0)
        # session_task_index 11 (exit_1) genuinely is policy-relevant.
        self.assertEqual(by_age[11]["policy_relevant_failure_rate"], 1.0)

    def test_compare_context_policies_exposes_the_same_stratification(self):
        records = _agy_incident_records()
        comparison = compare_context_policies(records)
        resume = comparison["resume"]
        self.assertEqual(resume["count"], 12)
        self.assertEqual(resume["raw_failure_count"], 6)
        self.assertEqual(resume["provider_or_runtime_operational_count"], 4)
        self.assertEqual(resume["policy_relevant_count"], 1)
        self.assertNotIn("warning", resume)


class UnknownCauseWarningTests(unittest.TestCase):
    """Acceptance criteria: resume/compact/fresh comparison exposes an
    explicit warning when failure causes are unavailable."""

    def test_warning_present_when_every_failure_is_unclassified(self):
        records = [
            TaskEconomicsRecord(task_id="a", runtime="agent_crew", context_policy="fresh", outcome="success"),
            TaskEconomicsRecord(task_id="b", runtime="agent_crew", context_policy="fresh", outcome="failed", raw_outcome="failed"),
            TaskEconomicsRecord(task_id="c", runtime="agent_crew", context_policy="fresh", outcome="failed", raw_outcome="failed"),
        ]
        comparison = compare_context_policies(records)
        self.assertIn("warning", comparison["fresh"])
        self.assertEqual(comparison["fresh"]["warning"], UNKNOWN_FAILURE_CAUSE_WARNING)

    def test_no_warning_when_at_least_one_failure_is_classified(self):
        records = [
            TaskEconomicsRecord(
                task_id="a", runtime="agent_crew", context_policy="fresh",
                outcome="failed", raw_outcome="failed:exit_1", failure_category="work_product_or_test",
            ),
            TaskEconomicsRecord(task_id="b", runtime="agent_crew", context_policy="fresh", outcome="failed", raw_outcome="failed"),
        ]
        comparison = compare_context_policies(records)
        self.assertNotIn("warning", comparison["fresh"])

    def test_no_warning_when_there_are_no_failures_at_all(self):
        records = [TaskEconomicsRecord(task_id="a", runtime="agent_crew", context_policy="fresh", outcome="success")]
        comparison = compare_context_policies(records)
        self.assertNotIn("warning", comparison["fresh"])


if __name__ == "__main__":
    unittest.main()
