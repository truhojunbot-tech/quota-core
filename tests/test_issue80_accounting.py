"""quota-core#80 accounting correctness: one charge per usage record, no
fabricated totals, and configured pricing reaching the policy decision."""

from __future__ import annotations

import unittest
from dataclasses import replace

from quota_core.context_economics import (
    PricingBook,
    ProviderUsageRecord,
    RuntimeAttribution,
    TokenComponents,
    claude_token_components,
    codex_token_components,
    correlate_task_economics,
    gemini_token_components,
    shadow_comparison_report,
    token_components_total,
)
from quota_core.context_economics.policy import recommend_task_policy
from quota_core.context_economics.schema import TaskEconomicsRecord, TaskTokenTelemetry


def _attr(task_id: str, **kw) -> RuntimeAttribution:
    return RuntimeAttribution(task_id=task_id, runtime="agent_crew", **kw)


class CorrelateClaimsEachRecordOnceTests(unittest.TestCase):
    def test_medium_tier_record_is_not_charged_to_two_tasks(self):
        # Two tasks in one session with no time windows: both fall to the
        # session-only tier and would each receive the same usage record.
        usage = [ProviderUsageRecord(provider="claude", provider_session_id="s",
                                     tokens=TokenComponents(provider_total=100))]
        out = correlate_task_economics([_attr("t1", provider_session_id="s"),
                                        _attr("t2", provider_session_id="s")], usage)
        charged = [r.tokens.provider_total for r in out]
        self.assertEqual(charged, [100, None])
        self.assertEqual([r.attribution_confidence for r in out], ["medium", "low"])

    def test_low_tier_record_is_not_charged_to_two_tasks(self):
        usage = [ProviderUsageRecord(provider="claude", project="p", started_at=110, completed_at=115,
                                     tokens=TokenComponents(provider_total=70))]
        out = correlate_task_economics([_attr("t1", project="p", started_at=100, completed_at=120),
                                        _attr("t2", project="p", started_at=105, completed_at=125)], usage)
        self.assertEqual(sum(r.tokens.provider_total or 0 for r in out), 70)

    def test_medium_tier_does_not_take_a_record_another_task_won_on_window(self):
        usage = [ProviderUsageRecord(provider="claude", provider_session_id="s", started_at=100,
                                     completed_at=110, tokens=TokenComponents(provider_total=5))]
        out = correlate_task_economics([_attr("t1", provider_session_id="s"),
                                        _attr("t2", provider_session_id="s", started_at=100, completed_at=110)], usage)
        self.assertEqual([r.tokens.provider_total for r in out], [None, 5])


    def test_merged_total_stays_unknown_unless_every_record_reports_one(self):
        # A reported 100-token record plus a distinct 50-output-token record
        # without a total is not a 100-token task: the total is unknown.
        usage = [ProviderUsageRecord(provider="claude", provider_session_id="s",
                                     tokens=TokenComponents(provider_total=100)),
                 ProviderUsageRecord(provider="claude", provider_session_id="s",
                                     tokens=TokenComponents(output=50))]
        [record] = correlate_task_economics([_attr("t", provider_session_id="s")], usage)
        self.assertIsNone(record.tokens.provider_total)
        self.assertIsNone(record.tokens.output)
        misrouted = replace(record, outcome="failed", orchestration_validity="misrouted")
        self.assertIsNone(recommend_task_policy(misrouted).orchestration_waste["misrouted_tokens"])

    def test_merged_total_sums_when_every_record_reports_one(self):
        usage = [ProviderUsageRecord(provider="claude", provider_session_id="s",
                                     tokens=TokenComponents(provider_total=100, tool_tokens=3)),
                 ProviderUsageRecord(provider="claude", provider_session_id="s",
                                     tokens=TokenComponents(provider_total=50))]
        [record] = correlate_task_economics([_attr("t", provider_session_id="s")], usage)
        self.assertEqual(record.tokens.provider_total, 150)
        self.assertEqual(record.tokens.tool_tokens, 3)
        misrouted = replace(record, outcome="failed", orchestration_validity="misrouted")
        self.assertEqual(recommend_task_policy(misrouted).orchestration_waste["misrouted_tokens"], 150)

class ProviderTotalIsReportedOnlyTests(unittest.TestCase):
    def test_partial_components_are_not_stored_as_a_provider_total(self):
        for extractor in (claude_token_components, codex_token_components, gemini_token_components):
            with self.subTest(extractor=extractor.__name__):
                self.assertIsNone(extractor({"output_tokens": 500}).provider_total)

    def test_partial_components_do_not_become_a_policy_total(self):
        record = TaskEconomicsRecord(task_id="t", runtime="agent_crew", outcome="failed",
                                     tokens=claude_token_components({"output_tokens": 500}),
                                     orchestration_validity="misrouted")
        waste = recommend_task_policy(record).orchestration_waste
        self.assertIsNone(waste["misrouted_tokens"])

    def test_reported_total_is_kept(self):
        self.assertEqual(codex_token_components({"output_tokens": 5, "total_tokens": 9}).provider_total, 9)
        self.assertEqual(claude_token_components({"total_tokens": 9}).provider_total, 9)

    def test_codex_reasoning_is_not_added_on_top_of_output(self):
        tokens = codex_token_components({"input_tokens": 500, "cached_input_tokens": 300,
                                         "output_tokens": 120, "reasoning_tokens": 80})
        # reasoning is a subset of output: 200 fresh + 300 cached + 120 output.
        self.assertEqual(token_components_total(tokens), 620)
        record = TaskEconomicsRecord(task_id="t", runtime="agent_crew", outcome="failed",
                                     tokens=TokenComponents(fresh_input=200, cache_creation=0, cache_read=300,
                                                            output=120, tool_tokens=tokens.tool_tokens),
                                     orchestration_validity="misrouted")
        self.assertEqual(recommend_task_policy(record).orchestration_waste["misrouted_tokens"], 620)

    def test_gemini_cached_content_is_not_counted_twice(self):
        tokens = gemini_token_components({"prompt_token_count": 500, "cached_content_token_count": 300,
                                          "candidates_token_count": 120})
        self.assertEqual(token_components_total(tokens), 620)

    def test_full_breakdown_still_totals_without_a_reported_total(self):
        tokens = claude_token_components({"input_tokens": 100, "output_tokens": 40,
                                          "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 800})
        self.assertEqual(token_components_total(tokens), 5940)


class ShadowReportPricingTests(unittest.TestCase):
    def test_policy_decision_component_costs_use_configured_pricing(self):
        record = TaskEconomicsRecord(task_id="t", runtime="agent_crew", provider="claude", model="m",
                                     outcome="success",
                                     task_telemetry=TaskTokenTelemetry(output_tokens=10))
        book = PricingBook.from_mapping({"claude": {"default": {"output": 2.0}}})
        artifact = shadow_comparison_report([record], pricing=book)["artifacts"][0]
        costs = artifact["policy_decision"]["component_costs"]["components"]
        self.assertEqual(costs["output"], 20.0)


if __name__ == "__main__":
    unittest.main()
