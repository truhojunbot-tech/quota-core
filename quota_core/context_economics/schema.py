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

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
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
    "context_pack_built",
    "test_scope_resolved",
    "test_stage_deferred",
    "provider_context_observed",
    "provider_context_capped",
    "provider_context_cleared",
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
    "context_pack_built",
    "test_scope_resolved",
    "test_stage_deferred",
    "provider_context_observed",
    "provider_context_capped",
    "provider_context_cleared",
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
# signature from issue #60's agent_crew#205/#206 incident is genuinely defined
# in agent_crew's own dispatcher (`server.py:258-262`'s docstring, implemented
# at `server.py:299-300`: `if "subscriber fell behind updates" in tail: return
# "agy_subscriber_lag"`) -- it is real, shipped code, not a guess. What is
# true is only that no task has *failed* with that reason in any locally
# observed `error_info`/`outcome` value as of this change (round-1 review,
# 2026-08: corrected from an earlier, wrong "absent/unconfirmed" claim here).
# It is included below alongside the other AGY transient tags.
#
# NOTE: despite the name, the markers below are NOT a true `agy_`-prefix
# match -- they are three literal tags (`agy_quota`, `agy_timeout`,
# `agy_subscriber`) matched by substring, same as every other marker in this
# tuple. A reason like `agy_transient` or `agy_stream_error` would NOT match
# any of these and falls through to `"unknown"`. A real `agy_` prefix rule
# was considered and deliberately not implemented: the only `agy_`-tagged
# reasons actually observed locally are the three literal tags already
# listed here (see `agent_crew_adapter.py`'s tasks.db error_info enrichment),
# so generalizing to every future `agy_*` tag would be exactly the kind of
# unguessed/unevidenced category fabrication issue #60 forbids.
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

