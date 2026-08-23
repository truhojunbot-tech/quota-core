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

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quota_core.context_economics import (
    ProviderUsageRecord,
    RuntimeAttribution,
    TaskEconomicsRecord,
    TokenComponents,
    PARTIAL_UNKNOWN_FAILURE_CAUSE_WARNING,
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
from quota_core.context_economics.agent_crew_adapter import (
    enrich_with_task_error_reasons,
    read_attribution_jsonl,
    read_attribution_jsonl_with_task_errors,
    read_task_error_reasons,
)

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

    def test_exit_1_is_unknown_not_work_product(self):
        # Round-1 review of issue #60: a bare/generic exit code is
        # evidence-free -- a crash, an OOM kill, and a genuine failing test
        # all produce the same bare exit_1/exit_code_137, so it must not
        # positively identify a "work product" (test/lint/assertion)
        # failure. "unknown" is the honest bucket, per the issue's own "do
        # not fabricate a category when evidence is insufficient" rule.
        self.assertEqual(classify_failure_category("failed", "failed:exit_1"), "unknown")

    def test_other_bare_exit_codes_are_also_unknown(self):
        # exit_code_137 (SIGKILL/OOM) must not be misfiled as work_product_or_test.
        self.assertEqual(classify_failure_category("failed", "failed:exit_code_137"), "unknown")
        self.assertEqual(classify_failure_category("failed", "failed:exit_1_agy_transient"), "unknown")

    def test_positively_identified_test_failure_is_work_product_or_test(self):
        # work_product_or_test is reserved for reasons that positively
        # identify a test/lint/assertion failure -- none are reachable from
        # real data yet, but the marker still exists for a future producer.
        self.assertEqual(classify_failure_category("failed", "failed:test_fail"), "work_product_or_test")

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
    """Round-1 review of issue #60: infer_retryable must mirror agent_crew's
    own dispatcher tag lists (_detect_transient_error_in_log) exactly, not
    the coarser failure_category -- deriving from category alone got
    agy_quota_exhausted (78% of every real observed local failure reason)
    wrong, since it shares a category with genuinely-retryable tags."""

    def test_dispatcher_retryable_tags_are_retryable(self):
        for reason in (
            "claude_429",
            "claude_throttle",
            "gemini_capacity",
            "gemini_resource_exhausted",
            "codex_capacity",
            "agy_timeout",
            "agy_subscriber_lag",
        ):
            with self.subTest(reason=reason):
                self.assertTrue(infer_retryable(reason))

    def test_dispatcher_nonretryable_tags_are_not_retryable(self):
        # agent_crew's own dispatcher comment calls this group "clear
        # reason; no point in immediate retry" -- agy_quota_exhausted alone
        # is 321/413 (78%) of every real observed local failure reason, so
        # this is the single highest-stakes case to get right.
        for reason in ("gemini_quota_exhausted", "gemini_ineligible_tier", "agy_quota_exhausted"):
            with self.subTest(reason=reason):
                self.assertFalse(infer_retryable(reason))

    def test_unrecognized_reason_is_neither_true_nor_false(self):
        self.assertIsNone(infer_retryable("dispatcher_timeout"))
        self.assertIsNone(infer_retryable("no_result_submitted"))
        self.assertIsNone(infer_retryable("exit_1"))
        self.assertIsNone(infer_retryable("totally_novel_thing"))
        self.assertIsNone(infer_retryable(None))
        self.assertIsNone(infer_retryable(""))

    def test_max_retries_suffixed_reason_is_not_retryable_even_if_tag_matches(self):
        # Real observed tasks.db error_info reasons: the dispatcher's own
        # retry loop already gave up on these -- the reason string itself
        # documents that, so claiming retryable=True would contradict the
        # evidence it's derived from, even though "agy_timeout"/
        # "agy_subscriber_lag" would otherwise match the retryable list.
        self.assertFalse(infer_retryable("transient_agy_timeout_max_retries"))
        self.assertFalse(infer_retryable("transient_agy_subscriber_lag_max_retries"))


class AttributionDerivationTests(unittest.TestCase):
    """attribution_from_dict derives failure_reason/failure_category/retryable
    from outcome when the source dict doesn't supply them explicitly."""

    def test_real_shaped_failure_is_classified_on_parse(self):
        record = attribution_from_dict(
            {"runtime": "agent_crew", "task_id": "t1", "outcome": "failed:dispatcher_timeout"}
        )
        self.assertEqual(record.failure_reason, "dispatcher_timeout")
        self.assertEqual(record.failure_category, "runtime_or_dispatcher")
        # dispatcher_timeout is agent_crew's own orchestration layer giving
        # up -- it is not one of the dispatcher's two explicit retry-tag
        # lists (see InferRetryableTests), so retryable is honestly unknown,
        # not guessed False.
        self.assertIsNone(record.retryable)

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
        self.assertEqual(record.failure_category, "unknown")

    def test_explicit_null_retryable_is_respected_not_derived(self):
        # Round-1 review of issue #60: validate_attribution_dict explicitly
        # allows null for retryable/failure_reason/failure_category, but the
        # old `if not isinstance(retryable, bool): retryable = infer(...)`
        # check could not distinguish "key absent" from "key explicitly
        # null" -- an explicit null was silently overwritten with a guess.
        record = attribution_from_dict(
            {
                "runtime": "agent_crew",
                "task_id": "t1",
                "outcome": "failed:agy_quota_exhausted",
                "retryable": None,
                "failure_reason": None,
                "failure_category": None,
            }
        )
        self.assertIsNone(record.retryable)
        self.assertIsNone(record.failure_reason)
        self.assertIsNone(record.failure_category)

    def test_absent_keys_still_derive_as_before(self):
        # Same outcome, but the three keys are simply absent (the real
        # producer shape) -- must still derive, unlike the explicit-null case above.
        record = attribution_from_dict({"runtime": "agent_crew", "task_id": "t1", "outcome": "failed:agy_quota_exhausted"})
        self.assertFalse(record.retryable)
        self.assertEqual(record.failure_reason, "agy_quota_exhausted")
        self.assertEqual(record.failure_category, "provider_or_transport")


class RoundTripFidelityTests(unittest.TestCase):
    """Round-1 review of issue #60: attribution_to_dict/attribution_from_dict
    must only derive a field when its key is *absent*, never when it is
    present-and-null, and must never fabricate a sibling field just because
    an unrelated field was explicitly set."""

    def test_raw_outcome_explicit_none_survives_round_trip_with_failed_outcome(self):
        original = RuntimeAttribution(runtime="agent_crew", task_id="t1", outcome="failed", raw_outcome=None)
        restored = attribution_from_dict(attribution_to_dict(original))
        self.assertIsNone(restored.raw_outcome)

    def test_raw_outcome_explicit_none_survives_round_trip_with_success_outcome(self):
        original = RuntimeAttribution(runtime="agent_crew", task_id="t1", outcome="success", raw_outcome=None)
        restored = attribution_from_dict(attribution_to_dict(original))
        self.assertIsNone(restored.raw_outcome)

    def test_only_terminal_source_set_does_not_fabricate_siblings(self):
        original = RuntimeAttribution(runtime="agent_crew", task_id="t1", terminal_source="agent_reported")
        restored = attribution_from_dict(attribution_to_dict(original))
        self.assertEqual(restored, original)
        self.assertIsNone(restored.failure_reason)
        self.assertIsNone(restored.failure_category)
        self.assertIsNone(restored.retryable)

    def test_real_producer_shape_without_raw_outcome_key_still_derives(self):
        # Real Agent Crew producer dicts never set "raw_outcome" at all --
        # this must still fall back to deriving it from "outcome".
        record = attribution_from_dict({"runtime": "agent_crew", "task_id": "t1", "outcome": "failed:dispatcher_timeout"})
        self.assertEqual(record.raw_outcome, "failed:dispatcher_timeout")

    def test_outcome_itself_survives_round_trip_with_no_raw_outcome(self):
        # Round-2 review regression: round-1's raw_outcome fix went too far
        # and also stopped populating `outcome` correctly -- a directly
        # constructed record with only `outcome` set (no `raw_outcome`, the
        # shape attribution_to_dict emits for a record that never had a raw
        # source) came back as outcome=None after a to_dict/from_dict cycle,
        # silently turning a terminal failure into "in progress". Covers
        # both terminal values plus the genuinely-in-progress None case.
        for outcome in ("failed", "success", "unknown", None):
            with self.subTest(outcome=outcome):
                original = RuntimeAttribution(runtime="agent_crew", task_id="t1", outcome=outcome)
                restored = attribution_from_dict(attribution_to_dict(original))
                self.assertEqual(restored.outcome, outcome)
                self.assertIsNone(restored.raw_outcome)

    def test_outcome_only_record_round_trips_identically(self):
        # The exact repro from round-2 review: construct a record with only
        # `outcome` set, nothing else, and confirm the full record survives
        # a round trip unchanged.
        original = RuntimeAttribution(runtime="agent_crew", task_id="t1", outcome="failed")
        restored = attribution_from_dict(attribution_to_dict(original))
        self.assertEqual(restored, original)


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
        # No failure in this fixture positively identifies a context/policy
        # cause -- policy_relevant stays 0. exit_1 is a bare, evidence-free
        # exit code (round-1 review of issue #60): it is neither
        # work_product_or_test nor policy_relevant, it's unknown.
        self.assertEqual(rates["policy_relevant_count"], 0)
        self.assertEqual(rates["work_product_or_test_count"], 0)
        # The bare "failed" (no reason) task AND the exit_1 task are both
        # unknown, not silently dropped or misfiled as work-product evidence.
        self.assertEqual(rates["unknown_count"], 2)
        self.assertEqual(
            rates["provider_or_runtime_operational_count"]
            + rates["work_product_or_test_count"]
            + rates["policy_relevant_count"]
            + rates["unknown_count"]
            + rates["cancelled_count"],
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
        # session_task_index 11 (exit_1) is a bare exit code -- evidence-free,
        # so it is unknown, not policy-relevant (round-1 review of issue #60;
        # previously this asserted the opposite, which was the exact "false
        # precision" bug the review caught).
        self.assertEqual(by_age[11]["policy_relevant_failure_rate"], 0.0)
        self.assertEqual(by_age[11]["unknown_failure_rate"], 1.0)

    def test_compare_context_policies_exposes_the_same_stratification(self):
        records = _agy_incident_records()
        comparison = compare_context_policies(records)
        resume = comparison["resume"]
        self.assertEqual(resume["count"], 12)
        self.assertEqual(resume["raw_failure_count"], 6)
        self.assertEqual(resume["provider_or_runtime_operational_count"], 4)
        self.assertEqual(resume["policy_relevant_count"], 0)
        self.assertEqual(resume["unknown_count"], 2)
        # 2 of 6 failures unknown (33%) is below the 50% partial-warning
        # threshold, so no warning here -- see UnknownCauseWarningTests for
        # the >=50% case.
        self.assertNotIn("warning", resume)


class UnknownCauseWarningTests(unittest.TestCase):
    """Acceptance criteria: resume/compact/fresh comparison exposes an
    explicit warning when failure causes are unavailable -- including,
    since round-1 review of issue #60, when they are only *mostly*
    unavailable, not just when they are entirely unavailable."""

    def test_warning_present_when_every_failure_is_unclassified(self):
        records = [
            TaskEconomicsRecord(task_id="a", runtime="agent_crew", context_policy="fresh", outcome="success"),
            TaskEconomicsRecord(task_id="b", runtime="agent_crew", context_policy="fresh", outcome="failed", raw_outcome="failed"),
            TaskEconomicsRecord(task_id="c", runtime="agent_crew", context_policy="fresh", outcome="failed", raw_outcome="failed"),
        ]
        comparison = compare_context_policies(records)
        self.assertIn("warning", comparison["fresh"])
        self.assertEqual(comparison["fresh"]["warning"], UNKNOWN_FAILURE_CAUSE_WARNING)

    def test_partial_warning_present_when_at_least_half_unclassified(self):
        # Round-1 review of issue #60: the original warning was all-or-
        # nothing, so a group that is e.g. 50%+ (but not 100%) unknown
        # reported a confident-looking policy_relevant_failure_rate with no
        # warning at all. 1 of 2 failures classified, 1 unknown -- exactly
        # the 50% threshold.
        records = [
            TaskEconomicsRecord(
                task_id="a", runtime="agent_crew", context_policy="fresh",
                outcome="failed", raw_outcome="failed:test_fail", failure_category="work_product_or_test",
            ),
            TaskEconomicsRecord(task_id="b", runtime="agent_crew", context_policy="fresh", outcome="failed", raw_outcome="failed"),
        ]
        comparison = compare_context_policies(records)
        self.assertIn("warning", comparison["fresh"])
        self.assertEqual(comparison["fresh"]["warning"], PARTIAL_UNKNOWN_FAILURE_CAUSE_WARNING)

    def test_no_warning_when_most_failures_are_classified(self):
        records = [
            TaskEconomicsRecord(
                task_id="a", runtime="agent_crew", context_policy="fresh",
                outcome="failed", raw_outcome="failed:test_fail", failure_category="work_product_or_test",
            ),
            TaskEconomicsRecord(
                task_id="b", runtime="agent_crew", context_policy="fresh",
                outcome="failed", raw_outcome="failed:agy_quota_exhausted", failure_category="provider_or_transport",
            ),
            TaskEconomicsRecord(task_id="c", runtime="agent_crew", context_policy="fresh", outcome="failed", raw_outcome="failed"),
        ]
        comparison = compare_context_policies(records)
        self.assertNotIn("warning", comparison["fresh"])

    def test_no_warning_when_there_are_no_failures_at_all(self):
        records = [TaskEconomicsRecord(task_id="a", runtime="agent_crew", context_policy="fresh", outcome="success")]
        comparison = compare_context_policies(records)
        self.assertNotIn("warning", comparison["fresh"])


def _write_jsonl(rows: list[dict]) -> Path:
    """Write a minimal real-shaped ``attribution.jsonl`` file and return its path."""

    tmp_dir = Path(tempfile.mkdtemp())
    path = tmp_dir / "attribution.jsonl"
    with path.open("w", encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row) + "\n")
    return path


def _build_tasks_db(rows: list[tuple[str, str, dict | None]]) -> Path:
    """Build a minimal real-shaped tasks.db: (task_id, status, error_info-dict-or-None)."""

    tmp_dir = Path(tempfile.mkdtemp())
    db_path = tmp_dir / "tasks.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT, error_info TEXT)"
    )
    for task_id, status, error_info in rows:
        conn.execute(
            "INSERT INTO tasks (task_id, status, error_info) VALUES (?, ?, ?)",
            (task_id, status, json.dumps(error_info) if error_info is not None else None),
        )
    conn.commit()
    conn.close()
    return db_path


