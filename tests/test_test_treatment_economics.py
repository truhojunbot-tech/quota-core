"""Tests for quota-core#68's tester-treatment/lock-wait telemetry consumer
(Agent Crew #278/#279's real producer contract: `record_test_economics()`
writing `effective_test_scope`/`test_scope_source`/`test_scope_hash`/
`lock_wait_seconds`/`lock_defer_count` onto the same `task_attribution` row
`attribution.jsonl` mirrors, plus the `test_scope_resolved`/
`test_stage_deferred` lifecycle events -- see schema.py's `RuntimeAttribution`
docstring and `tests/fixtures/agent_crew/test_treatment/README.md`)."""

from __future__ import annotations

import unittest
from pathlib import Path

from quota_core.context_economics import (
    TaskEconomicsRecord,
    attribution_from_dict,
    attribution_to_dict,
    lifecycle_event_from_dict,
    lock_wait_summary,
)
from quota_core.context_economics.agent_crew_adapter import (
    read_attribution_jsonl,
    read_lifecycle_events_jsonl,
)
from quota_core.context_economics.agent_crew_adapter import (
    test_stage_deferrals_for_task as find_deferrals_for_task,
)
from quota_core.context_economics.analytics import (
    test_treatment_cohorts as partition_by_treatment,
)
from quota_core.context_economics.analytics import (
    test_treatment_failure_rates as treatment_failure_rates,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "agent_crew" / "test_treatment"


def _record(**overrides) -> TaskEconomicsRecord:
    base = dict(
        task_id="t1", runtime="agent_crew", outcome="success",
        effective_test_scope=None, lock_wait_seconds=None, lock_defer_count=None,
    )
    base.update(overrides)
    return TaskEconomicsRecord(**base)


class RuntimeAttributionFieldsTests(unittest.TestCase):
    def test_explicit_null_fields_parse_as_none(self):
        row = {
            "task_id": "impl-1", "outcome": "completed",
            "effective_test_scope": None, "test_scope_source": None,
            "test_scope_hash": None, "lock_wait_seconds": None, "lock_defer_count": None,
        }
        a = attribution_from_dict(row)
        self.assertIsNone(a.effective_test_scope)
        self.assertIsNone(a.test_scope_source)
        self.assertIsNone(a.test_scope_hash)
        self.assertIsNone(a.lock_wait_seconds)
        self.assertIsNone(a.lock_defer_count)

    def test_absent_fields_also_parse_as_none(self):
        # A pre-#279 attribution.jsonl line: the keys are missing entirely,
        # not null. Must parse identically to the explicit-null case.
        row = {"task_id": "old-1", "outcome": "completed"}
        a = attribution_from_dict(row)
        self.assertIsNone(a.effective_test_scope)
        self.assertIsNone(a.lock_wait_seconds)
        self.assertIsNone(a.lock_defer_count)
        self.assertNotIn("effective_test_scope", a.extra)
        self.assertNotIn("lock_wait_seconds", a.extra)

    def test_zero_lock_wait_seconds_is_not_none(self):
        row = {"task_id": "t1", "outcome": "completed", "lock_wait_seconds": 0.0, "lock_defer_count": 0}
        a = attribution_from_dict(row)
        self.assertEqual(a.lock_wait_seconds, 0.0)
        self.assertIsNotNone(a.lock_wait_seconds)
        self.assertEqual(a.lock_defer_count, 0)
        self.assertIsNotNone(a.lock_defer_count)

    def test_real_scope_values_survive_parsing(self):
        row = {
            "task_id": "t1", "outcome": "completed",
            "effective_test_scope": "full_suite", "test_scope_source": "operator",
            "test_scope_hash": "sha256:abcd", "lock_wait_seconds": 275.0, "lock_defer_count": 1,
        }
        a = attribution_from_dict(row)
        self.assertEqual(a.effective_test_scope, "full_suite")
        self.assertEqual(a.test_scope_source, "operator")
        self.assertEqual(a.test_scope_hash, "sha256:abcd")
        self.assertEqual(a.lock_wait_seconds, 275.0)
        self.assertEqual(a.lock_defer_count, 1)

    def test_round_trip_through_to_dict_and_back(self):
        row = {
            "task_id": "t1", "outcome": "completed",
            "effective_test_scope": "targeted", "test_scope_source": "builtin",
            "test_scope_hash": "sha256:xyz", "lock_wait_seconds": 0.0, "lock_defer_count": 0,
        }
        a = attribution_from_dict(row)
        d = attribution_to_dict(a)
        b = attribution_from_dict(d)
        self.assertEqual(a.effective_test_scope, b.effective_test_scope)
        self.assertEqual(a.lock_wait_seconds, b.lock_wait_seconds)
        self.assertEqual(a.lock_defer_count, b.lock_defer_count)

    def test_non_numeric_lock_wait_seconds_falls_back_to_none_not_garbage(self):
        row = {"task_id": "t1", "outcome": "completed", "lock_wait_seconds": "not-a-number"}
        a = attribution_from_dict(row)
        self.assertIsNone(a.lock_wait_seconds)


class LifecycleEventTypesTests(unittest.TestCase):
    def test_test_scope_resolved_is_no_longer_dropped(self):
        raw = {
            "schema_version": 1, "event_type": "test_scope_resolved",
            "ts": "2026-09-06T18:23:20.100000", "task_id": "t1", "project": "p",
            "effective_test_scope": "targeted", "test_scope_source": "builtin",
            "test_scope_hash": "sha256:abc", "lock_wait_seconds": 0.0, "lock_defer_count": 0,
        }
        event = lifecycle_event_from_dict(raw)
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, "test_scope_resolved")
        self.assertEqual(event.extra["effective_test_scope"], "targeted")
        self.assertEqual(event.extra["lock_wait_seconds"], 0.0)

    def test_test_stage_deferred_is_no_longer_dropped(self):
        raw = {
            "schema_version": 1, "event_type": "test_stage_deferred",
            "ts": "2026-09-06T18:26:40.000000", "task_id": "t1", "project": "p",
            "defer_count": 1, "waiting_since": 1788701000.0, "lock_wait_seconds": 0.0,
        }
        event = lifecycle_event_from_dict(raw)
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, "test_stage_deferred")
        self.assertEqual(event.extra["defer_count"], 1)

    def test_still_tolerant_of_a_genuinely_unknown_event_type(self):
        raw = {"schema_version": 1, "event_type": "something_from_the_future", "ts": "2026-09-06T00:00:00"}
        self.assertIsNone(lifecycle_event_from_dict(raw))


