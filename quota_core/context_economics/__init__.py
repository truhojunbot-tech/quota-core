"""Provider-neutral context economics / session efficiency measurement layer.

This subpackage answers: how efficiently is an AI runtime using context, and
what does the data suggest? It is independent of any single orchestrator
(Agent Crew or otherwise) and works from raw Claude/Codex/Gemini telemetry
even when no orchestrator-level attribution is present.

See ``docs/context-economics.md`` for the full public contract.
"""

from __future__ import annotations

from .analytics import (
    UNKNOWN_FAILURE_CAUSE_WARNING,
    cache_creation_per_successful_task,
    cache_read_per_task,
    compare_context_policies,
    context_age_vs_failure_rate,
    context_age_vs_token_usage,
    failed_retry_token_waste,
    fresh_input_per_successful_task,
    stratified_failure_rates,
    tokens_per_outcome,
    tokens_per_successful_task,
)
from .compact_analysis import before_after_compact
from .correlate import ProviderUsageRecord, correlate_task_economics
from .agent_crew_adapter import reconcile_attribution_by_task
from .schema import (
    SCHEMA_VERSION,
    ContextLifecycleEvent,
    ContextPolicy,
    FailureCategory,
    LifecycleEventType,
    NormalizedOutcome,
    RuntimeAttribution,
    TaskEconomicsRecord,
    TokenComponents,
    attribution_from_dict,
    attribution_to_dict,
    classify_failure_category,
    extract_failure_reason,
    infer_retryable,
    lifecycle_event_from_dict,
    lifecycle_event_to_dict,
    normalize_outcome,
    parse_flexible_timestamp,
    task_economics_to_dict,
    token_components_from_dict,
    token_components_to_dict,
    token_components_total,
    validate_attribution_dict,
)
from .token_components import (
    claude_token_components,
    codex_token_components,
    gemini_token_components,
    merge_token_components,
    token_components_for_provider,
)

__all__ = [
    "SCHEMA_VERSION",
    "ContextPolicy",
    "LifecycleEventType",
    "NormalizedOutcome",
    "FailureCategory",
    "normalize_outcome",
    "extract_failure_reason",
    "classify_failure_category",
    "infer_retryable",
    "parse_flexible_timestamp",
    "reconcile_attribution_by_task",
    "TokenComponents",
    "RuntimeAttribution",
    "ContextLifecycleEvent",
    "TaskEconomicsRecord",
    "ProviderUsageRecord",
    "attribution_to_dict",
    "attribution_from_dict",
    "lifecycle_event_to_dict",
    "lifecycle_event_from_dict",
    "task_economics_to_dict",
    "token_components_to_dict",
    "token_components_from_dict",
    "token_components_total",
    "validate_attribution_dict",
    "claude_token_components",
    "codex_token_components",
    "gemini_token_components",
    "token_components_for_provider",
    "merge_token_components",
    "correlate_task_economics",
    "fresh_input_per_successful_task",
    "cache_creation_per_successful_task",
    "cache_read_per_task",
    "failed_retry_token_waste",
    "tokens_per_successful_task",
    "tokens_per_outcome",
    "context_age_vs_token_usage",
    "context_age_vs_failure_rate",
    "compare_context_policies",
    "stratified_failure_rates",
    "UNKNOWN_FAILURE_CAUSE_WARNING",
    "before_after_compact",
]
