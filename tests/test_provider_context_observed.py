"""Tests for quota-core#70's `provider_context_observed` consumer.

The producer (Agent Crew #288) emits one lifecycle row per dispatch describing
the provider context window it measured. Its contract, which this consumer
exists to preserve rather than reinterpret:

  * `context_tokens: null` is **unknown** — no store, unreadable, or a provider
    for which tokens are not measured at all. `0` is a **measured empty
    window**. Collapsing either into the other is the one thing that would make
    every downstream cohort wrong in a way nobody could detect afterwards.
  * a dispatch that tripped a cap arrives as `provider_context_capped`, a
    *different* event. Observed and capped are the two arms of one branch, so
    summing them as two samples double-counts a single dispatch.

⛔fixture-validated / production-sample pending: these fixtures are shaped from
  the producer's emitter as merged, not captured from a deployed fleet. Nothing
  here supports an end-to-end economics claim yet.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from quota_core.context_economics import (
    TaskEconomicsRecord,
    lifecycle_event_from_dict,
)
from quota_core.context_economics.agent_crew_adapter import read_lifecycle_events_jsonl
from quota_core.context_economics.analytics import (
    provider_context_by_policy,
    provider_context_window_summary,
)
from quota_core.context_economics.correlate import attach_provider_context_observations
from quota_core.context_economics.schema import (
    ProviderContextObservation,
    provider_context_observation_from_event,
    provider_context_observations_from_events,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "agent_crew" / "provider_context"


def _event(**overrides):
    base = {
        "schema_version": 1,
        "event_type": "provider_context_observed",
        "ts": "2026-09-12T09:00:00.000000",
        "task_id": "t1",
        "project": "demo",
        "context_id": "ctx-1",
        "context_generation": 1,
        "provider": "claude",
        "provider_session_id": "sess-1",
        "context_tokens": 1234,
        "context_bytes": 5678,
        "cap_mb": 64.0,
        "cap_tokens": 0,
    }
    base.update(overrides)
    return lifecycle_event_from_dict(base)


def _record(**overrides) -> TaskEconomicsRecord:
    base = dict(task_id="t1", runtime="agent_crew", outcome="success")
    base.update(overrides)
    return TaskEconomicsRecord(**base)


class ParserTests(unittest.TestCase):
    def test_the_event_type_is_recognised(self):
        self.assertEqual(_event().event_type, "provider_context_observed")

    def test_a_measured_window_is_extracted(self):
        obs = provider_context_observation_from_event(_event())
        assert obs is not None
        self.assertEqual(obs.context_tokens, 1234)
        self.assertEqual(obs.context_bytes, 5678)
        self.assertEqual(obs.task_id, "t1")
        self.assertEqual(obs.provider_session_id, "sess-1")
        self.assertFalse(obs.capped)

    def test_a_measured_zero_stays_zero(self):
        """⛔The distinction the whole contract turns on."""
        obs = provider_context_observation_from_event(_event(context_tokens=0))
        assert obs is not None
        self.assertEqual(obs.context_tokens, 0)
        self.assertIsNotNone(obs.context_tokens)

    def test_an_unknown_window_stays_none(self):
        obs = provider_context_observation_from_event(_event(context_tokens=None))
        assert obs is not None
        self.assertIsNone(obs.context_tokens)

    def test_an_absent_field_is_unknown_not_zero(self):
        payload = dict(_event().extra)
        payload.pop("context_tokens", None)
        obs = provider_context_observation_from_event(
            _event(**{k: v for k, v in payload.items()}, context_tokens=None))
        assert obs is not None
        self.assertIsNone(obs.context_tokens)

    def test_a_bool_is_not_a_measurement(self):
        """⛔`True` is an int in Python. A boolean in a token field is malformed
        producer data, not a window of one token."""
        for value in (True, False):
            obs = provider_context_observation_from_event(_event(context_tokens=value))
            assert obs is not None
            self.assertIsNone(obs.context_tokens, value)

    def test_other_event_types_return_none(self):
        self.assertIsNone(
            provider_context_observation_from_event(_event(event_type="task_started")))

    def test_the_capped_event_is_parsed_too_and_flagged(self):
        """The cap arrives under a different name and spells two fields
        differently; both are normalised, neither is invented."""
        # The real capped row carries no `provider_session_id` key at all --
        # it spells the session `conversation_id`. Leaving the observed
        # spelling in would let the top-level field satisfy the assertion and
        # the fallback would never be exercised.
        payload = {
            "schema_version": 1, "event_type": "provider_context_capped",
            "ts": "2026-09-12T09:00:00.000000", "task_id": "t1",
            "project": "demo", "context_id": "ctx-1", "context_generation": 1,
            "provider": "claude", "conversation_id": "sess-9", "bytes": 999,
            "cap_mb": 64.0, "cap_tokens": 400000, "context_tokens": 500000,
            "tripped_by": "tokens",
        }
        obs = provider_context_observation_from_event(lifecycle_event_from_dict(payload))
        assert obs is not None
        self.assertTrue(obs.capped)
        self.assertEqual(obs.provider_session_id, "sess-9")
        self.assertEqual(obs.context_bytes, 999)
        self.assertEqual(obs.tripped_by, "tokens")


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.events = read_lifecycle_events_jsonl(FIXTURE_DIR / "observations.jsonl")
        self.observations = provider_context_observations_from_events(self.events)
        self.by_task = {o.task_id: o for o in self.observations}

    def test_every_production_shape_is_read(self):
        self.assertEqual(len(self.observations), 6)

    def test_the_positive_case(self):
        self.assertEqual(self.by_task["task-claude-positive"].context_tokens, 412345)

    def test_the_measured_zero_case(self):
        self.assertEqual(self.by_task["task-claude-zero"].context_tokens, 0)

    def test_the_attempted_but_unreadable_case(self):
        self.assertIsNone(self.by_task["task-claude-unreadable"].context_tokens)
        self.assertEqual(self.by_task["task-claude-unreadable"].context_bytes, 2048)

    def test_providers_without_token_measurement(self):
        """⛔Still emitted, still joined — a cohort that can only see one
        provider cannot compare policies across them. Their window is unknown,
        which is the honest value, not a reason to drop the row."""
        for task_id in ("task-agy", "task-codex"):
            self.assertIsNone(self.by_task[task_id].context_tokens, task_id)
            self.assertIsNotNone(self.by_task[task_id].context_bytes, task_id)

    def test_the_capped_dispatch_is_flagged_not_counted_as_normal(self):
        capped = self.by_task["task-capped"]
        self.assertTrue(capped.capped)
        self.assertEqual(capped.provider_session_id, "sess-f")
        self.assertEqual(capped.context_bytes, 147435520)


class DuplicateDefenceTests(unittest.TestCase):
    """Requirement 6: a dispatch carrying BOTH events is contaminated or
    historical data, and must never be summed as two independent samples."""

    def setUp(self):
        self.events = read_lifecycle_events_jsonl(FIXTURE_DIR / "duplicated.jsonl")

    def test_one_dispatch_yields_one_observation(self):
        observations = provider_context_observations_from_events(self.events)
        self.assertEqual(len(observations), 1)

    def test_the_capped_row_wins(self):
        """⛔The cap event is the authoritative record of what happened to that
        dispatch: a reset was forced. Keeping the observation instead would
        describe the dispatch as ordinary."""
        obs = provider_context_observations_from_events(self.events)[0]
        self.assertTrue(obs.capped)

    def test_the_collision_is_reported_not_hidden(self):
        obs = provider_context_observations_from_events(self.events)[0]
        self.assertTrue(obs.duplicate_event_types)
        self.assertIn("provider_context_observed", obs.duplicate_event_types)
        self.assertIn("provider_context_capped", obs.duplicate_event_types)

    def test_a_clean_stream_reports_no_collision(self):
        obs = provider_context_observation_from_event(_event())
        assert obs is not None
        self.assertEqual(obs.duplicate_event_types, ())


class JoinTests(unittest.TestCase):
    """Requirement 3: join on task_id AND context identity — never on timing."""

    def test_an_observation_is_attached_to_its_task(self):
        records = attach_provider_context_observations(
            [_record(task_id="t1", context_id="ctx-1", provider="claude",
                     provider_session_id="sess-1", context_generation=1)],
            [provider_context_observation_from_event(_event())],
        )
        self.assertEqual(records[0].context_tokens, 1234)
        self.assertEqual(records[0].context_bytes, 5678)
        self.assertFalse(records[0].context_window_capped)

    def test_a_task_with_no_observation_stays_unknown(self):
        records = attach_provider_context_observations([_record(task_id="t1")], [])
        self.assertIsNone(records[0].context_tokens)
        self.assertIsNone(records[0].context_window_capped)

    def test_a_mismatched_context_identity_is_refused(self):
        """⛔Same task_id, different context — that is contradictory data, not a
        match. Attaching it anyway would put one dispatch's window on another's
        economics."""
        records = attach_provider_context_observations(
            [_record(task_id="t1", context_id="ctx-1")],
            [provider_context_observation_from_event(_event(context_id="ctx-OTHER"))],
        )
        self.assertIsNone(records[0].context_tokens)
        self.assertTrue(any("context identity" in n for n in records[0].attribution_notes))

    def test_identity_fields_unknown_on_one_side_do_not_block_the_join(self):
        """A record that never learned its context_id is not a contradiction."""
        records = attach_provider_context_observations(
            [_record(task_id="t1", context_id=None)],
            [provider_context_observation_from_event(_event())],
        )
        self.assertEqual(records[0].context_tokens, 1234)

    def test_the_capped_flag_reaches_the_record(self):
        records = attach_provider_context_observations(
            [_record(task_id="t1")],
            [provider_context_observation_from_event(
                _event(event_type="provider_context_capped", conversation_id="s"))],
        )
        self.assertTrue(records[0].context_window_capped)

    def test_context_pack_tokens_are_not_touched(self):
        """Requirement 3: the Context Pack's token budget and the provider's
        context window are different quantities and stay separate fields."""
        record = _record(task_id="t1")
        joined = attach_provider_context_observations(
            [record], [provider_context_observation_from_event(_event())])[0]
        self.assertEqual(joined.tokens, record.tokens)


class DenominatorTests(unittest.TestCase):
    """Requirement 4: unknown measurements must not drag an average down."""

    def _records(self):
        return [
            _record(task_id="a", context_tokens=100_000),
            _record(task_id="b", context_tokens=200_000),
            _record(task_id="c", context_tokens=None),      # unknown
            _record(task_id="d", context_tokens=None),      # unknown
        ]

    def test_the_mean_uses_only_known_measurements(self):
        summary = provider_context_window_summary(self._records())
        self.assertEqual(summary["mean_context_tokens"], 150_000)

    def test_the_denominators_are_exposed(self):
        summary = provider_context_window_summary(self._records())
        self.assertEqual(summary["total_row_count"], 4)
        self.assertEqual(summary["known_count"], 2)
        self.assertEqual(summary["unknown_count"], 2)

    def test_a_measured_zero_counts_as_known(self):
        """⛔It is a measurement, so it belongs in the denominator AND pulls the
        mean down legitimately. Treating it as unknown would flatter the mean."""
        summary = provider_context_window_summary(
            [_record(task_id="a", context_tokens=0),
             _record(task_id="b", context_tokens=100)])
        self.assertEqual(summary["known_count"], 2)
        self.assertEqual(summary["mean_context_tokens"], 50)

    def test_all_unknown_yields_no_mean_rather_than_zero(self):
        summary = provider_context_window_summary(
            [_record(task_id="a", context_tokens=None)])
        self.assertIsNone(summary["mean_context_tokens"])
        self.assertEqual(summary["known_count"], 0)

    def test_capped_dispatches_are_counted_separately(self):
        summary = provider_context_window_summary([
            _record(task_id="a", context_tokens=1, context_window_capped=False),
            _record(task_id="b", context_tokens=2, context_window_capped=True),
            _record(task_id="c", context_tokens=3, context_window_capped=None),
        ])
        self.assertEqual(summary["capped_count"], 1)
        self.assertEqual(summary["capped_known_count"], 2)


class PolicyStratificationTests(unittest.TestCase):
    """Requirement 5: resume/fresh/reset comes from the joined context policy,
    never inferred from timing or a working directory."""

    def test_windows_are_grouped_by_the_joined_policy(self):
        buckets = provider_context_by_policy([
            _record(task_id="a", context_policy="resume", context_tokens=300),
            _record(task_id="b", context_policy="resume", context_tokens=500),
            _record(task_id="c", context_policy="fresh", context_tokens=100),
        ])
        self.assertEqual(buckets["resume"]["mean_context_tokens"], 400)
        self.assertEqual(buckets["fresh"]["mean_context_tokens"], 100)

    def test_each_bucket_carries_its_own_denominators(self):
        buckets = provider_context_by_policy([
            _record(task_id="a", context_policy="resume", context_tokens=300),
            _record(task_id="b", context_policy="resume", context_tokens=None),
        ])
        self.assertEqual(buckets["resume"]["known_count"], 1)
        self.assertEqual(buckets["resume"]["unknown_count"], 1)

    def test_an_unknown_policy_is_its_own_bucket_not_folded_into_fresh(self):
        buckets = provider_context_by_policy(
            [_record(task_id="a", context_policy="unknown", context_tokens=7)])
        self.assertIn("unknown", buckets)
        self.assertNotIn("fresh", buckets)


if __name__ == "__main__":
    unittest.main()