class TasksDbEnrichmentTests(unittest.TestCase):
    """quota-core issue #60 round-1 review: the adapter must also read
    tasks.db's error_info -- real attribution.jsonl reasons alone left the
    vast majority of real observed local failures unclassified, because most
    never got a terminal outcome row written to attribution.jsonl at all
    (321 of 413 real observed tasks.db error_info rows locally are exactly
    this shape: status=failed with a reason, but no attribution.jsonl
    terminal row)."""

    def test_read_task_error_reasons_parses_json_reason_column(self):
        db_path = _build_tasks_db([
            ("t1", "failed", {"reason": "agy_quota_exhausted"}),
            ("t2", "done", None),
            ("t3", "failed", {"reason": "exit_1"}),
        ])
        reasons = read_task_error_reasons(db_path)
        self.assertEqual(reasons, {"t1": "agy_quota_exhausted", "t3": "exit_1"})

    def test_read_task_error_reasons_missing_db_returns_empty_dict_not_raise(self):
        self.assertEqual(read_task_error_reasons(Path("/nonexistent/tasks.db")), {})

    def test_read_task_error_reasons_malformed_error_info_is_skipped(self):
        db_path = _build_tasks_db([("t1", "failed", None)])
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE tasks SET error_info = 'not valid json' WHERE task_id = 't1'")
        conn.commit()
        conn.close()
        self.assertEqual(read_task_error_reasons(db_path), {})

    def test_enrich_backfills_outcome_when_attribution_never_terminated(self):
        """The single highest-yield real case: attribution.jsonl's own
        stream never wrote a terminal row for this task (outcome=None,
        "still in progress"), but tasks.db's dispatcher already marked it
        status=failed with a reason. Must not stay silently invisible."""

        db_path = _build_tasks_db([("t1", "failed", {"reason": "agy_quota_exhausted"})])
        attributions = [RuntimeAttribution(runtime="agent_crew", task_id="t1")]
        enriched = enrich_with_task_error_reasons(attributions, db_path)
        self.assertEqual(len(enriched), 1)
        record = enriched[0]
        self.assertEqual(record.outcome, "failed")
        self.assertEqual(record.failure_reason, "agy_quota_exhausted")
        self.assertEqual(record.failure_category, "provider_or_transport")
        self.assertFalse(record.retryable)
        self.assertEqual(record.extra.get("outcome_source"), "tasks_db_status")

    def test_enrich_does_not_backfill_when_tasks_db_status_is_not_failed(self):
        # tasks.db shows this task eventually succeeded (status != "failed")
        # even though it briefly carried an error_info from an earlier
        # attempt -- must not be turned into a fabricated failure.
        db_path = _build_tasks_db([("t1", "done", {"reason": "exit_1"})])
        attributions = [RuntimeAttribution(runtime="agent_crew", task_id="t1")]
        enriched = enrich_with_task_error_reasons(attributions, db_path)
        self.assertIsNone(enriched[0].outcome)

    def test_enrich_never_overrides_an_explicit_non_failed_outcome(self):
        db_path = _build_tasks_db([("t1", "failed", {"reason": "agy_quota_exhausted"})])
        attributions = [RuntimeAttribution(runtime="agent_crew", task_id="t1", outcome="success", raw_outcome="completed")]
        enriched = enrich_with_task_error_reasons(attributions, db_path)
        self.assertEqual(enriched[0].outcome, "success")
        self.assertIsNone(enriched[0].failure_reason)

    def test_enrich_prefers_richer_reason_over_bare_attribution_reason(self):
        db_path = _build_tasks_db([("t1", "failed", {"reason": "agy_quota_exhausted"})])
        attributions = [
            RuntimeAttribution(
                runtime="agent_crew", task_id="t1", outcome="failed", raw_outcome="failed:exit_1",
                failure_reason="exit_1", failure_category="unknown", retryable=None,
            )
        ]
        enriched = enrich_with_task_error_reasons(attributions, db_path)
        record = enriched[0]
        self.assertEqual(record.failure_reason, "agy_quota_exhausted")
        self.assertEqual(record.failure_category, "provider_or_transport")
        self.assertFalse(record.retryable)
        self.assertEqual(record.extra.get("failure_reason_source"), "tasks_db")

    def test_enrich_leaves_record_unchanged_when_reasons_already_match(self):
        db_path = _build_tasks_db([("t1", "failed", {"reason": "exit_1"})])
        original = RuntimeAttribution(
            runtime="agent_crew", task_id="t1", outcome="failed", raw_outcome="failed:exit_1",
            failure_reason="exit_1", failure_category="unknown",
        )
        enriched = enrich_with_task_error_reasons([original], db_path)
        self.assertEqual(enriched[0], original)

    def test_enrich_is_a_no_op_when_db_is_missing_not_a_hard_dependency(self):
        attributions = [RuntimeAttribution(runtime="agent_crew", task_id="t1", outcome="failed", raw_outcome="failed:exit_1")]
        enriched = enrich_with_task_error_reasons(attributions, Path("/nonexistent/tasks.db"))
        self.assertEqual(enriched, attributions)

    def test_read_attribution_jsonl_with_task_errors_end_to_end(self):
        db_path = _build_tasks_db([("agy-tester-11", "failed", {"reason": "agy_quota_exhausted"})])
        records = read_attribution_jsonl_with_task_errors(AGY_FIXTURE, db_path)
        record = next(r for r in records if r.task_id == "agy-tester-11")
        # Fixture's own attribution.jsonl already reports this task as
        # failed:exit_1 -- tasks.db's richer agy_quota_exhausted must win.
        self.assertEqual(record.failure_reason, "agy_quota_exhausted")
        self.assertEqual(record.failure_category, "provider_or_transport")

    def test_enrich_backfills_success_when_attribution_never_terminated(self):
        """Round-2 review: the symmetric counterpart to
        test_enrich_backfills_outcome_when_attribution_never_terminated.
        Round-1's fix only ever backfilled outcome="failed" from tasks.db,
        never outcome="success" from status="completed" -- on real local
        data that asymmetry took the failure rate from an ~11% jsonl-only
        understatement to a ~75% overstatement (worse than the original
        bug), because every newly-visible failure entered the numerator
        while the far more numerous newly-visible successes never entered
        the denominator."""

        db_path = _build_tasks_db([("t1", "completed", None)])
        attributions = [RuntimeAttribution(runtime="agent_crew", task_id="t1")]
        enriched = enrich_with_task_error_reasons(attributions, db_path)
        self.assertEqual(len(enriched), 1)
        record = enriched[0]
        self.assertEqual(record.outcome, "success")
        self.assertEqual(record.raw_outcome, "completed")
        self.assertEqual(record.extra.get("outcome_source"), "tasks_db_status")
        self.assertIsNone(record.failure_reason)
        self.assertIsNone(record.failure_category)

    def test_enrich_success_backfill_never_overrides_an_explicit_outcome(self):
        db_path = _build_tasks_db([("t1", "completed", None)])
        attributions = [
            RuntimeAttribution(runtime="agent_crew", task_id="t1", outcome="failed", raw_outcome="failed:exit_1")
        ]
        enriched = enrich_with_task_error_reasons(attributions, db_path)
        self.assertEqual(enriched[0].outcome, "failed")

    def test_enrich_does_not_guess_outcome_for_ambiguous_statuses(self):
        # needs_human/pending/blocked/in_progress are real observed
        # tasks.db statuses that are not positive evidence of either a
        # success or a failure -- must not be backfilled either way.
        for status in ("needs_human", "pending", "blocked", "in_progress"):
            with self.subTest(status=status):
                db_path = _build_tasks_db([("t1", status, None)])
                attributions = [RuntimeAttribution(runtime="agent_crew", task_id="t1")]
                enriched = enrich_with_task_error_reasons(attributions, db_path)
                self.assertIsNone(enriched[0].outcome)

    def test_enrich_backfills_failed_even_without_an_error_info_reason(self):
        # Real local data has tasks.db rows with status="failed" but no
        # error_info reason at all (49 of 465 real observed failed rows,
        # alongside 416 that do carry one) -- these must still backfill
        # outcome, just without a reason, rather than being skipped because
        # they're absent from the error_info-only read.
        db_path = _build_tasks_db([("t1", "failed", None)])
        attributions = [RuntimeAttribution(runtime="agent_crew", task_id="t1")]
        enriched = enrich_with_task_error_reasons(attributions, db_path)
        record = enriched[0]
        self.assertEqual(record.outcome, "failed")
        self.assertEqual(record.raw_outcome, "failed")
        self.assertIsNone(record.failure_reason)
        self.assertEqual(record.failure_category, "unknown")

    def test_recommended_entry_point_reconciles_duplicate_rows_per_task(self):
        """Round-2 review regression (codex finding): read_attribution_jsonl_with_task_errors
        enriched every duplicate attribution.jsonl row for a task
        independently and never reconciled afterward, so a task with e.g. a
        dispatch row plus a progress row (both outcome=None in the real
        event-stream shape) came back as *two* separately-backfilled rows
        for one real task -- multiplying the failure count for any caller
        that counts rows directly, on top of Blocker 1's asymmetry. On real
        local data this took the already-wrong ~75% failure rate to ~96%."""

        attribution_path = _write_jsonl([
            {"runtime": "agent_crew", "task_id": "t1", "agent": "claude", "outcome": ""},
            {"runtime": "agent_crew", "task_id": "t1", "agent": "claude", "outcome": ""},
        ])
        db_path = _build_tasks_db([("t1", "failed", {"reason": "agy_quota_exhausted"})])
        records = read_attribution_jsonl_with_task_errors(attribution_path, db_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].outcome, "failed")
        self.assertEqual(records[0].failure_reason, "agy_quota_exhausted")


if __name__ == "__main__":
    unittest.main()
