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
    outcome: str | None = None
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
    outcome: str | None = None
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
        "outcome": attribution.outcome,
        "extra": dict(attribution.extra),
    }


def attribution_from_dict(data: dict[str, Any]) -> RuntimeAttribution:
    """Build a :class:`RuntimeAttribution` from a raw dict, tolerantly.

    Unknown top-level keys are preserved under ``extra`` so forward-compatible
    fields are not silently dropped. Missing/older fields fall back to safe
    defaults instead of raising.
    """

    known_keys = {
        "runtime", "task_id", "schema_version", "project", "task_type", "role", "agent",
        "provider", "model", "context_id", "provider_session_id", "context_policy",
        "context_generation", "session_task_index", "previous_task_id", "retry_of",
        "fallback_of", "started_at", "completed_at", "outcome", "extra",
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
        return str(value) if value is not None else None

    return RuntimeAttribution(
        runtime=str(data.get("runtime") or "unknown"),
        task_id=str(data.get("task_id") or ""),
        schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        project=_opt_str("project"),
        task_type=_opt_str("task_type"),
        role=_opt_str("role"),
        agent=_opt_str("agent"),
        provider=_opt_str("provider"),
        model=_opt_str("model"),
        context_id=_opt_str("context_id"),
        provider_session_id=_opt_str("provider_session_id"),
        context_policy=context_policy,  # type: ignore[arg-type]
        context_generation=_opt_int("context_generation"),
        session_task_index=_opt_int("session_task_index"),
        previous_task_id=_opt_str("previous_task_id"),
        retry_of=_opt_str("retry_of"),
        fallback_of=_opt_str("fallback_of"),
        started_at=_opt_int("started_at"),
        completed_at=_opt_int("completed_at"),
        outcome=_opt_str("outcome"),
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
    """

    known_keys = {
        "event_type", "runtime", "timestamp", "schema_version", "project", "task_id",
        "context_id", "provider_session_id", "provider", "extra",
    }
    event_type = data.get("event_type")
    if event_type not in _LIFECYCLE_EVENT_TYPES:
        return None
    raw_timestamp = data.get("timestamp")
    if raw_timestamp is None:
        return None
    try:
        timestamp = int(raw_timestamp)
    except (TypeError, ValueError):
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
    for key in ("started_at", "completed_at", "context_generation", "session_task_index"):
        value = data.get(key)
        if value is not None and not isinstance(value, int):
            errors.append(f"{key} must be an integer or null")
    return tuple(errors)


__all__ = [
    "SCHEMA_VERSION",
    "ContextPolicy",
    "LifecycleEventType",
    "AttributionConfidence",
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
