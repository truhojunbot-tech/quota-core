"""quota-core#72 — an auto-clear that was *attempted* is not a fresh context.

The producer clears a pane's context by sending keystrokes and records the
result honestly: `outcome="attempted"` when the send returned success,
`"send_failed"` when it did not. A successful *send* is evidence the keys were
delivered, not evidence the provider acted on them — so "attempted" means
"we asked, and we do not know what happened".

That attempted send nonetheless sets the reset flag on the task, so the next
dispatch is attributed `context_policy="fresh"`. quota-core#70's policy
stratification then counts it as a plain fresh context, indistinguishable from
one that genuinely started empty. A resume-vs-fresh economics comparison built
on that cohort is measuring an unknown mixture and cannot say so.

Three states are kept apart here, per the issue:

    (a) no clearing intervention observed        -> status None
    (b) attempted, provider completion unknown   -> status "attempted"
    (c) confirmed by a provider-native signal    -> status "confirmed"

plus the producer's fourth honest outcome, `send_failed`, which means the
intervention did NOT land and therefore cannot be why anything looks fresh.

⛔PRODUCTION SAMPLE PENDING. Every fixture below is shaped from the producer's
  emit site, not captured from a live run: no organic post-deployment clearing
  data exists yet. This file proves the consumer path — parse, join, cohort,
  persist — and deliberately proves nothing about real auto-clear economics.
  See `docs/provider_context_cleared.md`.
"""

from __future__ import annotations


from quota_core.context_economics.schema import (
    ContextLifecycleEvent,
    ProviderContextClearing,
    TaskEconomicsRecord,
    provider_context_clearing_from_event,
    provider_context_clearings_from_events,
    task_economics_to_dict,
)
from quota_core.context_economics.correlate import attach_provider_context_clearings
from quota_core.context_economics.analytics import (
    compare_context_policies,
    context_policy_cohort,
    provider_context_by_policy,
)


def _cleared_event(outcome="attempted", task_id="impl-a1b2c3d4", **over):
    """A `provider_context_cleared` row in the producer's exact emit shape.

    Field-for-field from the producer's own call site: task_id, project, role,
    agent, provider, pane_id, context_id, context_generation,
    provider_session_id, context_tokens, token_source, cap_tokens, reason,
    outcome.
    """
    payload = {
        "task_id": task_id,
        "project": "demo",
        "role": "implementer",
        "agent": "provider-a",
        "provider": "provider-a",
        "pane_id": "%7",
        "context_id": "ctx-9",
        "context_generation": 3,
        "provider_session_id": "sess-77",
        "context_tokens": 412_000,
        "token_source": "transcript",
        "cap_tokens": 200_000,
        "reason": "auto_clear_token_threshold",
        "outcome": outcome,
    }
    payload.update(over)
    return ContextLifecycleEvent(
        event_type="provider_context_cleared",
        runtime="cli",
        task_id=payload["task_id"],
        project=payload["project"],
        context_id=payload["context_id"],
        provider_session_id=payload["provider_session_id"],
        provider=payload["provider"],
        timestamp=1_789_000_000,
        extra=payload,
    )


def _record(task_id="impl-a1b2c3d4", policy="fresh", **over):
    kwargs = dict(
        task_id=task_id, runtime="cli", project="demo", provider="provider-a",
        context_id="ctx-9", context_generation=3, provider_session_id="sess-77",
        context_policy=policy,
    )
    kwargs.update(over)
    return TaskEconomicsRecord(**kwargs)


# ── 1. parsing the producer's row ─────────────────────────────────────


def test_a_cleared_event_is_extracted():
    clearing = provider_context_clearing_from_event(_cleared_event())
    assert isinstance(clearing, ProviderContextClearing)
    assert clearing.task_id == "impl-a1b2c3d4"
    assert clearing.context_id == "ctx-9"
    assert clearing.context_generation == 3
    assert clearing.provider_session_id == "sess-77"
    assert clearing.context_tokens == 412_000
    assert clearing.cap_tokens == 200_000
    assert clearing.reason == "auto_clear_token_threshold"


def test_another_event_type_is_not_one_of_these():
    """Applied unconditionally to a mixed stream, like its siblings."""
    other = ContextLifecycleEvent(event_type="provider_context_observed",
                                  runtime="cli", task_id="impl-a1b2c3d4",
                                  timestamp=1, extra={"task_id": "impl-a1b2c3d4"})
    assert provider_context_clearing_from_event(other) is None


def test_each_producer_outcome_maps_to_its_own_state():
    """★★(b) and (c) must never collapse into each other, and `send_failed`
    must not read as either.

    Written as an explicit loop rather than `pytest.mark.parametrize` so this
    module imports under `python -m unittest discover`, which is what this
    repo's CI actually runs and which installs only the package itself. A
    module-scope `import pytest` made discovery fail on the import, turning the
    whole suite red in CI while passing locally where pytest happens to exist.
    """
    for outcome, status in (
        ("attempted", "attempted"),
        ("send_failed", "failed"),
        ("confirmed", "confirmed"),
    ):
        assert provider_context_clearing_from_event(_cleared_event(outcome)).status == status, outcome


