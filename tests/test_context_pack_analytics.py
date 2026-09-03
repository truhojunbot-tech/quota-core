"""Tests for quota-core#62's Context Pack telemetry consumer (Agent Crew
#239's real producer contract: ContextPack.telemetry() emitted as a
"context_pack_built" lifecycle event -- see schema.py's
ContextPackAttribution/context_pack_attribution_from_event docstrings)."""

from __future__ import annotations

import unittest

from quota_core.context_economics import (
    ContextLifecycleEvent,
    ContextPackAttribution,
    context_composition,
    context_pack_attribution_from_event,
    context_pack_attributions_from_events,
    context_pack_efficiency,
    lifecycle_event_from_dict,
    retrieval_mode_comparison,
)


def _real_shape_event(**overrides) -> ContextLifecycleEvent:
    """A context_pack_built event dict matching Agent Crew's real wire shape
    (record_context_event's payload: schema_version/event_type/ts plus the
    kwargs the dispatcher passes -- task_id/project/role/agent/context_id/
    context_generation and ContextPack.telemetry()'s own dict), parsed
    through the same lifecycle_event_from_dict the real adapter uses so this
    test exercises the actual known_keys/extra split, not a hand-built
    ContextLifecycleEvent that could drift from the real parser's behavior."""

    payload = {
        "schema_version": 1,
        "event_type": "context_pack_built",
        "ts": "2026-09-03T00:00:00.000000",
        "task_id": "impl-1",
        "project": "demo-project",
        "role": "implementer",
        "agent": "claude",
        "context_id": "ctx-1",
        "context_generation": 1,
        "context_pack_id": "cp1-abcd1234",
        "context_pack_hash": "abcd1234",
        "context_pack_schema_version": 1,
        "mode": "lexical",
        "candidate_count": 40,
        "selected_count": 12,
        "total_tokens": 1800,
        "tokens_by_category": {"mandatory": 400, "authoritative": 1000, "episodic": 400},
        "stale_count": 0,
        "conflict_count": 0,
        "latency_ms": 245.3,
        "degraded": False,
        "degraded_reason": "",
        "budget": {"max_tokens": 4000, "max_items": 16},
    }
    payload.update(overrides)
    event = lifecycle_event_from_dict(payload)
    assert event is not None
    return event


