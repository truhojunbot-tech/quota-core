"""quota-core#78 -- Agent Crew #317/#318 task-level token/cache telemetry.

The producer's own contract (`agent_crew/telemetry.py`) says: "``None`` means
the provider did not supply the fact; callers must never derive or estimate
it." Most of what follows is that one sentence, tested from the consumer side.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from quota_core.context_economics import (
    TASK_ATTRIBUTION_HASH_FIELDS,
    TASK_TOKEN_TELEMETRY_FIELDS,
    TaskTokenTelemetry,
    attribution_from_dict,
    attribution_to_dict,
    correlate_task_economics,
    context_window_observations,
    validate_attribution_dict,
    task_economics_to_dict,
    task_telemetry_by_stable_prefix,
    task_token_telemetry_from_dict,
    task_token_telemetry_summary,
    task_token_telemetry_to_dict,
)
from quota_core.context_economics.agent_crew_adapter import read_attribution_jsonl

FIXTURE = Path(__file__).parent / "fixtures" / "agent_crew" / "task_token_telemetry"


def _attr(**over):
    row = {"runtime": "agent_crew", "task_id": "t1", "outcome": "completed"}
    row.update(over)
    return attribution_from_dict(row)


def _record(**over):
    return correlate_task_economics([_attr(**over)], [])[0]


class ParsingTests(unittest.TestCase):
    def test_parses_every_field_the_producer_declares(self):
        """The eight columns are exactly agent_crew/telemetry.py's contract."""
        a = _attr(
            uncached_input_tokens=1840, cache_write_tokens=0, cache_read_tokens=118450,
            output_tokens=3120, reasoning_tokens=910, context_window_tokens=124300,
            stable_prefix_hash="sha256:abc", context_pack_hash="cp1-9f",
        )
        self.assertEqual(a.task_telemetry.uncached_input_tokens, 1840)
        self.assertEqual(a.task_telemetry.cache_write_tokens, 0)
        self.assertEqual(a.task_telemetry.cache_read_tokens, 118450)
        self.assertEqual(a.task_telemetry.output_tokens, 3120)
        self.assertEqual(a.task_telemetry.reasoning_tokens, 910)
        self.assertEqual(a.task_telemetry.context_window_tokens, 124300)
        self.assertEqual(a.stable_prefix_hash, "sha256:abc")
        self.assertEqual(a.context_pack_hash, "cp1-9f")

    def test_absent_key_and_explicit_null_both_mean_unknown(self):
        absent = _attr()
        explicit = _attr(**{name: None for name in TASK_TOKEN_TELEMETRY_FIELDS})
        self.assertEqual(absent.task_telemetry, explicit.task_telemetry)
        self.assertEqual(absent.task_telemetry.observed_components, ())
        self.assertFalse(absent.task_telemetry.has_any_observation)

    def test_measured_zero_is_not_unknown(self):
        """The single invariant this whole type exists to carry."""
        a = _attr(cache_read_tokens=0, reasoning_tokens=0)
        self.assertEqual(a.task_telemetry.cache_read_tokens, 0)
        self.assertEqual(a.task_telemetry.reasoning_tokens, 0)
        self.assertIsNone(a.task_telemetry.output_tokens)
        self.assertEqual(
            a.task_telemetry.observed_components, ("cache_read_tokens", "reasoning_tokens")
        )

    def test_a_stray_bool_never_fabricates_a_measurement(self):
        """bool is an int subclass; `true` must not become a 1-token reading."""
        for value in (True, False):
            a = _attr(uncached_input_tokens=value, cache_read_tokens=value)
            self.assertIsNone(a.task_telemetry.uncached_input_tokens)
            self.assertIsNone(a.task_telemetry.cache_read_tokens)

    def test_non_numeric_stays_unknown_rather_than_coerced(self):
        a = _attr(output_tokens="lots", context_window_tokens=[1, 2])
        self.assertIsNone(a.task_telemetry.output_tokens)
        self.assertIsNone(a.task_telemetry.context_window_tokens)

    def test_new_columns_do_not_leak_into_extra_as_unknown_forward_fields(self):
        a = _attr(uncached_input_tokens=5, stable_prefix_hash="h")
        for name in TASK_TOKEN_TELEMETRY_FIELDS + TASK_ATTRIBUTION_HASH_FIELDS:
            self.assertNotIn(name, a.extra)

    def test_standalone_parser_matches_the_attribution_path(self):
        row = {"uncached_input_tokens": 12, "cache_read_tokens": 0}
        self.assertEqual(task_token_telemetry_from_dict(row), _attr(**row).task_telemetry)