# A bare/generic nonzero exit code (`exit_1`, `exit_code_137`, ...) is
# evidence-free: a crash, an OOM kill, a network drop, and a genuine failing
# test all produce an identical bare exit code, so it must NOT positively
# identify a "work product" (test/lint/assertion) failure -- round-1 review
# of quota-core issue #60 flagged this as the exact "false precision" the
# issue's own acceptance criteria forbid ("do not fabricate a category when
# evidence is insufficient"). Deliberately no `exit_*` marker is listed here;
# a bare exit code now falls through every marker table below to the
# `"unknown"` default, which is the honest bucket for it. `work_product_or_test`
# is reserved for reasons that positively identify a test/lint/assertion
# failure -- none of these markers are reachable from any reason string
# observed locally as of this change (same situation as
# `_CONTEXT_POLICY_MARKERS` below); they exist so a future producer that
# starts tagging this is picked up automatically.
_WORK_PRODUCT_MARKERS: tuple[str, ...] = (
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


# agent_crew's own dispatcher (`_detect_transient_error_in_log` in
# agent_crew's `server.py:231-300`, read read-only for ground truth) splits
# its transient-error tags into two explicit, disjoint groups -- not a single
# "provider/transport = retryable" rule. Round-1 review of quota-core issue
# #60 caught that deriving retryability from the coarse `failure_category`
# instead of these specific tags gets the single most common real failure
# reason wrong: `agy_quota_exhausted` is 321 of 413 (78%) of every failure
# reason observed locally in `~/.agent_crew/*/tasks.db`, and the dispatcher's
# own comment calls it "clear reason; no point in immediate retry" -- i.e.
# explicitly NOT retryable, even though it classifies as
# `"provider_or_transport"`.
_RETRYABLE_REASON_TAGS: tuple[str, ...] = (
    "claude_429",
    "claude_throttle",
    "gemini_capacity",
    "gemini_resource_exhausted",
    "codex_capacity",
    "agy_timeout",
    "agy_subscriber_lag",
)

# agent_crew's own "exhausted for hours+; retry is futile" group.
_NONRETRYABLE_REASON_TAGS: tuple[str, ...] = (
    "gemini_quota_exhausted",
    "gemini_ineligible_tier",
    "agy_quota_exhausted",
)


def infer_retryable(reason: str | None) -> bool | None:
    """Best-effort retryability derived from the specific failure reason tag.

    Mirrors agent_crew's own dispatcher (``_detect_transient_error_in_log``)
    verbatim: its explicitly-retryable tag group
    (:data:`_RETRYABLE_REASON_TAGS`) and its explicitly-non-retryable tag
    group (:data:`_NONRETRYABLE_REASON_TAGS`), matched by substring against
    ``reason`` the same way :func:`classify_failure_category`'s marker tables
    work. Deliberately does **not** derive from the coarser
    ``failure_category`` -- ``"provider_or_transport"`` alone conflates the
    two groups and gets ``agy_quota_exhausted`` (78% of all real observed
    failures locally) wrong.

    A reason containing ``"max_retries"`` (e.g. the real observed
    ``transient_agy_timeout_max_retries`` /
    ``transient_agy_subscriber_lag_max_retries`` tags in
    ``~/.agent_crew/*/tasks.db`` ``error_info``) is treated as non-retryable
    regardless of which tag it otherwise matches: the reason string itself
    documents that agent_crew's own dispatcher already exhausted its retry
    budget for this task, so claiming it is still retryable would
    contradict the evidence it's derived from.

    Returns ``None`` when ``reason`` is missing or matches neither explicit
    tag group -- there isn't enough evidence to claim retryability either
    way, and a guessed ``True``/``False`` would overstate what agent_crew's
    dispatcher actually encodes for it.
    """

    if not reason:
        return None
    lowered = reason.lower()
    if "max_retries" in lowered:
        return False
    if any(tag in lowered for tag in _NONRETRYABLE_REASON_TAGS):
        return False
    if any(tag in lowered for tag in _RETRYABLE_REASON_TAGS):
        return True
    return None


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
class TaskTokenTelemetry:
    """One task's directly-observed token/cache components (quota-core#78).

    Consumer-side mirror of Agent Crew #317/#318's ``task_attribution`` token
    columns, which the producer fills on real task completion by reading the
    provider's own session transcript. As everywhere else in this module,
    quota-core does not import the producer: this depends only on the wire
    shape of the attribution row.

    ⛔Every field is ``None`` when the producer did not observe it, and the
      producer is explicit about this: its own contract says "``None`` means
      the provider did not supply the fact; callers must never derive or
      estimate it", and its writer updates only the columns it actually
      measured rather than overwriting the rest with nulls. A measured ``0``
      (a task that genuinely read nothing from cache) must therefore stay
      distinguishable from ``None`` (a provider that does not report the
      component at all, or a row written before #317 shipped).

    ⛔There is deliberately NO total. These are five different economic
      quantities -- input the provider had to process fresh, input it wrote
      into cache, input it read back from cache, output it produced, and the
      reasoning subset of that output -- and a provider that exposes only some
      of them would otherwise get a "total" that silently means something
      different from another provider's. ``reasoning_tokens`` in particular is
      a SUBSET of ``output_tokens`` on the providers that report both, so
      adding them would double-count. Compose whatever total a specific
      pricing model needs at the point of use, where the provider is known.

    ⛔``context_window_tokens`` is a window MEASUREMENT, not a billing
      component, and it measures the same physical quantity as quota-core#70's
      lifecycle ``context_tokens`` observation. The two are kept in separate
      fields and reconciled explicitly rather than merged -- see
      :func:`~quota_core.context_economics.analytics.reconcile_context_window`.

    PRODUCTION SAMPLE PENDING: the wire shape is confirmed against real
    post-deploy attribution rows, but no row with a MEASURED value has been
    captured yet, so nothing built on this type may be presented as measured
    cache-locality or resume-vs-fresh economics. See
    ``docs/task-token-telemetry.md``.
    """

    uncached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_read_tokens: int | None = None
    output_tokens: int | None = None
    #: The reasoning subset of ``output_tokens`` where a provider separates it.
    #: Never added to ``output_tokens`` -- see the class note above.
    reasoning_tokens: int | None = None
    #: The provider's context window at task completion. Reconciled against the
    #: quota-core#70 lifecycle observation, never summed with it.
    context_window_tokens: int | None = None

    @property
    def observed_components(self) -> tuple[str, ...]:
        """Names of the components this task actually reported.

        An empty tuple means the row carried no measurement at all, which is a
        different fact from a row that measured zeroes.
        """

        return tuple(
            name for name in (
                "uncached_input_tokens", "cache_write_tokens", "cache_read_tokens",
                "output_tokens", "reasoning_tokens", "context_window_tokens",
            )
            if getattr(self, name) is not None
        )

    @property
    def has_any_observation(self) -> bool:
        return bool(self.observed_components)


#: The exact ``task_attribution`` column names Agent Crew #317/#318 writes.
#: Kept as one tuple so the parser, the serializer and the known-key set of
#: :func:`attribution_from_dict` cannot drift apart from each other.
TASK_TOKEN_TELEMETRY_FIELDS: tuple[str, ...] = (
    "uncached_input_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "output_tokens",
    "reasoning_tokens",
    "context_window_tokens",
)

#: Attribution DIMENSIONS from the same contract. Carried so a caller can group
#: or compare by them; never interpreted here. A hash is an identity, not a
#: claim about what it identifies -- two rows sharing a `stable_prefix_hash`
#: were assembled from the same stable prefix, which is not by itself evidence
#: that the provider cached it.
TASK_ATTRIBUTION_HASH_FIELDS: tuple[str, ...] = (
    "stable_prefix_hash",
    "context_pack_hash",
)


def task_token_telemetry_from_dict(data: dict[str, Any]) -> TaskTokenTelemetry:
    """Parse the quota-core#78 token components out of one attribution row.

    Tolerant in the same way as every other parser here: an absent key and an
    explicit ``null`` both mean unknown, a non-numeric value is left unknown
    rather than coerced, and ``bool`` is rejected outright because it is an
    ``int`` subclass in Python -- a stray ``true`` would otherwise fabricate a
    one-token measurement.
    """

    def _opt_int(key: str) -> int | None:
        value = data.get(key)
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    return TaskTokenTelemetry(**{name: _opt_int(name) for name in TASK_TOKEN_TELEMETRY_FIELDS})


def task_token_telemetry_to_dict(telemetry: TaskTokenTelemetry) -> dict[str, Any]:
    """Flatten back to the producer's own column names.

    Every key is always present, including when its value is ``None``: omitting
    a null would leave a reader unable to tell "this producer measured nothing"
    from "this consumer version does not know the field".
    """

    return {name: getattr(telemetry, name) for name in TASK_TOKEN_TELEMETRY_FIELDS}


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
    # Agent Crew #278/#279 tester-treatment/lock-wait contract (quota-core
    # #68): NULL in the producer's `task_attribution` table means "no scope
    # was ever resolved for this row" (not a test task, or a pre-#279
    # historical row) -- kept as `None` here, never defaulted to a fake
    # "targeted"/"unknown" string or a zero wait. `lock_wait_seconds=0.0` is
    # a real measured value (dispatched with no contention) and must stay
    # distinguishable from `None` (never measured).
    effective_test_scope: str | None = None
    test_scope_source: str | None = None
    test_scope_hash: str | None = None
    lock_wait_seconds: float | None = None
    lock_defer_count: int | None = None
    # Agent Crew #317/#318 (quota-core#78) -- the task's own observed token and
    # cache components, and the two hashes that identify what was assembled for
    # it. Nested rather than flattened so the "these are never summed" boundary
    # is visible in the type: see `TaskTokenTelemetry`.
    task_telemetry: TaskTokenTelemetry = field(default_factory=TaskTokenTelemetry)
    #: Identity of the stable prompt prefix the dispatch was built on, and of
    #: the Context Pack assembled for it. Dimensions only -- see
    #: `TASK_ATTRIBUTION_HASH_FIELDS`.
    stable_prefix_hash: str | None = None
    context_pack_hash: str | None = None
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
class ContextPackAttribution:
    """One task dispatch's Context Pack telemetry (quota-core#62).

    Consumer-side mirror of Agent Crew #239's real producer contract
    (``ContextPack.telemetry()`` in ``agent_crew/context_pack.py``, emitted
    as a ``"context_pack_built"`` :class:`ContextLifecycleEvent`). quota-core
    does not import agent_crew -- this dataclass and its parser
    (:func:`context_pack_attribution_from_event`) only depend on the JSONL
    wire shape, which may gain fields over time; anything this repo's
    installed version doesn't recognize is simply not extracted, never
    guessed. Every field the producer might not populate stays ``None``
    (or an empty ``dict``/``list`` for a collection) rather than a
    misleading zero -- "no pack" and "pack with zero of something" must
    stay distinguishable to a caller.
    """

    task_id: str | None = None
    project: str | None = None
    context_id: str | None = None
    context_generation: int | None = None
    role: str | None = None
    agent: str | None = None
    mode: str | None = None
    context_pack_id: str | None = None
    context_pack_hash: str | None = None
    context_pack_schema_version: int | None = None
    candidate_count: int | None = None
    selected_count: int | None = None
    total_tokens: int | None = None
    tokens_by_category: dict[str, int] = field(default_factory=dict)
    stale_count: int | None = None
    conflict_count: int | None = None
    latency_ms: float | None = None
    degraded: bool | None = None
    degraded_reason: str | None = None
    budget: dict[str, Any] = field(default_factory=dict)
    timestamp: int | None = None


@dataclass(frozen=True)
class ProviderContextObservation:
    """One dispatch's provider context-window measurement (quota-core#70).

    Consumer-side mirror of the producer's ``provider_context_observed`` /
    ``provider_context_capped`` lifecycle rows (Agent Crew #288). As everywhere
    else in this module, quota-core does not import the producer -- this depends
    only on the JSONL wire shape, and anything the installed version does not
    recognise is left unextracted rather than guessed.

    ⛔``context_tokens=None`` means UNKNOWN: no store, unreadable, or a provider
      for which the window is not measured at all. ``0`` means a measured empty
      window. Collapsing either into the other would make every cohort built on
      this field wrong in a way no consumer could detect afterwards, which is
      the single invariant this type exists to carry.

    ⛔``capped`` distinguishes the two producer events rather than hiding them
      behind one name. A capped dispatch had a fresh context FORCED; an observed
      one did not. They are the two arms of one branch in the producer, so a
      caller that sums them as two samples double-counts a single dispatch --
      see ``provider_context_observations_from_events``, which collapses a
      contaminated pair before any caller can.

    The provider's context WINDOW is not the Context Pack's token budget. They
    are different quantities measured at different layers and stay in separate
    fields (``ContextPackAttribution`` carries the latter).
    """

    task_id: str | None = None
    project: str | None = None
    context_id: str | None = None
    context_generation: int | None = None
    provider: str | None = None
    provider_session_id: str | None = None
    agent: str | None = None
    role: str | None = None
    task_type: str | None = None
    context_tokens: int | None = None
    context_bytes: int | None = None
    cap_mb: float | None = None
    cap_tokens: int | None = None
    capped: bool = False
    tripped_by: str | None = None
    timestamp: int | None = None
    #: Every event type seen for this dispatch when more than one arrived.
    #: Empty on clean data; populated only by the duplicate defence below, so a
    #: caller can tell a collapsed pair from a single clean row.
    duplicate_event_types: tuple[str, ...] = ()


#: The producer's honest outcomes for a context-clearing intervention, mapped
#: to the three states this module keeps apart. `send_failed` is a fourth: the
#: keystrokes never landed, so the intervention cannot explain anything.
#:
#: ⛔Anything not listed maps to `None` — unknown, never folded into
#:   `attempted`. A future producer value guessed at here would be indis-
#:   tinguishable from a measured one in every cohort downstream.
_CLEARING_STATUS_BY_OUTCOME: dict[str, str] = {
    "attempted": "attempted",
    "send_failed": "failed",
    "confirmed": "confirmed",
}

ContextClearStatus = Literal["attempted", "failed", "confirmed"]


@dataclass(frozen=True)
class ProviderContextClearing:
    """One dispatch's context-clearing intervention (quota-core#72).

    Consumer-side mirror of the producer's ``provider_context_cleared`` row.
    As everywhere else in this module, quota-core does not import the producer:
    this depends only on the JSONL wire shape.

    ⛔``status="attempted"`` means the producer sent the clear keystrokes and
      the send returned success. That is evidence the keys were DELIVERED, not
      evidence the provider acted on them. The producer is careful to say so,
      and the distinction must survive the whole way here — because the same
      attempted send also sets the reset flag, so the next dispatch is
      attributed ``context_policy="fresh"`` whether or not anything was
      actually cleared.

      Counting such a dispatch as a plain fresh context makes a resume-vs-fresh
      comparison an average over an unknown mixture, with nothing in the data
      to reveal it. See :func:`~quota_core.context_economics.analytics.
      context_policy_cohort`.

    ⛔``status=None`` is unknown — no row, or an outcome this version does not
      recognise. ``raw_outcome`` keeps whatever the producer actually wrote so
      a later reader can classify it without a reparse.

    PRODUCTION SAMPLE PENDING: no organic post-deployment clearing data has been
    captured yet, so nothing built on this type may be presented as measured
    auto-clear economics. See ``docs/provider_context_cleared.md``.
    """

    task_id: str | None = None
    project: str | None = None
    context_id: str | None = None
    context_generation: int | None = None
    provider: str | None = None
    provider_session_id: str | None = None
    agent: str | None = None
    role: str | None = None
    pane_id: str | None = None
    #: The window measurement that tripped the threshold, when the producer
    #: had one. `None` is unknown; `0` would be a measured empty window.
    context_tokens: int | None = None
    token_source: str | None = None
    cap_tokens: int | None = None
    reason: str | None = None
    status: ContextClearStatus | None = None
    raw_outcome: str | None = None
    timestamp: int | None = None
    #: Populated only when more than one row arrived for one dispatch, so a
    #: caller can tell a collapsed pair from a single clean row.
    duplicate_event_types: tuple[str, ...] = ()


def provider_context_clearing_from_event(
    event: ContextLifecycleEvent,
) -> ProviderContextClearing | None:
    """Extract a :class:`ProviderContextClearing` from one lifecycle event.

    Returns ``None`` for any other event type, so a caller filtering a mixed
    stream can apply it unconditionally — the same pattern as
    :func:`provider_context_observation_from_event`.
    """

    if event.event_type != "provider_context_cleared":
        return None
    extra = event.extra or {}

    def _opt_int(*keys: str) -> int | None:
        for key in keys:
            value = extra.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            return int(value)
        return None

    def _opt_str(*keys: str) -> str | None:
        for key in keys:
            value = extra.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    raw_outcome = _opt_str("outcome")
    return ProviderContextClearing(
        task_id=event.task_id,
        project=event.project,
        context_id=event.context_id,
        context_generation=_opt_int("context_generation"),
        provider=event.provider or _opt_str("provider", "agent"),
        provider_session_id=(event.provider_session_id
                             or _opt_str("provider_session_id", "conversation_id")),
        agent=_opt_str("agent"),
        role=_opt_str("role"),
        pane_id=_opt_str("pane_id"),
        context_tokens=_opt_int("context_tokens"),
        token_source=_opt_str("token_source"),
        cap_tokens=_opt_int("cap_tokens"),
        reason=_opt_str("reason"),
        status=_CLEARING_STATUS_BY_OUTCOME.get(raw_outcome or ""),  # type: ignore[arg-type]
        raw_outcome=raw_outcome,
        timestamp=event.timestamp,
    )


def provider_context_clearings_from_events(
    events: Iterable[ContextLifecycleEvent],
) -> list[ProviderContextClearing]:
    """Parse a mixed event stream into one clearing per dispatch.

    ⛔Two rows for one ``task_id`` are one intervention reported twice, not two
      interventions — the same duplicate defence the observation stream needed.
      The first is kept and the collision is recorded in
      ``duplicate_event_types`` rather than silently discarded, so a caller can
      see that the producer contradicted itself.
    """

    by_task: dict[str, ProviderContextClearing] = {}
    seen: dict[str, list[str]] = {}
    loose: list[ProviderContextClearing] = []
    for clearing in (provider_context_clearing_from_event(e) for e in events):
        if clearing is None:
            continue
        if not clearing.task_id:
            loose.append(clearing)
            continue
        if clearing.task_id in by_task:
            seen.setdefault(clearing.task_id, [by_task[clearing.task_id].raw_outcome or "?"])
            seen[clearing.task_id].append(clearing.raw_outcome or "?")
            continue
        by_task[clearing.task_id] = clearing
    return [
        replace(c, duplicate_event_types=tuple(seen[task_id]))
        if task_id in seen else c
        for task_id, c in by_task.items()
    ] + loose


_PROVIDER_CONTEXT_EVENT_TYPES = ("provider_context_observed", "provider_context_capped")


def provider_context_observation_from_event(
    event: ContextLifecycleEvent,
) -> ProviderContextObservation | None:
    """Extract a ``ProviderContextObservation`` from one lifecycle event.

    Returns ``None`` for any other event type, so a caller filtering a mixed
    stream can apply it unconditionally -- the same pattern as
    ``context_pack_attribution_from_event``.

    The two producer events spell two fields differently: the capped row names
    the session ``conversation_id`` and the store ``bytes``; the observed row
    names them ``provider_session_id`` and ``context_bytes``. Both spellings are
    read; neither value is invented when absent.
    """

    if event.event_type not in _PROVIDER_CONTEXT_EVENT_TYPES:
        return None
    extra = event.extra or {}

    def _opt_int(*keys: str) -> int | None:
        for key in keys:
            value = extra.get(key)
            # ⛔ before : in Python  is 1, and a boolean in a
            #   token field is malformed producer data, not a one-token window.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            return int(value)
        return None

    def _opt_float(key: str) -> float | None:
        value = extra.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    def _opt_str(*keys: str) -> str | None:
        for key in keys:
            value = extra.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    return ProviderContextObservation(
        task_id=event.task_id,
        project=event.project,
        context_id=event.context_id,
        context_generation=_opt_int("context_generation"),
        provider=event.provider or _opt_str("provider", "agent"),
        provider_session_id=(event.provider_session_id
                             or _opt_str("provider_session_id", "conversation_id")),
        agent=_opt_str("agent"),
        role=_opt_str("role"),
        task_type=_opt_str("task_type"),
        context_tokens=_opt_int("context_tokens"),
        context_bytes=_opt_int("context_bytes", "bytes"),
        cap_mb=_opt_float("cap_mb"),
        cap_tokens=_opt_int("cap_tokens"),
        capped=event.event_type == "provider_context_capped",
        tripped_by=_opt_str("tripped_by"),
        timestamp=event.timestamp,
    )


def provider_context_observations_from_events(
    events: Iterable[ContextLifecycleEvent],
) -> list[ProviderContextObservation]:
    """Every provider-context observation in ``events``, **one per dispatch**.

    ⛔The duplicate defence (quota-core#70 requirement 6). The producer emits
      ``observed`` and ``capped`` as the two arms of one branch, so a dispatch
      carrying both is contaminated or pre-contract data -- never two
      independent samples. Summing them would double-count one dispatch's
      window in every average built on this list.

      The capped row wins, because it is the authoritative record of what
      happened to that dispatch: a fresh context was forced. Keeping the
      observation instead would describe a reset dispatch as ordinary. The
      collision is reported in ``duplicate_event_types`` rather than silently
      resolved -- a consumer that sees contaminated data should be able to say
      so, and a silent merge is indistinguishable from clean input.

    Rows with no ``task_id`` cannot be attributed to a dispatch at all and are
    therefore never collapsed against each other; each is returned as-is.
    """

    observations = [
        obs for obs in (provider_context_observation_from_event(e) for e in events)
        if obs is not None
    ]
    by_task: dict[str, list[ProviderContextObservation]] = {}
    unattributable: list[ProviderContextObservation] = []
    order: list[str] = []
    for obs in observations:
        if not obs.task_id:
            unattributable.append(obs)
            continue
        if obs.task_id not in by_task:
            by_task[obs.task_id] = []
            order.append(obs.task_id)
        by_task[obs.task_id].append(obs)

    collapsed: list[ProviderContextObservation] = []
    for task_id in order:
        group = by_task[task_id]
        if len(group) == 1:
            collapsed.append(group[0])
            continue
        seen = tuple(dict.fromkeys(
            "provider_context_capped" if o.capped else "provider_context_observed"
            for o in group))
        winner = next((o for o in group if o.capped), group[-1])
        collapsed.append(replace(winner, duplicate_event_types=seen))
    return collapsed + unattributable


def context_pack_attribution_from_event(event: ContextLifecycleEvent) -> ContextPackAttribution | None:
    """Extract a :class:`ContextPackAttribution` from one lifecycle event.

    Returns ``None`` for any event that isn't ``"context_pack_built"`` --
    callers filtering a mixed lifecycle-event stream can call this
    unconditionally and discard the ``None`` results, the same pattern
    :func:`lifecycle_event_from_dict` itself uses for unrecognized types.

    The real producer telemetry rides in ``event.extra`` (every top-level
    key ``lifecycle_event_from_dict`` doesn't recognize as one of its own
    fixed columns lands there, by that function's own tolerant-parsing
    design) -- this function does not re-parse raw JSON itself, so it
    stays correct regardless of how the caller obtained the event.
    """

    if event.event_type != "context_pack_built":
        return None

    extra = event.extra

    def _opt_int(key: str) -> int | None:
        value = extra.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _opt_float(key: str) -> float | None:
        value = extra.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _opt_str(key: str) -> str | None:
        value = extra.get(key)
        return value if isinstance(value, str) else None

    def _opt_bool(key: str) -> bool | None:
        value = extra.get(key)
        return value if isinstance(value, bool) else None

    def _opt_dict(key: str) -> dict:
        value = extra.get(key)
        return dict(value) if isinstance(value, dict) else {}

    return ContextPackAttribution(
        task_id=event.task_id,
        project=event.project,
        context_id=event.context_id,
        context_generation=_opt_int("context_generation"),
        role=_opt_str("role"),
        agent=_opt_str("agent"),
        mode=_opt_str("mode"),
        context_pack_id=_opt_str("context_pack_id"),
        context_pack_hash=_opt_str("context_pack_hash"),
        context_pack_schema_version=_opt_int("context_pack_schema_version"),
        candidate_count=_opt_int("candidate_count"),
        selected_count=_opt_int("selected_count"),
        total_tokens=_opt_int("total_tokens"),
        tokens_by_category=_opt_dict("tokens_by_category"),
        stale_count=_opt_int("stale_count"),
        conflict_count=_opt_int("conflict_count"),
        latency_ms=_opt_float("latency_ms"),
        degraded=_opt_bool("degraded"),
        degraded_reason=_opt_str("degraded_reason"),
        budget=_opt_dict("budget"),
        timestamp=event.timestamp,
    )


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
    # Agent Crew #278/#279 (quota-core #68) -- see RuntimeAttribution for the
    # None-means-unknown / 0.0-is-measured semantics this carries forward.
    effective_test_scope: str | None = None
    test_scope_source: str | None = None
    test_scope_hash: str | None = None
    lock_wait_seconds: float | None = None
    lock_defer_count: int | None = None
    # quota-core#78 -- carried straight through from the attribution row. Kept
    # SEPARATE from `tokens`: that field is what provider usage telemetry says
    # the dispatch billed across its API calls, this one is what the provider's
    # own session transcript says the task observed. They are two measurements
    # of overlapping-but-not-identical things, so merging them would double
    # count; a caller comparing them is doing reconciliation, not addition.
    task_telemetry: TaskTokenTelemetry = field(default_factory=TaskTokenTelemetry)
    stable_prefix_hash: str | None = None
    context_pack_hash: str | None = None
    # quota-core#70 -- the PROVIDER's context window for this dispatch, joined
    # from its lifecycle observation. Distinct from `tokens`, which is what the
    # dispatch itself billed, and from the Context Pack budget, which is what
    # was assembled for it. `None` is unknown; `0` is a measured empty window.
    # `context_window_capped=None` means no observation was joined at all --
    # `False` means one was, and it was not capped.
    context_tokens: int | None = None
    context_bytes: int | None = None
    context_window_capped: bool | None = None
    # quota-core#72 -- whether a context-clearing intervention was observed for
    # this dispatch, and what the producer could honestly say about it.
    # `None` means no clearing row was joined: unknown, NOT "no intervention
    # happened". `"attempted"` means the clear was sent and the provider's
    # completion is unconfirmed, which is why such a dispatch must not be
    # counted as a plain fresh context -- see `context_policy_cohort`.
    # `raw` keeps the producer's own string for a value this version does not
    # recognise.
    context_clear_status: ContextClearStatus | None = None
    context_clear_outcome: str | None = None
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
        "effective_test_scope": attribution.effective_test_scope,
        "test_scope_source": attribution.test_scope_source,
        "test_scope_hash": attribution.test_scope_hash,
        "lock_wait_seconds": attribution.lock_wait_seconds,
        "lock_defer_count": attribution.lock_defer_count,
        # quota-core#78: written FLAT, under the producer's own column names,
        # so a round trip through this function reproduces a row the producer
        # itself could have written -- and so a null stays visible rather than
        # vanishing into an absent key.
        **task_token_telemetry_to_dict(attribution.task_telemetry),
        "stable_prefix_hash": attribution.stable_prefix_hash,
        "context_pack_hash": attribution.context_pack_hash,
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
    - ``raw_outcome``/``failure_reason``/``failure_category``/``retryable``
      (quota-core issue #60) are derived from ``outcome`` when the
      corresponding key is *absent* from ``data``: ``raw_outcome`` falls
      back to the raw ``outcome`` string, ``failure_reason`` from the
      colon-delimited suffix of a real ``"failed:<reason>"`` outcome,
      ``failure_category`` via :func:`classify_failure_category`, and
      ``retryable`` via :func:`infer_retryable`. A future producer that
      supplies one of these keys explicitly -- **including an explicit
      ``null``** -- wins over the derived value; ``key in data`` (not
      ``data.get(key) is None``) is what distinguishes "absent, please
      derive" from "explicitly null, leave it null" (round-1 review of
      issue #60: a naive ``.get()`` check could not tell these apart, so an
      explicit null could never survive a to_dict/from_dict round-trip, and
      setting one field could silently fabricate an unrelated sibling
      field). An explicit-but-invalid value (wrong type, or a
      ``failure_category`` not in the known set) is treated the same as
      absent and still gets derived, rather than kept as garbage.
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
        "failure_reason", "failure_category", "retryable", "terminal_source",
        "effective_test_scope", "test_scope_source", "test_scope_hash",
        *TASK_TOKEN_TELEMETRY_FIELDS, *TASK_ATTRIBUTION_HASH_FIELDS,
        "lock_wait_seconds", "lock_defer_count", "extra",
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
        # bool is a subclass of int in Python -- excluded explicitly so a
        # stray `true`/`false` (never sent by the real producer, which
        # always writes int()/float()-cast values) doesn't silently
        # fabricate a measurement like `lock_defer_count=1`, matching the
        # sibling parser in context_pack_attribution_from_event.
        if value is None or isinstance(value, bool):
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

    def _opt_float(key: str) -> float | None:
        value = data.get(key)
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    agent = _opt_str("agent")
    provider = _opt_str("provider")
    if provider is None and agent is not None and agent.lower() in _KNOWN_AGENT_PROVIDERS:
        provider = agent.lower()

    # Real Agent Crew producer dicts only ever set "outcome" (the raw string,
    # e.g. "failed:dispatcher_timeout"), never "raw_outcome" at all.
    # attribution_to_dict's own output always sets *both* keys: "outcome" is
    # already the *normalized* value (one of "success"/"failed"/"unknown"/
    # None) and "raw_outcome" is the original raw string, which may
    # legitimately be None (e.g. for a directly-constructed record that
    # never had a raw source, or an in-progress task). Those are two
    # different shapes of "no value" that must not be conflated: an absent
    # "raw_outcome" key means "derive it from outcome, per the real
    # contract"; a present "raw_outcome" key whose value is None means "this
    # really has no raw outcome, don't fabricate one from outcome" (round-1
    # review: the old `_opt_str(...) is None` check could not tell these
    # apart, so `RuntimeAttribution(outcome="failed", raw_outcome=None)`
    # incorrectly round-tripped to `raw_outcome="failed"`).
    if "raw_outcome" in data:
        raw_outcome = _opt_str("raw_outcome")
    else:
        raw_outcome = _opt_str("outcome")

    # Round-1's fix above (correctly) stopped deriving `raw_outcome` from
    # `outcome` unconditionally, but it went further and also stopped
    # respecting an already-normalized "outcome" key entirely -- `outcome`
    # was *always* re-derived from `raw_outcome` via `normalize_outcome`,
    # even when the dict already carried a valid normalized value (exactly
    # the shape `attribution_to_dict` itself emits). That silently dropped
    # `outcome` on every round-trip where `raw_outcome` was `None`, e.g.
    # `RuntimeAttribution(outcome="failed")` -> to_dict -> from_dict came
    # back as `outcome=None` (round-2 review regression, quota-core issue
    # #60). Fix: "outcome" and "raw_outcome" are independent keys, each
    # resolved from its own presence in `data` -- when "outcome" is present
    # and is already a valid `NormalizedOutcome` (or explicit None), trust
    # it as-is; otherwise (the real Agent Crew contract shape, e.g.
    # "failed:dispatcher_timeout" or "") normalize it the same way as
    # before. "outcome" absent entirely still falls back to deriving from
    # `raw_outcome`, unchanged from round-1.
    if "outcome" in data:
        _raw_outcome_field = data.get("outcome")
        if _raw_outcome_field is None or _raw_outcome_field in ("success", "failed", "unknown"):
            outcome = _raw_outcome_field  # type: ignore[assignment]
        else:
            outcome = normalize_outcome(str(_raw_outcome_field))
    else:
        outcome = normalize_outcome(raw_outcome)

    if "failure_reason" in data:
        failure_reason = _opt_str("failure_reason")
    else:
        failure_reason = extract_failure_reason(raw_outcome)

    if "failure_category" in data:
        _raw_failure_category = data.get("failure_category")
        if _raw_failure_category is None:
            failure_category = None
        elif _raw_failure_category in _FAILURE_CATEGORIES:
            failure_category = _raw_failure_category
        else:
            # Present but not a recognized category -- garbage, not a
            # deliberate null; derive instead of keeping it.
            failure_category = classify_failure_category(outcome, raw_outcome)
    else:
        failure_category = classify_failure_category(outcome, raw_outcome)

    if "retryable" in data:
        _raw_retryable = data.get("retryable")
        if _raw_retryable is None:
            retryable = None
        elif isinstance(_raw_retryable, bool):
            retryable = _raw_retryable
        else:
            retryable = infer_retryable(failure_reason)
    else:
        retryable = infer_retryable(failure_reason)

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
        effective_test_scope=_opt_str("effective_test_scope"),
        test_scope_source=_opt_str("test_scope_source"),
        test_scope_hash=_opt_str("test_scope_hash"),
        lock_wait_seconds=_opt_float("lock_wait_seconds"),
        lock_defer_count=_opt_int("lock_defer_count"),
        task_telemetry=task_token_telemetry_from_dict(data),
        stable_prefix_hash=_opt_str("stable_prefix_hash"),
        context_pack_hash=_opt_str("context_pack_hash"),
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
        "effective_test_scope": record.effective_test_scope,
        "test_scope_source": record.test_scope_source,
        "test_scope_hash": record.test_scope_hash,
        "lock_wait_seconds": record.lock_wait_seconds,
        "lock_defer_count": record.lock_defer_count,
        # quota-core#78. NESTED under its own key, exactly like `tokens` above:
        # this dict is quota-core's own record schema, not a mirror of the
        # producer's row, and keeping the components grouped is what stops a
        # reader treating them as interchangeable with `tokens` (what the
        # dispatch billed) or with `context_tokens` (#70's window observation).
        # `attribution_to_dict` flattens the same fields instead, because that
        # one IS a producer-row mirror and must round-trip as such.
        #
        # Every component key is present even when null, for the same reason as
        # the #70 block below: PR #71 joined fields in memory and dropped them
        # here, so in-process tests passed while the written artifact was wrong.
        "task_telemetry": task_token_telemetry_to_dict(record.task_telemetry),
        "stable_prefix_hash": record.stable_prefix_hash,
        "context_pack_hash": record.context_pack_hash,
        # quota-core#70. Written WITHOUT coercion, and always present even when
        # null: `0` is a measured empty window, `null` is unknown, and
        # `context_window_capped=False` ("an observation was joined and it was
        # not capped") is a different fact from `None` ("none was joined").
        # Omitting a null key would leave a reader unable to tell "measured
        # nothing" from "this producer version did not report it" -- and
        # omitting the keys entirely, as the first version of this function did,
        # dropped every joined observation at the persistence boundary.
        "context_tokens": record.context_tokens,
        "context_bytes": record.context_bytes,
        "context_window_capped": record.context_window_capped,
        # quota-core#72: carried across the persistence boundary deliberately.
        # PR #71 joined the observation fields in memory and dropped them here,
        # so in-process tests passed and only the written artifact was wrong
        # (#70 review). A reader working from the artifact alone has to be able
        # to rebuild the same cohort this process did.
        "context_clear_status": record.context_clear_status,
        "context_clear_outcome": record.context_clear_outcome,
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
    # Agent Crew #278/#279 (quota-core #68): lock_wait_seconds=0.0 is a real
    # measured value and must remain a valid, non-error input -- only a
    # non-numeric/non-null value is flagged.
    lock_wait = data.get("lock_wait_seconds")
    if lock_wait is not None and (isinstance(lock_wait, bool) or not isinstance(lock_wait, (int, float))):
        errors.append("lock_wait_seconds must be a number or null")
    lock_defers = data.get("lock_defer_count")
    if lock_defers is not None and (isinstance(lock_defers, bool) or not isinstance(lock_defers, int)):
        errors.append("lock_defer_count must be an integer or null")
    return tuple(errors)


__all__ = [
    "TaskTokenTelemetry",
    "TASK_TOKEN_TELEMETRY_FIELDS",
    "TASK_ATTRIBUTION_HASH_FIELDS",
    "task_token_telemetry_from_dict",
    "task_token_telemetry_to_dict",
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