class ContextPackAttributionFromEventTests(unittest.TestCase):
    def test_non_context_pack_built_event_returns_none(self):
        event = ContextLifecycleEvent(event_type="task_started", runtime="agent_crew", timestamp=1)
        self.assertIsNone(context_pack_attribution_from_event(event))

    def test_real_shape_full_fixture(self):
        """Scenario 2 (issue #62 fixtures): lexical pack with a complete
        terminal outcome -- here, all fields present and well-typed."""

        event = _real_shape_event()
        attribution = context_pack_attribution_from_event(event)
        self.assertIsNotNone(attribution)
        self.assertEqual(attribution.task_id, "impl-1")
        self.assertEqual(attribution.project, "demo-project")
        self.assertEqual(attribution.context_id, "ctx-1")
        self.assertEqual(attribution.context_generation, 1)
        self.assertEqual(attribution.role, "implementer")
        self.assertEqual(attribution.agent, "claude")
        self.assertEqual(attribution.mode, "lexical")
        self.assertEqual(attribution.context_pack_id, "cp1-abcd1234")
        self.assertEqual(attribution.candidate_count, 40)
        self.assertEqual(attribution.selected_count, 12)
        self.assertEqual(attribution.total_tokens, 1800)
        self.assertEqual(attribution.tokens_by_category, {"mandatory": 400, "authoritative": 1000, "episodic": 400})
        self.assertEqual(attribution.stale_count, 0)
        self.assertEqual(attribution.latency_ms, 245.3)
        self.assertIs(attribution.degraded, False)
        self.assertEqual(attribution.budget, {"max_tokens": 4000, "max_items": 16})

    def test_semantic_hybrid_pack_with_latency_and_stale_item(self):
        """Scenario 3 (issue #62 fixtures): semantic/hybrid pack with
        retrieval latency and a stale item."""

        event = _real_shape_event(mode="hybrid", stale_count=2, latency_ms=980.1)
        attribution = context_pack_attribution_from_event(event)
        self.assertEqual(attribution.mode, "hybrid")
        self.assertEqual(attribution.stale_count, 2)
        self.assertEqual(attribution.latency_ms, 980.1)

    def test_retry_recovery_with_refreshed_pack_same_task_identity(self):
        """Scenario 4 (issue #62 fixtures): a retry re-dispatch produces a
        SECOND context_pack_built event for the same task_id, with a
        different pack hash (the pack was rebuilt, not reused) -- both must
        parse independently; deduping/choosing "the" pack for a task is a
        reporting-layer decision this parser does not make."""

        first = context_pack_attribution_from_event(_real_shape_event())
        second = context_pack_attribution_from_event(
            _real_shape_event(context_pack_hash="efgh5678", context_pack_id="cp1-efgh5678", candidate_count=45)
        )
        self.assertEqual(first.task_id, second.task_id)
        self.assertNotEqual(first.context_pack_hash, second.context_pack_hash)

    def test_reset_with_new_context_generation(self):
        """Scenario 5 (issue #62 fixtures): a reset bumps context_generation
        while task_id/context_id stay the same."""

        before = context_pack_attribution_from_event(_real_shape_event(context_generation=1))
        after = context_pack_attribution_from_event(_real_shape_event(context_generation=2))
        self.assertEqual(before.context_id, after.context_id)
        self.assertEqual(before.context_generation, 1)
        self.assertEqual(after.context_generation, 2)

    def test_degraded_pack_with_reason(self):
        event = _real_shape_event(degraded=True, degraded_reason="retrieval_provider_timeout", selected_count=0)
        attribution = context_pack_attribution_from_event(event)
        self.assertIs(attribution.degraded, True)
        self.assertEqual(attribution.degraded_reason, "retrieval_provider_timeout")

    def test_missing_fields_stay_none_not_zero(self):
        """A producer that hasn't started reporting a field (schema drift/
        older version) must leave it None/empty, never a fabricated 0."""

        minimal = {
            "schema_version": 1,
            "event_type": "context_pack_built",
            "ts": "2026-09-03T00:00:00.000000",
            "task_id": "impl-2",
        }
        event = lifecycle_event_from_dict(minimal)
        attribution = context_pack_attribution_from_event(event)
        self.assertIsNotNone(attribution)
        self.assertIsNone(attribution.mode)
        self.assertIsNone(attribution.candidate_count)
        self.assertIsNone(attribution.total_tokens)
        self.assertEqual(attribution.tokens_by_category, {})
        self.assertIsNone(attribution.degraded)
        self.assertEqual(attribution.budget, {})

    def test_malformed_field_types_are_ignored_not_coerced(self):
        """A future/buggy producer sending the wrong JSON type for a field
        (e.g. a string where a number is expected) must not crash the parser
        or silently coerce into a misleading number -- it stays unknown."""

        event = _real_shape_event(
            candidate_count="forty", latency_ms="fast", degraded="yes", tokens_by_category="not-a-dict"
        )
        attribution = context_pack_attribution_from_event(event)
        self.assertIsNone(attribution.candidate_count)
        self.assertIsNone(attribution.latency_ms)
        self.assertIsNone(attribution.degraded)
        self.assertEqual(attribution.tokens_by_category, {})

    def test_bool_is_not_mistaken_for_int(self):
        """Python's bool is a subclass of int -- a stray True/False in an
        integer-typed field must not silently parse as 1/0."""

        event = _real_shape_event(candidate_count=True)
        attribution = context_pack_attribution_from_event(event)
        self.assertIsNone(attribution.candidate_count)


class ContextPackAttributionsFromEventsTests(unittest.TestCase):
    def test_filters_mixed_lifecycle_stream(self):
        """Scenario 1 (issue #62 fixtures): a legacy task with no Context
        Pack telemetry alongside real pack events in the same stream --
        the non-pack event must be silently skipped, not raise."""

        events = [
            ContextLifecycleEvent(event_type="task_started", runtime="agent_crew", timestamp=1, task_id="legacy-1"),
            _real_shape_event(),
            ContextLifecycleEvent(event_type="context_compacted", runtime="agent_crew", timestamp=2),
        ]
        attributions = context_pack_attributions_from_events(events)
        self.assertEqual(len(attributions), 1)
        self.assertEqual(attributions[0].task_id, "impl-1")

    def test_empty_stream_returns_empty_list(self):
        self.assertEqual(context_pack_attributions_from_events([]), [])