class NoFabricatedTotalTests(unittest.TestCase):
    def test_the_type_exposes_no_total(self):
        """Five different economic quantities; a universal total would lie."""
        t = TaskTokenTelemetry(output_tokens=100, reasoning_tokens=40)
        for forbidden in ("total", "total_tokens", "provider_total", "sum"):
            self.assertFalse(hasattr(t, forbidden), forbidden)

    def test_summary_reports_each_component_with_its_own_denominator(self):
        records = [
            _record(task_id="a", uncached_input_tokens=100, cache_read_tokens=0),
            _record(task_id="b", uncached_input_tokens=300),
            _record(task_id="c"),
        ]
        s = task_token_telemetry_summary(records)
        self.assertEqual(s["uncached_input_tokens"]["known_count"], 2)
        self.assertEqual(s["uncached_input_tokens"]["mean"], 200.0)
        # The measured zero counts as an observation, not as "unknown".
        self.assertEqual(s["cache_read_tokens"]["known_count"], 1)
        self.assertEqual(s["cache_read_tokens"]["total"], 0)
        # A component nobody reported stays None rather than becoming 0.
        self.assertIsNone(s["reasoning_tokens"]["total"])
        self.assertEqual(s["reasoning_tokens"]["known_count"], 0)
        self.assertEqual(s["reasoning_tokens"]["unknown_count"], 3)

    def test_summary_never_crosses_components(self):
        """reasoning is a subset of output; no key may combine them."""
        s = task_token_telemetry_summary([_record(output_tokens=100, reasoning_tokens=40)])
        self.assertEqual(set(s), set(TASK_TOKEN_TELEMETRY_FIELDS))
        self.assertEqual(s["output_tokens"]["total"], 100)
        self.assertEqual(s["reasoning_tokens"]["total"], 40)


class HashDimensionTests(unittest.TestCase):
    def test_hashes_are_carried_without_interpretation(self):
        a = _attr(stable_prefix_hash="sha256:abc", context_pack_hash="cp1-9f")
        self.assertEqual(a.stable_prefix_hash, "sha256:abc")
        self.assertEqual(a.context_pack_hash, "cp1-9f")

    def test_grouping_by_prefix_asserts_nothing_about_caching(self):
        records = [
            _record(task_id="a", stable_prefix_hash="p1", cache_read_tokens=1000),
            _record(task_id="b", stable_prefix_hash="p1", cache_read_tokens=0),
            _record(task_id="c"),
        ]
        grouped = task_telemetry_by_stable_prefix(records)
        self.assertEqual(grouped["p1"]["sample_size"], 2)
        self.assertEqual(grouped["p1"]["cache_read_tokens"], 1000)
        # A shared prefix with a zero cache read is NOT reported as a cache hit.
        self.assertEqual(grouped["unknown"]["sample_size"], 1)
        self.assertIsNone(grouped["unknown"]["cache_read_tokens"])