class FixtureIntegrationTests(unittest.TestCase):
    """End-to-end: fixture -> adapter -> analytics, against the real #279-shaped files."""

    def setUp(self):
        self.attributions = read_attribution_jsonl(FIXTURE_DIR / "attribution.jsonl")
        self.events = read_lifecycle_events_jsonl(FIXTURE_DIR / "context_events.jsonl")

    def test_all_six_rows_parse(self):
        # impl-9c1a2b3d (1) + test-targeted (dispatch+terminal=2) +
        # test-fullsuite (dispatch+terminal=2) + test-historical (1) = 6.
        self.assertEqual(len(self.attributions), 6)

    def test_non_test_task_has_null_treatment_fields(self):
        impl = [a for a in self.attributions if a.task_id == "impl-9c1a2b3d"][0]
        self.assertIsNone(impl.effective_test_scope)
        self.assertIsNone(impl.lock_wait_seconds)

    def test_targeted_task_has_zero_wait(self):
        rows = [a for a in self.attributions if a.task_id == "test-targeted-4e5f6a7b"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row.effective_test_scope, "targeted")
            self.assertEqual(row.lock_wait_seconds, 0.0)
            self.assertEqual(row.lock_defer_count, 0)

    def test_deferred_then_dispatched_task_carries_summed_wait(self):
        rows = [a for a in self.attributions if a.task_id == "test-fullsuite-8c9d0e1f"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row.effective_test_scope, "full_suite")
            self.assertEqual(row.lock_wait_seconds, 275.0)
            self.assertEqual(row.lock_defer_count, 1)

    def test_deferred_task_produces_exactly_one_task_record_not_two(self):
        # A test_stage_deferred event shares the task_id with the eventually
        # dispatched attempt -- it must never surface as a second, separate
        # attribution/task-economics record for that task_id.
        task_ids = [a.task_id for a in self.attributions]
        self.assertEqual(task_ids.count("test-fullsuite-8c9d0e1f"), 2)  # dispatch + terminal, same task
        deferral_task_ids = {e.task_id for e in self.events if e.event_type == "test_stage_deferred"}
        self.assertEqual(deferral_task_ids, {"test-fullsuite-8c9d0e1f"})

    def test_historical_row_missing_keys_entirely_still_parses_as_unknown(self):
        hist = [a for a in self.attributions if a.task_id == "test-historical-2a3b4c5d"][0]
        self.assertIsNone(hist.effective_test_scope)
        self.assertIsNone(hist.lock_wait_seconds)
        self.assertIsNone(hist.lock_defer_count)

    def test_deferral_events_join_to_their_task_by_task_id(self):
        deferrals = find_deferrals_for_task(self.events, "test-fullsuite-8c9d0e1f")
        self.assertEqual(len(deferrals), 1)
        self.assertEqual(deferrals[0].extra["defer_count"], 1)
        self.assertEqual(find_deferrals_for_task(self.events, "test-targeted-4e5f6a7b"), [])

    def test_test_scope_resolved_events_present_for_both_test_tasks(self):
        resolved_task_ids = {e.task_id for e in self.events if e.event_type == "test_scope_resolved"}
        self.assertEqual(resolved_task_ids, {"test-targeted-4e5f6a7b", "test-fullsuite-8c9d0e1f"})


