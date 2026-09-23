# SEV-0 — tokenomics matched evidence

`discovery` + `result`. Evidence only: nothing here enforces, dispatches or writes
to a live database. Every source database was opened through a SQLite `mode=ro`
URI; the only files written are the two artifacts in this directory.

- Gate: Phase B published 2026-09-23T20:46:34Z (quota-core#80 run 5802779806,
  agent_crew#342 run 5802782579), published contract file sha
  `c255965b9e2a53b62ae903d6387332fef6ee21ba426f4a45fc5ba3de4b61cb5f`; owner #51
  comment 5792288011 §10.
- Sources: `~/.agent_crew/agent_crew/tasks.db` — `tasks` (639 rows),
  `task_attribution` (610), `tokenomics_shadow_receipts` (184).
- Machine-readable companion: `tokenomics-matched-evidence.json`.
  Regenerate with `python3.12 evidence/sev0/tokenomics_matched_evidence.py`.
- `UNKNOWN` means unknown. No unknown is reported as `0` anywhere below.

---

## 1. The comparison asked for cannot be made from emitted recommendations

The task asks for *actual vs counterfactual-baseline per recommendation kind* over
the baseline receipts plus every fresh receipt carrying
`shadow_decision_source='quota_core_contract'` and the published contract sha.

**That fresh set is empty, and so is every recommendation field.** Measured over
all 184 `tokenomics_shadow_receipts` rows:

| fact | count |
|---|---|
| receipts total | 184 |
| `shadow_decision_source = 'quota_core_contract'` | **0** |
| receipts carrying the published contract sha `c255965b…` | **0** |
| `shadow_decision_source = 'baseline'` | 164 (all with sha `23e80468…`) |
| `shadow_resolved_at` still null | 20 |
| `shadow_reason = 'task_decision_unavailable'` | 164 |
| receipts with a non-null recommendation (`recommendation_json` or `shadow_recommendation_json`) | **0** |

Every resolved receipt fell back to baseline because the contract artifact the
reader consulted contained no decision for that task id. No recommendation has
ever reached a dispatched task. An emitted-recommendation-vs-actual comparison
therefore has nothing to compare, and reporting one would be invention.

**What was done instead.** The Phase-B contract is a pure, deterministic function
of rows that already exist. `build_contract()` from this branch
(`quota_core/context_economics/contract_emitter.py`, producer commit `cfba4e8`,
policy contract sha `e325a541…`) was re-run read-only over the same organic
`task_attribution` rows, producing **610 decisions**, and those decisions were
joined by `task_id` to the actual task outcomes. This is a *replay*, not a record
of production behaviour, and is labelled as such in the JSON
(`provenance.contract_replay.caveat`). 163 of the 184 receipts join to a replayed
decision; 21 do not.

## 2. The contract admits no action anywhere in the organic population

`quality_preserving` is **False for 610/610** decisions. The gate reasons:

| override reason | decisions |
|---|---|
| `required_context_recall_unknown_or_negative` | 609 |
| `independent_review_correctness_unknown_or_negative` | 569 |
| `risk_assessment_unknown_requires_human_gate` | 529 |
| `task_outcome_not_success` | 129 |
| `no_budget_anchor_for_non_successful_or_unknown_outcome` | 129 |
| `human_gate_required` | 5 |

Consequences, all measured: `recommended_session_treatment` and
`recommended_cache_treatment` read `insufficient_evidence` for **610/610**;
`recommended_provider_tier` is `escalate_allowed` for 579 and `preserve_current`
for 31 — it never lowers a tier; `recommended_max_review_fix_rounds` is 3 for 579
and 1 for 31, matching the tier, and neither progress-state field that would move
it has ever been set (§5).

**The counterfactual baseline is therefore the observed behaviour itself.** Across
610 decisions the number that would have changed what actually ran is **0**. The
quality-floor delta between actual and counterfactual is exactly zero *by
construction* — because nothing was recommended — not because a change was
measured to be harmless. The distinction matters: this is not evidence that any
economy measure is safe.

One structural cause dominates. A single missing producer field —
`required_context_recalled` / `recall_not_applicable`, set on 1 of 610 rows — gates
609 of 610 decisions off on its own.

## 3. Quality floor, whole organic population (n = 610)

| metric | value | known / n |
|---|---|---|
| terminal success (`status = completed`) | 78.69 % (480) | 610 / 610 |
| review tasks carrying an independent verdict | 70.16 % (174) | 248 review tasks |
| of those verdicts, `approve` | 37.93 % (66) | 174 |
| **test tasks carrying an independent verdict** | **0.00 % (0)** | 97 test tasks |
| stale / missing-artifact incidence | 0.82 % (5) | 610 / 610 |
| timeout incidence | 4.75 % (29) | 610 / 610 |
| no-result-submitted incidence | 1.80 % (11) | 610 / 610 |
| provider capacity failure (quota exhausted or 429) | 7.38 % (45) | 610 / 610 |
| retry count | **UNKNOWN** — `retry_of` empty on all 610 rows | 0 / 610 |
| fallback count | 2 | 610 / 610 |
| elapsed seconds | median 102.4, p90 1012.6, sum 213 014.4 | 605 / 610 |
| cache-read tokens | median 9 721 624, p90 88 785 664, sum 5 698 100 997 | 174 / 610 |
| output tokens | median 48 295, sum 15 556 176 | 174 / 610 |
| reasoning tokens | median 56 185, sum 4 180 323 | 74 / 610 |
| provider lineage | codex 261, claude 245, gemini 104 | 610 / 610 |
| session lineage known | 402 | 610 |
| context policy | resume 573, fresh 34, UNKNOWN 3 | 610 |

Two of these are themselves findings. **No tester has ever recorded a verdict** —
0 of 97 test tasks — so the independent test gate contributes no measurable signal
to the quality floor at all. And `retry_of` is a column with no producer, so
retry/no-progress counts are unavailable, not zero.

## 4. Per recommendation kind

Full profiles for all six groupings are in the JSON under
`per_recommendation_kind`. The two that carry information:

**`recommended_max_review_fix_rounds`**

| | rounds = 1 (n = 31) | rounds = 3 (n = 579) |
|---|---|---|
| terminal success | 64.52 % | 79.45 % |
| review verdict present | 100 % (9/9) | 69.04 % (165/239) |
| approve share | 66.67 % | 36.36 % |
| stale/missing artifact | 9.68 % (3) | 0.35 % (2) |
| provider capacity failure | 16.13 % (5) | 6.91 % (40) |
| elapsed median / p90 (s) | 117.3 / 429.5 | 101.1 / 1061.9 |
| cache read median | 12 988 928 (20 known) | 8 886 712 (154 known) |
| provider | claude 9, codex 14, gemini 8 | claude 236, codex 247, gemini 96 |

The rounds = 1 group is the 31 tasks whose risk facts were actually assessed and
came back non-safety. Its lower terminal success is not a recommendation effect —
the recommendation was never applied — it is a property of which tasks got
assessed at all.

**`recommended_soft_budget_present`** splits 481 / 129, but `_envelope()` is gated
on `outcome == "success"`, so that split *is* the success split (99.79 % vs 0.00 %
terminal success). It restates its own grouping variable and is not independent
evidence. It is recorded in the JSON with that caveat attached.

`recommended_session_treatment` and `recommended_cache_treatment` are single-valued
(`insufficient_evidence`, 610/610) and so have no contrast to report.

## 5. Metric classification

A = exists in Qouta · B = measured but not joined · C = producer field missing ·
D = genuinely new. **A/B ⇒ join only. C/D ⇒ listed as a gap and not built here.**

| metric | class | where it lives | coverage |
|---|---|---|---|
| terminal outcome / success | A | `task_attribution.outcome` → `decision.evidence.outcome` | 608/610 |
| token components (uncached in, cache write, cache read, output) | A | `task_attribution.*_tokens` → `evidence.token_observations` | 174/610 |
| reasoning tokens | A | same | 74/610 |
| provider / session / context lineage | A | `agent`, `provider_session_id`, `context_policy`, `context_generation` → `provenance` + `current_behavior` | session 402/610 |
| `fallback_of` | A | `task_attribution.fallback_of` | 2/610 |
| risk facts (safety/architecture/routine/human gate) | A | `task_attribution` risk columns → `_risk()` | 127/610 assessed |
| independent review verdict | B | `tasks.verdict`; quota-core reads only `task_attribution`, so it never reaches `independent_review_correct` | 174/254 reviews |
| elapsed wall time | B | `task_attribution.started_at`/`completed_at`; no contract field | 605/610 |
| stale / missing-artifact incidence | B | `tasks.error_info.reason = 'no_artifact'` | 5/610 |
| provider capacity failure | B | `tasks.error_info.reason ∈ {agy_quota_exhausted, transient_claude_429_max_retries}` | 45/610 |
| redundant-review detection | B | `tasks.context.reviewed_sha`; not read by quota-core | 149/254 reviews |
| review→fix round index | B | `tasks.context.fix_round` | 31 tasks (1:18, 2:7, 3:6) |
| `required_context_recalled` / `recall_not_applicable` | **C** | column exists, no producer writes it; gates 609/610 decisions off | 1/610 |
| independent **test** verdict | **C** | `tasks.verdict` NULL for every test task; the tester role never writes one | 0/97 |
| `retry_of` | **C** | column exists, empty on all rows | 0/610 |
| `context_growth_tokens` | **C** | contract field, no producer | 0/610 |
| `new_evidence_or_progress` / `repeated_unchanged_state` | **C** | the two fields that move the review-fix round envelope have no producer; neither rationale string fired in 610 decisions | 0/610 |
| orchestration waste as **tokens** (stale/misrouted/duplicate) | **C** | emitted null for all 610; nothing attributes tokens to a waste class | 0/610 |
| model identity | **C** | `task_attribution.model` empty on 406/610 | 204/610 |
| per-round token attribution to a PR / review lineage | **D** | no table links spend to a review-fix lineage | n/a |
| currency cost | **D** | quota-core ships no pricing table by design; `component_costs` is all-null without caller-supplied `ProviderPricing` | 0/610 |

Nothing in class C or D was built. They are recorded as gaps.

## 6. Candidate canary

The named candidate is **no-progress / redundant review-fix round suppression** on
low-risk organic workload. It is measurable today from `tasks.context.reviewed_sha`
(present on 149 of the 248 review tasks in the joined population; 99 carry none), which yields 118 distinct (PR, reviewed SHA)
groups, 19 of them reviewed more than once.

Three nested cohorts, all measured:

| cohort | n | produced an `approve` | cache-read tokens | output tokens | wall time |
|---|---|---|---|---|---|
| every review after the first on an identical SHA | 31 | 2 | 318 227 222 (13 known) | 852 204 (13) | 2 704.9 s |
| re-review after a prior **terminal verdict** on that SHA | 16 | 1 | 318 227 222 (12 known) | 852 204 (12) | 1 975.5 s |
| re-review while **`request_changes` already stood** on that SHA | **11** | **0** | 64 223 256 (7 known) | 151 940 (7) | 1 195.4 s |

The broad forms are unsafe, and the data says so. Eight of the 19 repeated groups
have a first attempt that failed or timed out, so a naive "same SHA ⇒ suppress"
rule would suppress a legitimate retry. Worse, four re-reviews flipped the verdict
on an unchanged SHA — `437fdab4`, `4c123fc3`, `addc29ed`, `b5743086`, all
`approve → request_changes`. Suppressing the second review in those four cases
would have left an `approve` standing that an independent second reviewer
contradicted: a measured 4-in-16 quality-floor regression for the
after-terminal-verdict form. **The candidate as stated is not supported.**

The narrowed third cohort is a different matter, and it is the one recommended.

> **Non-regression statement.** The one canary this evidence supports is
> suppressing a review dispatch when a `request_changes` verdict already stands on
> the identical `(PR, reviewed_sha)` pair — the narrowest form of the named
> no-progress candidate. The organic population is 11 tasks over the 2026-09-20 →
> 2026-09-23 window (10 claude, 1 codex), against 248 review tasks and 610 organic
> tasks total. Not one of the 11 produced an `approve`: six re-confirmed
> `request_changes`, three failed, one timed out, one completed with no verdict.
> The suppression therefore cannot lower a gate, because the blocking verdict it
> preserves is strictly the one already standing — an ingress cannot lower the test
> gate, and here it does not touch it. The expected effect is the removal of those
> 11 dispatches: 64 223 256 cache-read tokens and 151 940 output tokens across the
> 7 of 11 with telemetry (the other 4 are **UNKNOWN**, so this is a lower bound,
> not a total), and 1 195.4 s of wall time. The expected quality-floor effect is
> nil on every metric in §3, since no suppressed task contributed a verdict, an
> artifact or a terminal success that the standing review had not already
> contributed. Rollback is a single boolean: the rule is a dispatch-time guard with
> no persistent state, so clearing it restores the current behaviour immediately
> and no suppressed work needs replaying — a suppressed re-review can simply be
> dispatched again. Two limits are stated rather than hidden. Eleven tasks is a
> small population, and the correct monitor during the canary is not a rate but an
> exhaustive one: any suppressed dispatch that would have carried an `approve` is a
> regression and must abort the canary. And this cohort is detectable only on the
> 149 of 248 review tasks that carry a `reviewed_sha`; on the other 99 the rule
> cannot fire and changes nothing.

Explicitly **not** recommended for canary, despite being cheaper to reason about:
the quality-gated recommendations (`session_treatment`, `cache_treatment`) are
`insufficient_evidence` for 610/610 and admit no action at all (§2), and
`recommended_max_review_fix_rounds = 1` would *reduce* the round cap on 31 tasks
whose progress-state evidence is category-C absent — that is a cut with no
measurement behind it.

## 7. What this evidence does not establish

- It does not show that any economy measure preserves quality. It shows the
  contract currently recommends no economy measure (§2).
- The 610 decisions are a deterministic replay, not production behaviour. Zero
  recommendations were delivered to any task (§1).
- Token coverage is 174/610. Every aggregate above is over the known subset and
  says so; none is extrapolated to the full population.
- Retry counts, context growth, waste-token attribution and per-lineage cost are
  UNKNOWN because their producers do not exist (§5). They are not zero.