class ContextWindowObservationTests(unittest.TestCase):
    """The #70 and #78 numbers are DIFFERENT measurements, not one to reconcile.

    Review of the first version of this change (review-impl-issue78-r0, P1)
    established the producer contract: `provider_context_observed` is emitted at
    dispatch time from the pre-task transcript window (server.py), while
    `telemetry_claude._extract_span` sums cache-read + cache-write +
    uncached-input across EVERY invocation in the completed task span. A
    multi-invocation task therefore disagrees by construction, and the earlier
    "conflict -> choose neither" policy discarded both valid observations.
    """

    def _with_dispatch_window(self, record, tokens):
        import dataclasses
        return dataclasses.replace(record, context_tokens=tokens)

    def test_a_multi_invocation_task_keeps_both_observations(self):
        """★The regression this class exists for. Producer-shaped: a task
        handed a 42k window that then made four invocations, so the span input
        total is ~4 windows' worth. Neither number is wrong and neither is
        dropped."""
        r = self._with_dispatch_window(_record(context_window_tokens=168000), 42000)
        out = context_window_observations(r)
        self.assertEqual(out["dispatch_window_tokens"], 42000)
        self.assertEqual(out["task_span_input_total_tokens"], 168000)
        self.assertEqual(out["known"], ("dispatch_window", "task_span_input_total"))
        self.assertIs(out["comparable"], False)
        self.assertAlmostEqual(out["invocation_amplification"], 4.0)

    def test_the_two_fields_are_never_merged_on_the_record(self):
        r = self._with_dispatch_window(_record(context_window_tokens=168000), 42000)
        self.assertEqual(r.context_tokens, 42000)
        self.assertEqual(r.task_telemetry.context_window_tokens, 168000)

    def test_a_difference_is_never_reported_as_a_conflict(self):
        out = context_window_observations(
            self._with_dispatch_window(_record(context_window_tokens=168000), 42000))
        self.assertNotIn("conflict", str(out.values()))
        # And nothing sums them: neither 210000 nor an average appears.
        self.assertNotIn(210000, out.values())
        self.assertNotIn(105000, out.values())

    def test_single_invocation_task_reports_amplification_near_one(self):
        out = context_window_observations(
            self._with_dispatch_window(_record(context_window_tokens=43000), 42000))
        self.assertLess(out["invocation_amplification"], 1.1)
        self.assertIs(out["comparable"], False)

    def test_each_side_alone(self):
        only_dispatch = context_window_observations(
            self._with_dispatch_window(_record(), 42000))
        self.assertEqual(only_dispatch["known"], ("dispatch_window",))
        self.assertIsNone(only_dispatch["invocation_amplification"])
        only_span = context_window_observations(_record(context_window_tokens=168000))
        self.assertEqual(only_span["known"], ("task_span_input_total",))
        self.assertIsNone(only_span["invocation_amplification"])

    def test_neither_known(self):
        out = context_window_observations(_record())
        self.assertEqual(out["known"], ())
        self.assertIsNone(out["dispatch_window_tokens"])
        self.assertIsNone(out["task_span_input_total_tokens"])
        self.assertIsNone(out["invocation_amplification"])

    def test_a_zero_dispatch_window_does_not_divide(self):
        out = context_window_observations(
            self._with_dispatch_window(_record(context_window_tokens=168000), 0))
        self.assertEqual(out["dispatch_window_tokens"], 0)
        self.assertIsNone(out["invocation_amplification"])

    def test_span_total_equals_its_components_where_all_are_present(self):
        """The producer derives it as exactly that sum, which is why it carries
        no information the three components do not already carry."""
        r = _record(uncached_input_tokens=1840, cache_write_tokens=0,
                    cache_read_tokens=118450, context_window_tokens=1840 + 0 + 118450)
        t = r.task_telemetry
        self.assertEqual(
            t.context_window_tokens,
            t.uncached_input_tokens + t.cache_write_tokens + t.cache_read_tokens)


