"""Public, provider-neutral context-economics schema.

These types describe *what a runtime task consumed and how*, independent of
any single orchestrator (Agent Crew or otherwise) and independent of any
single provider (Claude, Codex, Gemini, ...).

Versioning: bump ``SCHEMA_VERSION`` whenever a field is removed or an
existing field's meaning changes. Adding new optional fields does not
require a bump. Consumers should treat an unknown/newer ``schema_version``
as "parse tolerantly, do not assume new semantics".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

SCHEMA_VERSION = 1

ContextPolicy = Literal["resume", "compact", "fresh", "unknown"]

LifecycleEventType = Literal[
    "context_created",
    "context_resumed",
    "context_compacted",
    "context_reset",
    "context_recovered",
    "provider_fallback",
    "task_started",
    "task_completed",
    "task_failed",
]

_LIFECYCLE_EVENT_TYPES: tuple[str, ...] = (
    "context_created",
    "context_resumed",
    "context_compacted",
    "context_reset",
    "context_recovered",
    "provider_fallback",
    "task_started",
    "task_completed",
    "task_failed",
)

_CONTEXT_POLICIES: tuple[str, ...] = ("resume", "compact", "fresh", "unknown")

AttributionConfidence = Literal["high", "medium", "low"]

NormalizedOutcome = Literal["success", "failed", "unknown"]

# Real Agent Crew outcome values observed in production attribution.jsonl:
# "" (in progress), "completed", "failed", "failed:<reason>" (colon-delimited
# sub-reason). Match on the prefix before ":" so "failed:dispatcher_timeout"
# still normalizes to "failed".
_SUCCESS_OUTCOME_PREFIXES = {"success", "completed", "done", "ok"}
_FAILURE_OUTCOME_PREFIXES = {"failed", "error", "cancelled", "canceled", "timeout"}

FailureCategory = Literal[
    "context_or_policy",
    "provider_or_transport",
    "runtime_or_dispatcher",
    "work_product_or_test",
    "cancelled",
    "unknown",
]

_FAILURE_CATEGORIES: tuple[str, ...] = (
    "context_or_policy",
    "provider_or_transport",
    "runtime_or_dispatcher",
    "work_product_or_test",
    "cancelled",
    "unknown",
)

# quota-core issue #60: classify a failed task's raw reason into a small
# public category set so context-age/compact-policy reports can strip out
# failures that are not evidence about context handling. Markers below are
# grounded in reason strings actually observed locally (2026-08), not
# guessed:
#
# - `dispatcher_timeout`, `exit_1`, `agy_quota_exhausted`, `no_result_submitted`
#   -- from `~/.agent_crew/*/tasks.db` `tasks.error_info` (`{"reason": ...}`)
#   and the matching colon-encoded `attribution.jsonl` `outcome` values
#   (`"failed:dispatcher_timeout"`, `"failed:exit_1"`).
# - `claude_429`, `claude_throttle`, `gemini_capacity`,
#   `gemini_resource_exhausted`, `codex_capacity`, `agy_quota_exhausted`,
#   `agy_timeout` -- agent_crew's own dispatcher transient-error detector
#   (`_detect_transient_error_in_log` in agent_crew's `server.py`, read
#   read-only from the local agent_crew checkout for ground truth; quota_core
#   does not import agent_crew). These are exactly the signatures agent_crew
#   already treats as "upstream throttle, requeue" rather than a real
#   failure.
#
# `agy_subscriber_lag` / the "subscriber fell behind" AGY streaming-backpressure
# signature from issue #60's agent_crew#205/#206 incident was not present in
# any locally observed `error_info`/`outcome` value as of this change -- the
# dispatcher fix that would tag it is referenced by the issue but not
# confirmed shipped in this checkout. It is included here on the same
# `agy_`-prefixed / backpressure-marker pattern as the other AGY transient
# tags above, since that is the most defensible generalization from real
# data rather than a guess; see agent_crew_adapter.py's module docstring for
# the exact missing-field callout this relies on.
_PROVIDER_TRANSPORT_MARKERS: tuple[str, ...] = (
    "429",
    "throttle",
    "rate_limit",
    "capacity",
    "resource_exhausted",
    "quota_exhausted",
    "subscriber_lag",
    "subscriber_fell_behind",
    "backpressure",
    "agy_quota",
    "agy_timeout",
    "agy_subscriber",
)

# `dispatcher_timeout` is agent_crew's *own* orchestration layer giving up on
# a task past its deadline -- distinct from `agy_timeout` above (the
# underlying provider CLI/tool itself hanging, which agent_crew already
# treats as a transient provider/transport signature). `no_result_submitted`
# is the same shape: the dispatcher never got a terminal result from the
# agent process, which is an orchestration/runtime failure mode, not
# evidence about the task's own context or work product.
_RUNTIME_DISPATCHER_MARKERS: tuple[str, ...] = (
    "dispatcher",
    "no_result_submitted",
    "orchestrat",
)

# `exit_1` (and other nonzero exit codes) is the one real observed reason
# that reflects the agent's own process/test run failing on its merits --
# the closest thing to a "real" (non-operational) failure in the currently
# observed data.
_WORK_PRODUCT_MARKERS: tuple[str, ...] = (
    "exit_1",
    "exit_code",
    "test_fail",
    "lint_fail",
    "assertion",
)

# No reason string observed locally (attribution.jsonl outcome / tasks.db
# error_info.reason) currently encodes a context/policy cause -- Agent
# Crew's real contract has no field that positively attributes a failure to
# stale/overlong context or a bad compact/resume decision (see
# agent_crew_adapter.py's module docstring). These markers exist so a future
# producer that *does* start tagging this is picked up automatically instead
# of requiring another quota_core release; today they should never match.
_CONTEXT_POLICY_MARKERS: tuple[str, ...] = (
    "context_stale",
    "context_overflow",
    "compact_fail",
    "resume_fail",
    "context_or_policy",
)

# Real Agent Crew attribution rows only ever set `agent` to one of these --
# there is no separate `provider` field in the real contract. Used to derive
# `provider` when the source data doesn't supply one explicitly, per
# quota-core issue #58 point 4 ("derive/normalize provider from known Agent
# Crew agent identities where deterministic, otherwise preserve unknown").
_KNOWN_AGENT_PROVIDERS = {"claude", "codex", "gemini"}


def normalize_outcome(raw: str | None) -> NormalizedOutcome | None:
    """Map a runtime's raw outcome string to success/failed/unknown.

    Returns ``None`` for an empty/missing outcome (task still in progress,
    or outcome genuinely not reported) -- that is distinct from a real
    ``"unknown"`` terminal outcome the runtime explicitly reported.
    """

    if not raw:
        return None
    prefix = raw.split(":", 1)[0].strip().lower()
    if prefix in _SUCCESS_OUTCOME_PREFIXES:
        return "success"
    if prefix in _FAILURE_OUTCOME_PREFIXES:
        return "failed"
    return "unknown"


def extract_failure_reason(raw_outcome: str | None) -> str | None:
    """Extract the colon-delimited reason segment from a raw outcome string.

    Mirrors :func:`normalize_outcome`'s own prefix split:
    ``"failed:dispatcher_timeout"`` -> ``"dispatcher_timeout"``. Returns
    ``None`` for a bare ``"failed"`` (no reason reported) or an empty/missing
    raw outcome -- never fabricates a reason that was not actually there.
    """

    if not raw_outcome or ":" not in raw_outcome:
        return None
    _, _, reason = raw_outcome.partition(":")
    reason = reason.strip()
    return reason or None


def classify_failure_category(
    outcome: NormalizedOutcome | None,
    raw_outcome: str | None,
) -> FailureCategory | None:
    """Map a failed task's raw outcome/reason to the public failure-category set.

    Returns ``None`` when ``outcome`` is not ``"failed"`` -- category is not
    applicable to a success or an in-progress/unknown-outcome task. For a
    genuine failure, defaults to ``"unknown"`` whenever the reason is missing
    or does not match a recognized pattern; this function never guesses a
    more specific category than the evidence supports (quota-core issue #60:
    "do not fabricate a category when evidence is insufficient").

    ``"context_or_policy"`` is currently unreachable from any reason string
    Agent Crew's real contract is observed to emit -- see the module-level
    marker tables above and ``agent_crew_adapter.py``'s docstring for the
    missing producer metadata this would need.
    """

    if outcome != "failed":
        return None
    reason = extract_failure_reason(raw_outcome)
    candidate = reason if reason is not None else (raw_outcome or "").strip()
    lowered = candidate.lower()
    if not lowered:
        return "unknown"
    if lowered in {"cancelled", "canceled"} or "cancel" in lowered:
        return "cancelled"
    if any(marker in lowered for marker in _CONTEXT_POLICY_MARKERS):
        return "context_or_policy"
    if any(marker in lowered for marker in _PROVIDER_TRANSPORT_MARKERS):
        return "provider_or_transport"
    if any(marker in lowered for marker in _RUNTIME_DISPATCHER_MARKERS):
        return "runtime_or_dispatcher"
    if any(marker in lowered for marker in _WORK_PRODUCT_MARKERS):
        return "work_product_or_test"
    return "unknown"


def infer_retryable(category: FailureCategory | None) -> bool | None:
    """Best-effort retryability inferred from a failure category.

    Mirrors agent_crew's own dispatcher (``_detect_transient_error_in_log``):
    only the provider/transport transient signatures are currently
    auto-retried there. Returns ``None`` (neither True nor False) for
    ``"unknown"`` or a non-failure -- there isn't enough evidence to claim
    either way, and a bare ``False`` would overstate what is actually known.
    """

    if category is None or category == "unknown":
        return None
    return category == "provider_or_transport"


def parse_flexible_timestamp(value: Any) -> int | None:
    """Parse a timestamp that may be a unix epoch (int/float/numeric string)
    or an ISO-8601 string (Agent Crew's real ``ts`` field, e.g.
    ``"2026-08-21T23:56:19.497696"`` -- naive, implicitly UTC).
    """

    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            pass
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    return None


@dataclass(frozen=True)
class TokenComponents:
    """Provider-reported token usage, kept as separate economic categories.

    Fields are ``None`` when the provider/telemetry source does not expose
    that component -- callers must not fabricate a breakdown for providers
    that only report a total. ``provider_total`` is the provider's own
    reported total when available; :func:`token_components_total` derives a
    best-effort total when it is not.
    """

    fresh_input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_creation: int | None = None
    tool_tokens: int | None = None
    provider_total: int | None = None

    @property
    def has_full_breakdown(self) -> bool:
        """True when fresh/output/cache components are all known."""

        return self.fresh_input is not None and self.output is not None and self.cache_read is not None and self.cache_creation is not None

    @property
    def known_components(self) -> tuple[str, ...]:
        """Names of components that are actually populated."""

        names = []
        for name in ("fresh_input", "output", "cache_read", "cache_creation", "tool_tokens", "provider_total"):
            if getattr(self, name) is not None:
                names.append(name)
        return tuple(names)


def token_components_total(components: TokenComponents) -> int | None:
    """Best-effort total tokens: provider-reported total, else sum of known parts.

    Returns ``None`` when nothing is known at all (fully unobserved usage),
    which is distinct from a real ``0``.
    """

    if components.provider_total is not None:
        return components.provider_total
    parts = [components.fresh_input, components.output, components.cache_read, components.cache_creation, components.tool_tokens]
    known = [part for part in parts if part is not None]
    if not known:
        return None
    return sum(known)


@dataclass(frozen=True)
class RuntimeAttribution:
    """Provider-neutral runtime/context attribution for one task execution.

    Mirrors the durable attribution contract Agent Crew issue #202 documents
    (``schema_version`` .. ``outcome``), but this type must never depend on
    Agent Crew code -- it is populated by adapters (e.g.
    :mod:`quota_core.context_economics.agent_crew_adapter`) that translate a
    specific runtime's telemetry into this shape.

    Not the same concept as ``quota_core.session.report``'s existing
    ``runtime_attribution``/``reconciliation`` blocks (a human-vs-runtime
    token-usage split for one session, reconciled against the local quota
    scanner). This type is per-*task* orchestrator provenance (which agent,
    role, context, and generation ran a given task) -- the two do not
    currently share data or code, and a session-level report may reference
    both independently.
    """

    runtime: str
    task_id: str
    schema_version: int = SCHEMA_VERSION
    project: str | None = None
    task_type: str | None = None
    role: str | None = None
    agent: str | None = None
    provider: str | None = None
    model: str | None = None
    context_id: str | None = None
    provider_session_id: str | None = None
    context_policy: ContextPolicy = "unknown"
    context_generation: int | None = None
    session_task_index: int | None = None
    previous_task_id: str | None = None
    retry_of: str | None = None
    fallback_of: str | None = None
    started_at: int | None = None
    completed_at: int | None = None
    updated_at: int | None = None
    outcome: NormalizedOutcome | None = None
    raw_outcome: str | None = None
    failure_reason: str | None = None
    failure_category: FailureCategory | None = None
    retryable: bool | None = None
    terminal_source: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextLifecycleEvent:
    """One append-only context/task lifecycle event."""

    event_type: LifecycleEventType
    runtime: str
    timestamp: int
    schema_version: int = SCHEMA_VERSION
    project: str | None = None
    task_id: str | None = None
    context_id: str | None = None
    provider_session_id: str | None = None
    provider: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskEconomicsRecord:
    """A task-level context-economics record: attribution joined with usage."""

    task_id: str
    runtime: str
    tokens: TokenComponents = field(default_factory=TokenComponents)
    project: str | None = None
    provider: str | None = None
    model: str | None = None
    role: str | None = None
    agent: str | None = None
    task_type: str | None = None
    context_id: str | None = None
    provider_session_id: str | None = None
    context_policy: ContextPolicy = "unknown"
    context_generation: int | None = None
    session_task_index: int | None = None
    retry_of: str | None = None
    fallback_of: str | None = None
    started_at: int | None = None
    completed_at: int | None = None
    outcome: NormalizedOutcome | None = None
    raw_outcome: str | None = None
    failure_reason: str | None = None
    failure_category: FailureCategory | None = None
    retryable: bool | None = None
    terminal_source: str | None = None
    attribution_confidence: AttributionConfidence = "low"
    attribution_notes: tuple[str, ...] = ()

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.completed_at is None:
            return None
        return max(0.0, float(self.completed_at - self.started_at))

    @property
    def succeeded(self) -> bool:
        return self.outcome == "success"


# --- dict (de)serialization, following quota_core.snapshot's conventions ---


def token_components_to_dict(components: TokenComponents) -> dict[str, Any]:
    return {
        "fresh_input": components.fresh_input,
        "output": components.output,
        "cache_read": components.cache_read,
        "cache_creation": components.cache_creation,
        "tool_tokens": components.tool_tokens,
        "provider_total": components.provider_total,
    }


def token_components_from_dict(data: dict[str, Any]) -> TokenComponents:
    def _opt_int(key: str) -> int | None:
        value = data.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return TokenComponents(
        fresh_input=_opt_int("fresh_input"),
        output=_opt_int("output"),
        cache_read=_opt_int("cache_read"),
        cache_creation=_opt_int("cache_creation"),
        tool_tokens=_opt_int("tool_tokens"),
        provider_total=_opt_int("provider_total"),
    )


def attribution_to_dict(attribution: RuntimeAttribution) -> dict[str, Any]:
    return {
        "runtime": attribution.runtime,
        "task_id": attribution.task_id,
        "schema_version": attribution.schema_version,
        "project": attribution.project,
        "task_type": attribution.task_type,
        "role": attribution.role,
        "agent": attribution.agent,
        "provider": attribution.provider,
        "model": attribution.model,
        "context_id": attribution.context_id,
        "provider_session_id": attribution.provider_session_id,
        "context_policy": attribution.context_policy,
        "context_generation": attribution.context_generation,
        "session_task_index": attribution.session_task_index,
        "previous_task_id": attribution.previous_task_id,
        "retry_of": attribution.retry_of,
        "fallback_of": attribution.fallback_of,
        "started_at": attribution.started_at,
        "completed_at": attribution.completed_at,
        "updated_at": attribution.updated_at,
        "outcome": attribution.outcome,
        "raw_outcome": attribution.raw_outcome,
        "failure_reason": attribution.failure_reason,
        "failure_category": attribution.failure_category,
        "retryable": attribution.retryable,
        "terminal_source": attribution.terminal_source,
        "extra": dict(attribution.extra),
    }


def attribution_from_dict(data: dict[str, Any]) -> RuntimeAttribution:
    """Build a :class:`RuntimeAttribution` from a raw dict, tolerantly.

    Handles both the original synthetic-fixture shape and Agent Crew's real
    ``attribution.jsonl`` contract (quota-core issue #58):

    - timestamps may be unix epoch (int/float) or ISO-8601 strings,
    - ``outcome`` may be Agent Crew's real values (``""``/``"completed"``/
      ``"failed:<reason>"``) -- normalized into :data:`NormalizedOutcome`,
      with the original string preserved as ``raw_outcome``,
    - ``provider`` is derived from ``agent`` when the real contract doesn't
      supply a separate ``provider`` field and ``agent`` is a known identity
      (claude/codex/gemini); otherwise it stays unknown rather than guessed,
    - empty-string optional fields (Agent Crew writes ``""`` for "not set",
      e.g. ``provider_session_id``) normalize to ``None``.
    - ``failure_reason``/``failure_category``/``retryable``/``terminal_source``
      (quota-core issue #60) are derived from ``outcome`` when the source
      data doesn't supply them explicitly: ``failure_reason`` from the
      colon-delimited suffix of a real ``"failed:<reason>"`` outcome,
      ``failure_category`` via :func:`classify_failure_category`, and
      ``retryable`` via :func:`infer_retryable`. A future producer that
      supplies these explicitly wins over the derived value.
      ``terminal_source`` has no derivation -- Agent Crew's current contract
      does not expose it at all (see ``agent_crew_adapter.py``), so it stays
      ``None`` unless a source dict explicitly provides one.

    Unknown top-level keys are preserved under ``extra`` so forward-compatible
    fields are not silently dropped. Missing/older fields fall back to safe
    defaults instead of raising.
    """

    known_keys = {
        "runtime", "task_id", "schema_version", "project", "task_type", "role", "agent",
        "provider", "model", "context_id", "provider_session_id", "context_policy",
        "context_generation", "session_task_index", "previous_task_id", "retry_of",
        "fallback_of", "started_at", "completed_at", "updated_at", "outcome", "raw_outcome",
        "failure_reason", "failure_category", "retryable", "terminal_source", "extra",
    }
    extra = dict(data.get("extra") or {}) if isinstance(data.get("extra"), dict) else {}
    for key, value in data.items():
        if key not in known_keys:
            extra[key] = value

    context_policy = data.get("context_policy") or "unknown"
    if context_policy not in _CONTEXT_POLICIES:
        context_policy = "unknown"

    def _opt_int(key: str) -> int | None:
        value = data.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _opt_str(key: str) -> str | None:
        value = data.get(key)
        if value is None or value == "":
            return None
        return str(value)

    agent = _opt_str("agent")
    provider = _opt_str("provider")
    if provider is None and agent is not None and agent.lower() in _KNOWN_AGENT_PROVIDERS:
        provider = agent.lower()

    # Real Agent Crew producer dicts only ever set "outcome" (the raw string,
    # e.g. "failed:dispatcher_timeout"). attribution_to_dict's own output
    # additionally sets "raw_outcome" (the already-separated raw string,
    # alongside the *normalized* "outcome"). Prefer an explicit "raw_outcome"
    # when present so to_dict -> from_dict round-trips exactly; otherwise
    # fall back to "outcome" per the real contract -- this is a no-op for
    # real producer data, which never sets "raw_outcome".
    raw_outcome = _opt_str("raw_outcome")
    if raw_outcome is None:
        raw_outcome = _opt_str("outcome")
    outcome = normalize_outcome(raw_outcome)

    failure_reason = _opt_str("failure_reason")
    if failure_reason is None:
        failure_reason = extract_failure_reason(raw_outcome)

    failure_category = data.get("failure_category")
    if failure_category not in _FAILURE_CATEGORIES:
        failure_category = classify_failure_category(outcome, raw_outcome)

    retryable = data.get("retryable")
    if not isinstance(retryable, bool):
        retryable = infer_retryable(failure_category)

    return RuntimeAttribution(
        runtime=str(data.get("runtime") or "unknown"),
        task_id=str(data.get("task_id") or ""),
        schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        project=_opt_str("project"),
        task_type=_opt_str("task_type"),
        role=_opt_str("role"),
        agent=agent,
        provider=provider,
        model=_opt_str("model"),
        context_id=_opt_str("context_id"),
        provider_session_id=_opt_str("provider_session_id"),
        context_policy=context_policy,  # type: ignore[arg-type]
        context_generation=_opt_int("context_generation"),
        session_task_index=_opt_int("session_task_index"),
        previous_task_id=_opt_str("previous_task_id"),
        retry_of=_opt_str("retry_of"),
        fallback_of=_opt_str("fallback_of"),
        started_at=parse_flexible_timestamp(data.get("started_at")),
        completed_at=parse_flexible_timestamp(data.get("completed_at")),
        updated_at=parse_flexible_timestamp(data.get("updated_at")),
        outcome=outcome,
        raw_outcome=raw_outcome,
        failure_reason=failure_reason,
        failure_category=failure_category,  # type: ignore[arg-type]
        retryable=retryable,
        terminal_source=_opt_str("terminal_source"),
        extra=extra,
    )


def lifecycle_event_to_dict(event: ContextLifecycleEvent) -> dict[str, Any]:
    return {
        "event_type": event.event_type,
        "runtime": event.runtime,
        "timestamp": event.timestamp,
        "schema_version": event.schema_version,
        "project": event.project,
        "task_id": event.task_id,
        "context_id": event.context_id,
        "provider_session_id": event.provider_session_id,
        "provider": event.provider,
        "extra": dict(event.extra),
    }


def lifecycle_event_from_dict(data: dict[str, Any]) -> ContextLifecycleEvent | None:
    """Parse one lifecycle event dict; returns ``None`` for unrecognized event types.

    Tolerant by design: an unknown/future ``event_type`` is skipped rather
    than raising, so an older adapter can keep reading a newer event stream.

    Accepts Agent Crew's real ``ts`` field (an ISO-8601 string, e.g.
    ``"2026-08-21T23:56:19.497696"``) in addition to the original synthetic
    ``timestamp`` (unix epoch int) -- quota-core issue #58 point 1. ``ts``
    is preferred when both are present.
    """

    known_keys = {
        "event_type", "runtime", "timestamp", "ts", "schema_version", "project", "task_id",
        "context_id", "provider_session_id", "provider", "extra",
    }
    event_type = data.get("event_type")
    if event_type not in _LIFECYCLE_EVENT_TYPES:
        return None
    timestamp = parse_flexible_timestamp(data.get("ts"))
    if timestamp is None:
        timestamp = parse_flexible_timestamp(data.get("timestamp"))
    if timestamp is None:
        return None

    extra = dict(data.get("extra") or {}) if isinstance(data.get("extra"), dict) else {}
    for key, value in data.items():
        if key not in known_keys:
            extra[key] = value

    def _opt_str(key: str) -> str | None:
        value = data.get(key)
        return str(value) if value is not None else None

    return ContextLifecycleEvent(
        event_type=event_type,  # type: ignore[arg-type]
        runtime=str(data.get("runtime") or "unknown"),
        timestamp=timestamp,
        schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        project=_opt_str("project"),
        task_id=_opt_str("task_id"),
        context_id=_opt_str("context_id"),
        provider_session_id=_opt_str("provider_session_id"),
        provider=_opt_str("provider"),
        extra=extra,
    )


def task_economics_to_dict(record: TaskEconomicsRecord) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "runtime": record.runtime,
        "tokens": token_components_to_dict(record.tokens),
        "project": record.project,
        "provider": record.provider,
        "model": record.model,
        "role": record.role,
        "agent": record.agent,
        "task_type": record.task_type,
        "context_id": record.context_id,
        "provider_session_id": record.provider_session_id,
        "context_policy": record.context_policy,
        "context_generation": record.context_generation,
        "session_task_index": record.session_task_index,
        "retry_of": record.retry_of,
        "fallback_of": record.fallback_of,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "outcome": record.outcome,
        "raw_outcome": record.raw_outcome,
        "failure_reason": record.failure_reason,
        "failure_category": record.failure_category,
        "retryable": record.retryable,
        "terminal_source": record.terminal_source,
        "attribution_confidence": record.attribution_confidence,
        "attribution_notes": list(record.attribution_notes),
    }


def validate_attribution_dict(data: dict[str, Any]) -> tuple[str, ...]:
    """Return schema validation errors for a raw attribution dict."""

    errors: list[str] = []
    if not isinstance(data.get("runtime"), str) or not data.get("runtime"):
        errors.append("runtime must be a non-empty string")
    if not isinstance(data.get("task_id"), str) or not data.get("task_id"):
        errors.append("task_id must be a non-empty string")
    if "context_policy" in data and data.get("context_policy") not in _CONTEXT_POLICIES:
        errors.append("context_policy must be one of resume|compact|fresh|unknown")
    if "failure_category" in data and data.get("failure_category") not in _FAILURE_CATEGORIES:
        errors.append(
            "failure_category must be one of context_or_policy|provider_or_transport|"
            "runtime_or_dispatcher|work_product_or_test|cancelled|unknown"
        )
    if "retryable" in data and data.get("retryable") is not None and not isinstance(data.get("retryable"), bool):
        errors.append("retryable must be a boolean or null")
    for key in ("context_generation", "session_task_index"):
        value = data.get(key)
        if value is not None and not isinstance(value, int):
            errors.append(f"{key} must be an integer or null")
    # started_at/completed_at/updated_at accept unix epoch (int/float) or an
    # ISO-8601 string (Agent Crew's real `ts`-style timestamps) -- only flag
    # a value that parses as neither.
    for key in ("started_at", "completed_at", "updated_at"):
        value = data.get(key)
        if value is not None and parse_flexible_timestamp(value) is None:
            errors.append(f"{key} must be a unix epoch number, an ISO-8601 string, or null")
    return tuple(errors)


__all__ = [
    "SCHEMA_VERSION",
    "ContextPolicy",
    "LifecycleEventType",
    "AttributionConfidence",
    "NormalizedOutcome",
    "FailureCategory",
    "normalize_outcome",
    "extract_failure_reason",
    "classify_failure_category",
    "infer_retryable",
    "parse_flexible_timestamp",
    "TokenComponents",
    "token_components_total",
    "RuntimeAttribution",
    "ContextLifecycleEvent",
    "TaskEconomicsRecord",
    "token_components_to_dict",
    "token_components_from_dict",
    "attribution_to_dict",
    "attribution_from_dict",
    "lifecycle_event_to_dict",
    "lifecycle_event_from_dict",
    "task_economics_to_dict",
    "validate_attribution_dict",
]