def test_an_outcome_this_version_does_not_know_is_unknown_not_guessed():
    """⛔A future producer value must not be silently folded into `attempted`.
    The raw string is kept so a later reader can classify it; the status stays
    None, which every cohort below treats as "no usable intervention signal"."""
    clearing = provider_context_clearing_from_event(_cleared_event("teleported"))
    assert clearing.status is None
    assert clearing.raw_outcome == "teleported"


def test_a_missing_outcome_is_unknown():
    event = _cleared_event()
    del event.extra["outcome"]
    clearing = provider_context_clearing_from_event(event)
    assert clearing.status is None and clearing.raw_outcome is None


def test_duplicate_rows_for_one_dispatch_collapse():
    """Same defence as the observation stream: two rows for one dispatch are one
    sample, and the collapse is visible rather than silent."""
    events = [_cleared_event("attempted"), _cleared_event("send_failed")]
    clearings = provider_context_clearings_from_events(events)
    assert len(clearings) == 1
    assert clearings[0].duplicate_event_types


# ── 2. the join ───────────────────────────────────────────────────────


def test_a_clearing_joins_by_task_and_context_identity():
    [out] = attach_provider_context_clearings(
        [_record()], [provider_context_clearing_from_event(_cleared_event())])
    assert out.context_clear_status == "attempted"
    assert out.context_clear_outcome == "attempted"


def test_a_record_with_no_clearing_stays_unknown():
    """⛔`None`, never a fabricated "none"/False. A task with no intervention
    row and a task whose row was lost are both unknown here, and the module's
    nullable convention says so with the same value."""
    [out] = attach_provider_context_clearings([_record()], [])
    assert out.context_clear_status is None
    assert out.context_clear_outcome is None


def test_a_contradicting_context_identity_is_refused():
    """Same rule as #70's observation join: a shared task_id with a disagreeing
    identity is two different dispatches, and merging them would put one
    dispatch's intervention onto another's economics."""
    clearing = provider_context_clearing_from_event(
        _cleared_event(context_id="ctx-OTHER"))
    [out] = attach_provider_context_clearings([_record()], [clearing])
    assert out.context_clear_status is None
    assert any("clearing refused" in n for n in out.attribution_notes)


def test_the_join_never_rewrites_the_policy():
    """★★A `send_failed` clearing must not make anything look fresh — and no
    clearing, of any outcome, may edit `context_policy`. The producer decides
    policy; this join only annotates what intervention was observed."""
    for outcome in ("attempted", "send_failed", "confirmed"):
        clearing = provider_context_clearing_from_event(_cleared_event(outcome))
        [out] = attach_provider_context_clearings([_record(policy="resume")], [clearing])
        assert out.context_policy == "resume", outcome


# ── 3. cohorts: the contamination this issue exists to stop ───────────


def test_an_attempted_clear_is_not_pure_fresh():
    """★★The issue. A fresh-looking dispatch whose freshness may be an
    unconfirmed intervention cannot sit in the cohort that means "started
    empty on its own"."""
    record = _record(policy="fresh", context_clear_status="attempted")
    assert context_policy_cohort(record) != "fresh"


def test_a_plain_fresh_dispatch_is_still_pure_fresh():
    """⛔The control. Only rows carrying an unconfirmed intervention move."""
    assert context_policy_cohort(_record(policy="fresh")) == "fresh"


def test_a_confirmed_clear_is_fresh():
    """(c): once a provider-native signal confirms the clear, the context really
    did start empty and the row belongs with the other fresh ones."""
    assert context_policy_cohort(
        _record(policy="fresh", context_clear_status="confirmed")) == "fresh"


def test_a_failed_send_leaves_the_cohort_alone():
    """The keys never landed, so the intervention cannot be why this row looks
    like anything. It is classified on its own policy."""
    assert context_policy_cohort(
        _record(policy="resume", context_clear_status="failed")) == "resume"
    assert context_policy_cohort(
        _record(policy="fresh", context_clear_status="failed")) == "fresh"


def test_the_comparison_reports_the_cohorts_separately():
    """★★End to end: the attempted row must not inflate `fresh`'s sample."""
    comparison = compare_context_policies([
        _record(task_id="t1", policy="fresh"),
        _record(task_id="t2", policy="fresh", context_clear_status="attempted"),
        _record(task_id="t3", policy="resume"),
    ])
    assert comparison["fresh"]["count"] == 1, \
        "an unconfirmed auto-clear was counted as a pure fresh context"
    assert comparison["resume"]["count"] == 1
    contaminated = [k for k in comparison if k not in ("fresh", "resume")]
    assert len(contaminated) == 1, comparison
    assert comparison[contaminated[0]]["count"] == 1