class ValidatorTests(unittest.TestCase):
    """review-impl-issue78-r0 P2: the validator called malformed data valid."""

    def test_a_bool_is_rejected_for_every_token_field(self):
        for name in TASK_TOKEN_TELEMETRY_FIELDS:
            for value in (True, False):
                errors = validate_attribution_dict(
                    {"runtime": "agent_crew", "task_id": "t", name: value})
                self.assertTrue(any(name in e for e in errors), f"{name}={value}")

    def test_a_non_integer_is_rejected(self):
        errors = validate_attribution_dict(
            {"runtime": "agent_crew", "task_id": "t", "output_tokens": "lots"})
        self.assertTrue(any("output_tokens" in e for e in errors))

    def test_the_validator_agrees_with_the_parser(self):
        """⛔The actual defect: a validator that passes what its own parser
        drops is worse than none, because a caller trusts it."""
        for name in TASK_TOKEN_TELEMETRY_FIELDS:
            row = {"runtime": "agent_crew", "task_id": "t", name: True}
            self.assertIsNone(getattr(attribution_from_dict(row).task_telemetry, name))
            self.assertTrue(validate_attribution_dict(row))

    def test_measured_values_including_zero_stay_valid(self):
        row = {"runtime": "agent_crew", "task_id": "t"}
        row.update({name: 0 for name in TASK_TOKEN_TELEMETRY_FIELDS})
        self.assertEqual(validate_attribution_dict(row), ())
        row.update({name: 1234 for name in TASK_TOKEN_TELEMETRY_FIELDS})
        self.assertEqual(validate_attribution_dict(row), ())

    def test_null_and_absent_stay_valid(self):
        self.assertEqual(
            validate_attribution_dict({"runtime": "agent_crew", "task_id": "t"}), ())
        self.assertEqual(validate_attribution_dict(
            {"runtime": "agent_crew", "task_id": "t",
             **{n: None for n in TASK_TOKEN_TELEMETRY_FIELDS}}), ())

    def test_hash_fields_must_be_strings(self):
        for name in TASK_ATTRIBUTION_HASH_FIELDS:
            errors = validate_attribution_dict(
                {"runtime": "agent_crew", "task_id": "t", name: 12345})
            self.assertTrue(any(name in e for e in errors), name)
            self.assertEqual(validate_attribution_dict(
                {"runtime": "agent_crew", "task_id": "t", name: "sha256:ok"}), ())


class PersistenceTests(unittest.TestCase):
    """PR #71 joined fields in memory and dropped them at this boundary."""

    def test_attribution_round_trips_exactly(self):
        a = _attr(
            uncached_input_tokens=1840, cache_write_tokens=0, cache_read_tokens=118450,
            output_tokens=3120, reasoning_tokens=None, context_window_tokens=124300,
            stable_prefix_hash="sha256:abc", context_pack_hash="cp1-9f",
        )
        self.assertEqual(attribution_from_dict(attribution_to_dict(a)), a)

    def test_serialized_attribution_uses_the_producers_own_column_names(self):
        d = attribution_to_dict(_attr(uncached_input_tokens=7))
        for name in TASK_TOKEN_TELEMETRY_FIELDS + TASK_ATTRIBUTION_HASH_FIELDS:
            self.assertIn(name, d)

    def test_task_economics_record_survives_serialization(self):
        r = _record(
            uncached_input_tokens=1840, cache_write_tokens=0, cache_read_tokens=0,
            output_tokens=3120, context_window_tokens=124300,
            stable_prefix_hash="sha256:abc", context_pack_hash="cp1-9f",
        )
        d = task_economics_to_dict(r)
        # Nested under its own key, matching the existing `tokens` component
        # group rather than inventing a second convention in one schema.
        self.assertEqual(d["task_telemetry"]["uncached_input_tokens"], 1840)
        self.assertEqual(d["task_telemetry"]["cache_write_tokens"], 0)
        self.assertEqual(d["task_telemetry"]["cache_read_tokens"], 0)
        self.assertIsNone(d["task_telemetry"]["reasoning_tokens"])
        self.assertEqual(d["stable_prefix_hash"], "sha256:abc")
        self.assertEqual(d["context_pack_hash"], "cp1-9f")

    def test_every_key_is_present_even_when_null(self):
        d = task_economics_to_dict(_record())
        for name in TASK_TOKEN_TELEMETRY_FIELDS:
            self.assertIn(name, d["task_telemetry"])
            self.assertIsNone(d["task_telemetry"][name])
        for name in TASK_ATTRIBUTION_HASH_FIELDS:
            self.assertIn(name, d)
            self.assertIsNone(d[name])

    def test_serialized_record_carries_no_fabricated_total(self):
        d = task_economics_to_dict(_record(output_tokens=100, reasoning_tokens=40))
        for forbidden in ("task_total_tokens", "telemetry_total", "total_task_tokens"):
            self.assertNotIn(forbidden, d)
            self.assertNotIn(forbidden, d["task_telemetry"])

    def test_standalone_telemetry_dict_round_trips(self):
        t = TaskTokenTelemetry(uncached_input_tokens=1, cache_read_tokens=0)
        self.assertEqual(task_token_telemetry_from_dict(task_token_telemetry_to_dict(t)), t)


