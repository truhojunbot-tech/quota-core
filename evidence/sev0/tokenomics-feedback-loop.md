# SEV-0 §12 — tokenomics feedback loop (pass 2)

- pinned task: `sev0-tokenomics-feedback-loop-x`
- produced at: 2026-09-23T23:37:46+00:00
- producer commit: `513d7cbdaa736c2c3fa64ba235802d69860485fc`
- policy contract sha256: `e325a5412e32fc22901731bd5a24d92d4ac47afc699c5357dfd6e8f246e14218`
- sources: tasks.db (sqlite mode=ro)
- publishes: none — writes `tokenomics-feedback-loop.json` and `tokenomics-feedback-loop.md` only

## Recorded fields

| field | state | value |
| --- | --- | --- |
| `policy_recommendation` | RECORDED | do not dispatch a review to a commit on which a request_changes verdict already stands for the same (PR or branch, reviewed_sha) |
| `authorization_receipt_id` | RECORDED | `7567a365-fec7-418b-8709-9554519b4416` decision=BLOCK enforced=False |
| `actual_action` | OBSERVED | 1 canary evaluation(s), 0 applied |
| `counterfactual_baseline` | OBSERVED | PENDING |
| `quality_result` | OBSERVED | verdicts={'review-sev0-tokenomics-feedback-loop-x-r1': 'request_changes'} |
| `token_cache_economics` | OBSERVED | lineage review cache_read={'sum': 671744, 'min': 671744, 'max': 671744, 'known': 1, 'n': 1} |
| `provider_capacity_economics` | OBSERVED | reviewer dispatches avoided=0 |
| `retry_round_reduction` | OBSERVED | reviews=1, reduction=0 |
| `avoided_duplicate_rework` | OBSERVED | duplicate reviews avoided=0 |
| `rollback_status` | ARMED_AND_REVERSIBLE | unset AGENT_CREW_TOKENOMICS_CANARY_TASK_ID |

## UNKNOWN fields (explicit)

| field | why | consequence |
| --- | --- | --- |
| `repeated_unchanged_state / new_evidence_or_progress` | no producer writes them; neither string appears in report_producer, quality_evidence or agent_crew_adapter at this commit | a recorded canary outcome cannot mechanically change the round recommendation; the link is reported as no_producer, not as closed |
| `tokenomics_shadow_receipts.canary_*` | the emitter's read path is task_attribution (token/outcome columns) plus tasks(task_id, task_type, context, verdict) via quality evidence; `canary_` appears nowhere in quota_core at this commit | the canary outcome is joinable by task_id for a human reader, but is not an input to the emitted contract |
| `required_context_recalled` | no producer records whether the context a task needed was present; quality_evidence.derive_quality_evidence names this as the single missing producer signal. tasks.verdict, by contrast, IS read (see producer_read_path) | quality_preserving stays False for the lineage even on tasks whose independent_review_correct was derived from a real verdict; the override_reasons say required_context_recall_unknown_or_negative |
| `orchestration_waste.duplicate_tokens` | no producer attributes tokens to a waste class | avoided_duplicate_rework is counted in tasks and in the standing review's own token rows, never as a contract-side waste figure |
| `currency cost` | quota-core ships no pricing table by design | all economics below are token counts, never money |

## What the emitter actually reads

| source | read? | columns | via |
| --- | --- | --- | --- |
| `task_attribution` | True | outcome and the token columns consumed by read_organic_task_records | contract_emitter.build_contract -> report_producer.read_organic_task_records |
| `tasks` | True | task_id, task_type, context, verdict | contract_emitter.build_contract:126-135 -> report_producer._artifact_evidence (report_producer.py:173-188) -> quality_evidence.derive_quality_evidence -> quality_evidence.read_review_verdicts; the verdict of a linked REVIEW row becomes QualityEvidence.independent_review_correct |
| `tokenomics_shadow_receipts` | False | canary_* (canary_applied, canary_reason, canary_counterfactual, ...) | nothing — the substring `canary_` does not occur anywhere in quota_core at this commit, so the canary outcome cannot reach the contract |
| `round_inputs_without_a_producer` | n/a | repeated_unchanged_state / new_evidence_or_progress | declared on QualityEvidence and consumed by the policy, but written by no producer in this repo — this, not the tasks.verdict read path, is why the consumption link is labelled no_producer |

## Decision record for kind `suppress_identical_sha_rereview`

Producer: quota_core.context_economics.contract_emitter.build_contract (read-only; emitter path cfba4e8)

- BEFORE captured at 2026-09-23T23:29:58+00:00; fleet histogram of `recommended_max_review_fix_rounds`: {'1': 31, '3': 595}
- BEFORE `decision_count`: 626
- AFTER: captured

Consumption claim: **SEE_DIFF** — no_producer is a claim about the ROUND INPUTS only: repeated_unchanged_state / new_evidence_or_progress are the sole QualityEvidence fields that move recommended_max_review_fix_rounds and nothing writes them. It is NOT a claim that the emitter reads task_attribution alone — it also reads tasks.verdict through derive_quality_evidence (producer_read_path below). So any change in the diff comes from rows the producer does read — task_attribution outcome/token columns, tasks.verdict via quality evidence, and new lineage tasks entering the contract — and never from the canary columns, which quota_core does not read at all; see unknown_fields[0] and unknown_fields[1].

## Lineage

| task | type | status | verdict | reviewed_sha | error |
| --- | --- | --- | --- | --- | --- |
| `sev0-tokenomics-feedback-loop-x` | implement | failed | — | `—` | no_artifact |
| `review-sev0-tokenomics-feedback-loop-x-r1` | review | completed | request_changes | `886dda3b0` | — |
| `fix-review-sev0-tokenomics-feedback-loop-x-r1-r1` | implement | cancelled | — | `—` | — |

## How to reproduce

```bash
python3.12 scripts/tokenomics_feedback_record.py sev0-tokenomics-feedback-loop-x
```

Re-running after the pinned task's review cascade is terminal reloads the BEFORE snapshot from the committed JSON, captures AFTER, and emits the diff. Every read is `sqlite mode=ro`; nothing is published.

### Pass-2 branch verification

- method: the pass-2 branch was exercised before pass 1 was committed, against a sqlite backup COPY of tasks.db in /tmp seeded with one synthetic suppressed review; the live DB was opened mode=ro only and the copy was deleted afterwards
- result: pass=2, consumption=SEE_DIFF, diff emitted with the canary receipt row cited; the synthetic suppressed review still received recommended_max_review_fix_rounds=3, which is the no_producer finding below reproduced end to end
- verified at: 2026-09-23T23:31Z