class ContextCompositionTests(unittest.TestCase):
    def test_empty_input(self):
        result = context_composition([])
        self.assertEqual(result["pack_count"], 0)
        self.assertIsNone(result["mean_total_tokens"])
        self.assertEqual(result["category_totals"], {})

    def test_aggregates_real_shape_packs(self):
        packs = [
            context_pack_attribution_from_event(_real_shape_event()),
            context_pack_attribution_from_event(_real_shape_event(total_tokens=2200, tokens_by_category={"mandatory": 400, "authoritative": 1800})),
        ]
        result = context_composition(packs)
        self.assertEqual(result["pack_count"], 2)
        self.assertEqual(result["total_tokens_known_count"], 2)
        self.assertEqual(result["mean_total_tokens"], 2000.0)
        self.assertEqual(result["category_totals"], {"mandatory": 800, "authoritative": 2800, "episodic": 400})

    def test_missing_total_tokens_excluded_from_mean_not_treated_as_zero(self):
        known = context_pack_attribution_from_event(_real_shape_event(total_tokens=1000))
        unknown = ContextPackAttribution(task_id="t2")  # no total_tokens at all
        result = context_composition([known, unknown])
        self.assertEqual(result["pack_count"], 2)
        self.assertEqual(result["total_tokens_known_count"], 1)
        self.assertEqual(result["mean_total_tokens"], 1000.0)  # not (1000+0)/2


class ContextPackEfficiencyTests(unittest.TestCase):
    def test_empty_input(self):
        result = context_pack_efficiency([])
        self.assertEqual(result["pack_count"], 0)
        self.assertIsNone(result["mean_compression_ratio"])
        self.assertIsNone(result["degraded_rate"])

    def test_compression_ratio_and_degraded_rate(self):
        healthy = context_pack_attribution_from_event(_real_shape_event(candidate_count=40, selected_count=10, degraded=False))
        degraded = context_pack_attribution_from_event(_real_shape_event(candidate_count=40, selected_count=0, degraded=True))
        result = context_pack_efficiency([healthy, degraded])
        self.assertEqual(result["pack_count"], 2)
        self.assertAlmostEqual(result["mean_compression_ratio"], (0.25 + 0.0) / 2)
        self.assertEqual(result["degraded_known_count"], 2)
        self.assertEqual(result["degraded_rate"], 0.5)

    def test_zero_candidate_count_excluded_from_compression_sample(self):
        """candidate_count == 0 would be a division-by-zero pack, not a
        meaningful 0%/100% compression ratio -- excluded, not coerced."""

        zero_candidates = context_pack_attribution_from_event(_real_shape_event(candidate_count=0, selected_count=0))
        result = context_pack_efficiency([zero_candidates])
        self.assertEqual(result["candidate_selected_known_count"], 0)
        self.assertIsNone(result["mean_compression_ratio"])

    def test_latency_percentiles(self):
        packs = [
            context_pack_attribution_from_event(_real_shape_event(latency_ms=float(v)))
            for v in (100, 200, 300, 400, 500)
        ]
        result = context_pack_efficiency(packs)
        self.assertEqual(result["latency_known_count"], 5)
        self.assertEqual(result["latency_ms_p50"], 300)
        self.assertEqual(result["latency_ms_p95"], 500)

    def test_latency_p50_matches_true_nearest_rank_not_round_half_formula(self):
        """quota-core#62 review found the original implementation used
        round(pct * (n-1)) -- a different, undocumented method that happens
        to agree with true nearest-rank (ceil(pct*n), 1-indexed) on the
        5-element sample above but diverges on a 4-element one: p50 of
        [10,20,30,40] is 20 under nearest-rank, was 30 under the old
        formula."""

        packs = [
            context_pack_attribution_from_event(_real_shape_event(latency_ms=float(v)))
            for v in (10, 20, 30, 40)
        ]
        result = context_pack_efficiency(packs)
        self.assertEqual(result["latency_ms_p50"], 20)


class RetrievalModeComparisonTests(unittest.TestCase):
    def test_groups_by_mode_with_sample_sizes(self):
        lexical = [context_pack_attribution_from_event(_real_shape_event(mode="lexical")) for _ in range(3)]
        hybrid = [context_pack_attribution_from_event(_real_shape_event(mode="hybrid", latency_ms=900.0))]
        result = retrieval_mode_comparison(lexical + hybrid)
        self.assertEqual(set(result.keys()), {"lexical", "hybrid"})
        self.assertEqual(result["lexical"]["sample_size"], 3)
        self.assertEqual(result["hybrid"]["sample_size"], 1)

    def test_missing_mode_groups_under_unknown(self):
        no_mode = ContextPackAttribution(task_id="legacy-1")
        result = retrieval_mode_comparison([no_mode])
        self.assertEqual(list(result.keys()), ["unknown"])
        self.assertEqual(result["unknown"]["sample_size"], 1)