class FixtureIntegrationTests(unittest.TestCase):
    """Cross-repo: one Agent Crew-shaped row -> one serialized TaskEconomics."""

    def setUp(self):
        self.attributions = read_attribution_jsonl(FIXTURE / "attribution.jsonl")
        self.by_task = {a.task_id: a for a in self.attributions}

    def test_fixture_matches_the_real_row_shape(self):
        """Every key set here came from an observed post-#317 row."""
        raw = [json.loads(l) for l in open(FIXTURE / "attribution.jsonl") if l.strip()]
        measured = next(r for r in raw if r["task_id"] == "impl-measured-resume")
        for name in TASK_TOKEN_TELEMETRY_FIELDS + TASK_ATTRIBUTION_HASH_FIELDS:
            self.assertIn(name, measured)
        historical = next(r for r in raw if r["task_id"] == "impl-pre-317-historical")
        for name in TASK_TOKEN_TELEMETRY_FIELDS + TASK_ATTRIBUTION_HASH_FIELDS:
            self.assertNotIn(name, historical)

    def test_one_row_becomes_one_record_with_components_intact(self):
        a = self.by_task["impl-measured-resume"]
        records = correlate_task_economics([a], [])
        self.assertEqual(len(records), 1)
        d = task_economics_to_dict(records[0])
        self.assertEqual(d["task_id"], "impl-measured-resume")
        self.assertEqual(d["task_telemetry"]["uncached_input_tokens"], 1840)
        self.assertEqual(d["task_telemetry"]["cache_write_tokens"], 0)
        self.assertEqual(d["task_telemetry"]["cache_read_tokens"], 118450)
        self.assertEqual(d["task_telemetry"]["output_tokens"], 3120)
        self.assertEqual(d["task_telemetry"]["reasoning_tokens"], 910)
        self.assertEqual(d["task_telemetry"]["context_window_tokens"], 124300)
        self.assertEqual(d["stable_prefix_hash"], "sha256:1f2e3d4c5b6a7988")
        self.assertEqual(d["context_pack_hash"], "cp1-abcd1234")

    def test_partial_provider_keeps_unreported_components_unknown(self):
        t = self.by_task["review-partial-provider"].task_telemetry
        self.assertEqual(t.uncached_input_tokens, 9800)
        self.assertIsNone(t.reasoning_tokens)
        self.assertIsNone(t.context_window_tokens)
        self.assertIsNone(t.cache_write_tokens)

    def test_measured_fresh_row_keeps_its_genuine_zero(self):
        t = self.by_task["impl-measured-fresh"].task_telemetry
        self.assertEqual(t.cache_read_tokens, 0)
        self.assertEqual(t.reasoning_tokens, 0)
        self.assertEqual(t.cache_write_tokens, 24600)

    def test_all_null_and_pre_317_rows_parse_identically(self):
        """Explicit nulls and absent keys are the same fact: unknown."""
        self.assertEqual(
            self.by_task["test-all-null"].task_telemetry,
            self.by_task["impl-pre-317-historical"].task_telemetry,
        )
        self.assertFalse(self.by_task["test-all-null"].task_telemetry.has_any_observation)

    def test_summary_over_the_fixture_excludes_unknowns_from_every_mean(self):
        records = correlate_task_economics(self.attributions, [])
        s = task_token_telemetry_summary(records)
        self.assertEqual(s["uncached_input_tokens"]["total_row_count"], 5)
        self.assertEqual(s["uncached_input_tokens"]["known_count"], 3)
        self.assertEqual(s["uncached_input_tokens"]["unknown_count"], 2)
        # reasoning: measured on two rows (910 and 0), unknown on three.
        self.assertEqual(s["reasoning_tokens"]["known_count"], 2)
        self.assertEqual(s["reasoning_tokens"]["total"], 910)


class BoundaryTests(unittest.TestCase):
    def test_no_agent_crew_import_anywhere_in_the_package(self):
        import quota_core.context_economics as pkg

        root = Path(pkg.__file__).parent
        for path in root.glob("*.py"):
            source = path.read_text()
            for line in source.splitlines():
                stripped = line.strip()
                self.assertFalse(stripped.startswith("import agent_crew"), path.name)
                self.assertFalse(stripped.startswith("from agent_crew"), path.name)


if __name__ == "__main__":
    unittest.main()
