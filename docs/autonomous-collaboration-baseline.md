# Autonomous Collaboration Baseline -- resource constitution

## Purpose

A fleet of autonomous bots operating on shared infrastructure needs a
cross-cutting governance baseline (autonomy, HOLD/VETO/STOP semantics,
delegation, a shared reporting convention, closed-loop verification) that
every bot layers its own domain policy under. Owning and auditing that
org-wide baseline is out of scope for `quota_core` -- it belongs to whatever
policy-coordination role a given deployment designates for that job (a
"chief of staff" role in this fleet's private operations layer; see that
layer's own documentation for who holds it and where it is tracked).

This document is `quota_core`'s contribution to that baseline: the resource
and budget vocabulary an org-wide baseline typically delegates to a
dedicated resource/budget authority rather than defining itself, since
budget accounting is a domain in its own right.

`quota_core` owns this because it already owns cross-bot usage accounting
(see [reporting-semantics.md](reporting-semantics.md) and
[data-ingestion-architecture.md](data-ingestion-architecture.md)) -- budget
and quota are meaningless without a shared definition of what is being
measured. This document is the SSOT for the terms `budget`, `quota`, and
`scope` as used by anything that depends on `quota_core`. It is **not** the
SSOT for `HOLD`, `VETO`, or `STOP` -- those are cross-cutting governance
signals owned by the org-wide baseline described above; this document only
describes what a bot's budget/quota/scope state looks like when one of those
signals arrives, so that a runtime-enforcement layer (private operations,
not this public repo) has something concrete to enforce against.

## Roles

| Role | Owner | Responsibility |
|---|---|---|
| Org-wide baseline (autonomy, HOLD/VETO/STOP, delegation, reporting) | A deployment's policy-coordination layer (outside this repo) | Defines and audits the cross-cutting governance vocabulary every bot layers its own domain policy under. |
| Resource constitution (this repo) | `quota_core` | Defines budget/quota/scope vocabulary and the generic snapshot/attribution schema those terms are measured against. Public, provider-agnostic, no bot registry. |
| Runtime enforcement / operational guardrail | A private operations overlay | Applies this repo's resource vocabulary, under the org baseline's HOLD/VETO/STOP governance, to one live fleet: watches real usage against real limits and alerts on drift. Private, fleet-specific. |
| Execution orchestration | A multi-agent task runner (e.g. Agent Crew) | Runs the actual multi-agent task graph (implementer/reviewer/tester) that budget and scope apply to. |

Dependency direction follows the existing public/private boundary: private
operations, task runners, and policy-coordination code may depend on
definitions in `quota_core`; `quota_core` must never depend on any of them
(no imports of private-ops or orchestration packages, no bot names, no
fleet-specific thresholds, and no copy of another layer's governance prose
that could drift out of sync with its own SSOT -- link out to it by role,
not by name).

## Terms (quota_core SSOT)

- **Budget** -- a bounded allotment of a countable resource (tokens, provider
  requests, wall-clock minutes, dollars) assigned to a task, a bot, or a time
  window. A budget is a number plus a unit plus a window; it is not itself an
  enforcement mechanism.
- **Quota** -- the ceiling a provider or an internal policy imposes on a
  resource over a window (e.g. a provider's rolling utilization window, a
  per-repo issue-pickup rate). Quota is external or policy-imposed; budget is
  how a bot chooses to spend within a quota.
- **Scope** -- the set of repos, files, processes, or issues an action is
  allowed to touch. A bot staying in scope means it only writes to resources
  it owns or has been explicitly delegated (see
  [public-private-boundary.md](public-private-boundary.md) for the
  quota-core/quota-ops instance of this).

A bot that receives a `HOLD`, `VETO`, or `STOP` (as defined by its
deployment's org-wide baseline) is being told to change its *behavior*;
budget, quota, and scope are the *state* that behavior change acts on.
Concretely: a `HOLD` on a scope means "stop spending budget in that scope";
a `VETO` on an action means "that action's budget request is denied"; a
`STOP` means "treat remaining budget in this scope as zero until lifted."

## Runaway prevention

This elaborates a typical org baseline's "no recursive delegation, no task
explosion, no unbounded review/fix loops" rule with resource-shaped detail:

- **Recursive delegation** -- a bot delegating to another bot that could in
  turn delegate back must not be allowed to cycle. Delegation should be
  expressed as a directed edge in a fixed role graph (this document's Roles
  table plus the deployment's own bot/repo inventory), not as an open-ended
  "ask whoever seems relevant."
- **Task explosion** -- a single triggering event (one issue, one alert)
  must not be allowed to fan out into unbounded child issues/tasks without a
  human-visible checkpoint. Prefer a small number of explicitly-scoped
  follow-up issues over an open-ended tree.
- **Retry/review loops** -- adversarial review or fix-and-retry cycles must
  have a fixed round budget. If a bounded number of rounds does not converge,
  that is itself a result to report (per the org baseline's shared reporting
  convention), not a reason to keep looping silently.

## Persistence

Cross-cutting persistence (surviving context compaction, process restarts)
is the org-wide policy-coordination layer's responsibility, typically via a
versioned marker block in each bot's own persistent instructions. This
document's job is narrower: give a runtime-enforcement layer and any other
consumer a stable, versioned place to link to for what budget/quota/scope
actually mean, so that marker block does not have to carry the full
definition itself. `quota_core` has no bot session of its own to persist
state in -- persistence is discharged by each downstream consumer.
