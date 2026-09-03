# Autonomous Collaboration Baseline

## Purpose

This document is the resource constitution for a fleet of autonomous bots (each an
independent long-running Claude Code session) that read and write shared
infrastructure -- crontab, GitHub repos, tmux sessions, shared git clones -- without
a human approving every action. It defines the vocabulary and role boundaries the
fleet uses to stay coordinated instead of colliding, and it is the SSOT (single
source of truth) for the terms `budget`, `quota`, `scope`, `HOLD`, `VETO`, and
`STOP`.

`quota_core` owns this document because it already owns cross-bot usage
accounting (see [reporting-semantics.md](reporting-semantics.md) and
[data-ingestion-architecture.md](data-ingestion-architecture.md)) -- budget and
quota are meaningless without a shared definition of what is being measured.
Enforcement of these terms at runtime is a `quota-ops` concern; see that repo's
`docs/autonomous-collaboration-baseline.md` for how the definitions here get
applied to a live fleet.

## Roles

| Role | Owner | Responsibility |
|---|---|---|
| Resource constitution / budget authority | `quota_core` (this repo) | Defines what budget, quota, and scope mean; owns the generic snapshot/attribution schema those terms are measured against. Public, provider-agnostic, no bot registry. |
| Runtime enforcement / operational guardrail | `quota-ops` | Applies the constitution to one live fleet: watches real usage against real limits, executes HOLD/VETO/STOP, alerts on drift. Private, fleet-specific. |
| Execution orchestration | Agent Crew | Runs the actual multi-agent task graph (implementer/reviewer/tester) that budget and scope apply to. |
| Policy coordination / cross-bot routing | Alfred | Chief-of-staff role: rolls out or audits fleet-wide policy (like this baseline), routes work that crosses a single bot's domain, and is the first escalation point for anything a bot cannot resolve or execute itself. |

Dependency direction follows the existing public/private boundary: `quota-ops`,
Agent Crew, and Alfred may depend on definitions in `quota_core`; `quota_core`
must never depend on any of them (no `agent_crew` import, no bot names, no
fleet-specific thresholds).

## Terms

- **Budget** -- a bounded allotment of a countable resource (tokens, provider
  requests, wall-clock minutes, dollars) assigned to a task, a bot, or a time
  window. A budget is a number plus a unit plus a window; it is not itself an
  enforcement mechanism.
- **Quota** -- the ceiling a provider or an internal policy imposes on a
  resource over a window (e.g. Anthropic's 5-hour/7-day utilization, a
  per-repo issue-pickup rate). Quota is external or policy-imposed; budget is
  how a bot chooses to spend within a quota.
- **Scope** -- the set of repos, files, processes, or GitHub issues an action
  is allowed to touch. A bot staying in scope means it only writes to
  resources it owns or has been explicitly delegated (see
  [public-private-boundary.md](public-private-boundary.md) for the
  quota-core/quota-ops instance of this).
- **HOLD** -- a request to pause new actions in a scope without discarding
  in-flight work or state. A bot honoring a HOLD finishes or safely
  checkpoints what is already running, then waits; it does not start new
  units of work in the held scope until the HOLD is lifted.
- **VETO** -- a rejection of one specific proposed or in-flight action. A
  bot honoring a VETO abandons that action (rolling back any partial,
  reversible side effect it already made) but continues other unaffected
  work.
- **STOP** -- an unconditional halt of a bot's autonomous activity in a
  scope, broader than a single action (HOLD) or a single VETO. A bot
  honoring a STOP ceases all autonomous action in that scope immediately,
  including its own standing/scheduled work, and waits for explicit
  human or Alfred instruction before resuming.

HOLD/VETO/STOP are severity-ordered (STOP > VETO > HOLD) but are not
interchangeable substitutes for each other -- a VETO on one action must not be
treated as a HOLD on the whole scope, and a HOLD must not be silently
downgraded to "proceed once nothing new shows up for a while."

## Runaway prevention

Autonomous, self-continuing work (an issue-driven backlog loop, a review loop,
a delegation chain) must have a structural stop condition, not just a
best-effort one:

- **Recursive delegation** -- a bot delegating to another bot that could in
  turn delegate back must not be allowed to cycle. Delegation should be
  expressed as a directed edge in a fixed role graph (this document's Roles
  table), not as an open-ended "ask whoever seems relevant."
- **Task explosion** -- a single triggering event (one issue, one alert)
  must not be allowed to fan out into unbounded child issues/tasks without a
  human- or Alfred-visible checkpoint. Prefer a small number of
  explicitly-scoped follow-up issues over an open-ended tree.
- **Retry/review loops** -- adversarial review or fix-and-retry cycles must
  have a fixed round budget. If a bounded number of rounds does not converge,
  that is itself a result to report (see below), not a reason to keep
  looping silently.

## Reporting convention

Because no human watches most of this activity in real time, autonomous
findings must be legible after the fact from the artifact alone (a GitHub
issue/PR/comment), tagged as one of:

- `discovery` -- something true was found that nobody asked about (a bug, a
  stale config, an outage) and is being surfaced before or alongside fixing
  it.
- `proposal` -- a course of action is suggested but not yet started, usually
  because it crosses scope or needs a judgment call.
- `blocker` -- work cannot proceed without something external (another
  issue landing, a quota resetting, a human decision).
- `result` -- a completed unit of work, closed with verifiable evidence
  (tests, a diff, a measurement, a merged PR) rather than a narrative claim.

## Persistence

This baseline is only useful if it survives a bot's own context compaction or
process restart. Each bot that adopts it must reflect the parts relevant to
its own role in its own persistent instructions (`CLAUDE.md` or equivalent),
not only in this repo's docs -- a doc in a git repo is not on any bot's
context-restoration path by default.