class TestTreatmentCohortsTests(unittest.TestCase):
    def test_partitions_by_known_scope_and_unknown(self):
        records = [
            _record(task_id="a", effective_test_scope="targeted"),
            _record(task_id="b", effective_test_scope="targeted"),
            _record(task_id="c", effective_test_scope="full_suite"),
            _record(task_id="d", effective_test_scope=None),
            _record(task_id="e", effective_test_scope="some_future_scope"),
        ]
        cohorts = partition_by_treatment(records)
        self.assertEqual(len(cohorts["targeted"]), 2)
        self.assertEqual(len(cohorts["full_suite"]), 1)
        # None AND an unrecognized future value both land in "unknown".
        self.assertEqual(len(cohorts["unknown"]), 2)
        self.assertEqual(sum(len(v) for v in cohorts.values()), len(records))

    def test_every_record_appears_in_exactly_one_cohort(self):
        records = [_record(task_id=str(i), effective_test_scope=s)
                   for i, s in enumerate(["targeted", "full_suite", None, "targeted"])]
        cohorts = partition_by_treatment(records)
        seen_ids = [r.task_id for rows in cohorts.values() for r in rows]
        self.assertEqual(sorted(seen_ids), ["0", "1", "2", "3"])


class TestTreatmentFailureRatesTests(unittest.TestCase):
    def test_cohorts_never_pooled_and_each_has_its_own_sample_size(self):
        records = [
            _record(task_id="a", effective_test_scope="targeted", outcome="success"),
            _record(task_id="b", effective_test_scope="targeted", outcome="failed"),
            _record(task_id="c", effective_test_scope="full_suite", outcome="success"),
        ]
        rates = treatment_failure_rates(records)
        self.assertEqual(rates["targeted"]["observed_count"], 2)
        self.assertEqual(rates["targeted"]["raw_failure_count"], 1)
        self.assertEqual(rates["full_suite"]["observed_count"], 1)
        self.assertEqual(rates["full_suite"]["raw_failure_count"], 0)
        self.assertEqual(rates["unknown"]["observed_count"], 0)

    def test_unknown_cohort_is_the_non_test_population_not_a_shared_pool(self):
        records = [
            _record(task_id="a", effective_test_scope=None, outcome="failed"),
            _record(task_id="b", effective_test_scope="targeted", outcome="success"),
        ]
        rates = treatment_failure_rates(records)
        # The unknown-cohort failure does not leak into targeted's count.
        self.assertEqual(rates["targeted"]["raw_failure_count"], 0)
        self.assertEqual(rates["unknown"]["raw_failure_count"], 1)


class LockWaitSummaryTests(unittest.TestCase):
    def test_unknown_records_excluded_from_the_mean_not_treated_as_zero(self):
        records = [
            _record(task_id="a", lock_wait_seconds=None, lock_defer_count=None),  # non-test / historical
            _record(task_id="b", lock_wait_seconds=0.0, lock_defer_count=0),
            _record(task_id="c", lock_wait_seconds=100.0, lock_defer_count=1),
        ]
        summary = lock_wait_summary(records)
        self.assertEqual(summary["observed_count"], 3)
        self.assertEqual(summary["known_count"], 2)
        self.assertEqual(summary["unknown_count"], 1)
        # Mean over the two KNOWN rows only: (0.0 + 100.0) / 2 = 50.0, not /3.
        self.assertEqual(summary["mean_lock_wait_seconds"], 50.0)
        self.assertEqual(summary["total_lock_wait_seconds"], 100.0)
        self.assertEqual(summary["deferred_count"], 1)
        self.assertEqual(summary["max_lock_defer_count"], 1)

    def test_all_unknown_reports_none_not_zero(self):
        records = [_record(task_id="a", lock_wait_seconds=None, lock_defer_count=None)]
        summary = lock_wait_summary(records)
        self.assertIsNone(summary["mean_lock_wait_seconds"])
        self.assertIsNone(summary["total_lock_wait_seconds"])
        self.assertEqual(summary["known_count"], 0)

    def test_lock_wait_never_folds_into_duration_or_token_totals(self):
        # A record with a large lock_wait_seconds must not affect
        # duration_seconds -- that property is computed purely from
        # started_at/completed_at, independent of lock_wait_seconds, and this
        # test pins that independence so a future change can't accidentally
        # start blending scheduler delay into provider runtime.
        record = _record(
            task_id="a", lock_wait_seconds=99999.0, lock_defer_count=5,
            started_at=1000, completed_at=1010,
        )
        self.assertEqual(record.duration_seconds, 10.0)

    def test_empty_input(self):
        summary = lock_wait_summary([])
        self.assertEqual(summary["observed_count"], 0)
        self.assertEqual(summary["known_count"], 0)
        self.assertIsNone(summary["mean_lock_wait_seconds"])
        self.assertIsNone(summary["max_lock_defer_count"])


if __name__ == "__main__":
    unittest.main()