def test_every_record_lands_in_exactly_one_cohort():
    """The module's standing rule for stratification: no row is dropped and no
    row is double-counted, so `count` is always a usable denominator."""
    records = [
        _record(task_id="t1", policy="fresh"),
        _record(task_id="t2", policy="fresh", context_clear_status="attempted"),
        _record(task_id="t3", policy="resume", context_clear_status="failed"),
        _record(task_id="t4", policy="compact", context_clear_status="confirmed"),
        _record(task_id="t5", policy="unknown"),
    ]
    comparison = compare_context_policies(records)
    assert sum(int(e["count"]) for e in comparison.values()) == len(records)


def test_the_window_summary_keeps_the_same_cohort_split():
    """★★provider_context_by_policy (quota-core#70) is a SIBLING stratification
    to compare_context_policies, over the same records but reporting window
    size instead of failure/success. It has its own bucketing loop, so fixing
    one does not fix the other -- this is exactly the gap #72 was filed about:
    the Finding names this function, and #71 already stratified
    compare_context_policies correctly while this one still bucketed on raw
    context_policy, pooling an unconfirmed clear straight into pure fresh.
    """
    records = [
        _record(task_id="t1", policy="fresh", context_tokens=100_000),
        _record(task_id="t2", policy="fresh", context_clear_status="attempted",
                 context_tokens=180_000),
        _record(task_id="t3", policy="resume", context_tokens=50_000),
    ]
    by_policy = provider_context_by_policy(records)
    assert by_policy["fresh"]["total_row_count"] == 1, \
        "an unconfirmed auto-clear's window size was pooled into pure fresh"
    assert by_policy["fresh"]["max_context_tokens"] == 100_000
    contaminated = [k for k in by_policy if k not in ("fresh", "resume")]
    assert len(contaminated) == 1, by_policy
    assert by_policy[contaminated[0]]["max_context_tokens"] == 180_000


# ── 4. persistence — the #70-review bug, not repeated ─────────────────


def test_the_clearing_fields_survive_serialization():
    """★★PR #71 joined the observation fields in memory and dropped them at
    `task_economics_to_dict`, so in-process tests passed and only the written
    artifact was wrong. The same mistake here would silently un-contaminate the
    cohorts in every persisted report."""
    payload = task_economics_to_dict(
        _record(policy="fresh", context_clear_status="attempted",
                context_clear_outcome="attempted"))
    assert payload["context_clear_status"] == "attempted"
    assert payload["context_clear_outcome"] == "attempted"


def test_unknown_survives_serialization_as_unknown():
    """⛔A `None` that serialized as `"none"` would quietly promote every
    un-observed dispatch into a state it was never measured in."""
    payload = task_economics_to_dict(_record())
    assert payload["context_clear_status"] is None
    assert payload["context_clear_outcome"] is None


def test_the_persisted_row_is_enough_to_rebuild_the_cohort():
    """⛔The field surviving is not the point on its own — a downstream reader
    working only from the written artifact has to reach the same cohort this
    process did, or the separation exists in memory and nowhere else."""
    for policy, status, expected in (
        ("fresh", "attempted", False),
        ("fresh", "confirmed", True),
        ("fresh", None, True),
    ):
        record = _record(policy=policy, context_clear_status=status)
        payload = task_economics_to_dict(record)
        rebuilt = TaskEconomicsRecord(
            task_id=payload["task_id"], runtime=payload["runtime"],
            context_policy=payload["context_policy"],
            context_clear_status=payload["context_clear_status"])
        assert (context_policy_cohort(rebuilt) == "fresh") is expected
        assert context_policy_cohort(rebuilt) == context_policy_cohort(record)


def test_a_future_confirmed_row_does_not_break_an_attempted_one():
    """Requirement 4c: adding confirmed semantics later must leave rows already
    written as `attempted` classified exactly as they are today."""
    attempted = _record(task_id="t1", policy="fresh", context_clear_status="attempted")
    confirmed = _record(task_id="t2", policy="fresh", context_clear_status="confirmed")
    assert task_economics_to_dict(attempted)["context_clear_status"] == "attempted"
    assert context_policy_cohort(attempted) != "fresh"
    assert context_policy_cohort(confirmed) == "fresh"


# ── 5. the repo's own boundary ────────────────────────────────────────


def test_this_module_does_not_import_the_producer():
    """⛔quota-core is public and generic: it consumes a JSONL wire shape and
    never depends on whatever wrote it.

    ⛔Checked by parsing imports, not by grepping. This module's docstrings say
      "does not import agent_crew" in prose several times, so a substring match
      reports the very files that document the rule — a test that fails on its
      own subject matter teaches readers to disable it.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "quota_core"
    offenders = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                offenders += [f"{path}: import {a.name}" for a in node.names
                              if a.name.split(".")[0] == "agent_crew"]
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] == "agent_crew":
                    offenders.append(f"{path}: from {node.module}")
    assert offenders == [], offenders
