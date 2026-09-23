# SEV-0 §12 — tokenomics feedback loop (pass 1)

- pinned task: `sev0-tokenomics-feedback-loop-x`
- produced at: 2026-09-23T23:31:47+00:00
- producer commit: `869a3cf76a3306d94f44c700f2afbda741c32825`
- policy contract sha256: `e325a5412e32fc22901731bd5a24d92d4ac47afc699c5357dfd6e8f246e14218`
- sources: tasks.db (sqlite mode=ro)
- publishes: none — writes `tokenomics-feedback-loop.json` and `tokenomics-feedback-loop.md` only

## Recorded fields

| field | state | value |
| --- | --- | --- |
| `policy_recommendation` | RECORDED | do not dispatch a review to a commit on which a request_changes verdict already stands for the same (PR or branch, reviewed_sha) |
| `authorization_receipt_id` | RECORDED | `7567a365-fec7-418b-8709-9554519b4416` decision=BLOCK enforced=False |
| `actual_action` | PENDING | 0 canary evaluation(s), 0 applied |
| `counterfactual_baseline` | PENDING | PENDING |
| `quality_result` | PENDING | verdicts={} |
| `token_cache_economics` | PENDING | lineage review cache_read={'sum': 'UNKNOWN', 'known': 0, 'n': 0} |
| `provider_capacity_economics` | PENDING | reviewer dispatches avoided=PENDING |
| `retry_round_reduction` | PENDING | reviews=0, reduction=PENDING |
| `avoided_duplicate_rework` | PENDING | duplicate reviews avoided=PENDING |
| `rollback_status` | ARMED_AND_REVERSIBLE | unset AGENT_CREW_TOKENOMICS_CANARY_TASK_ID |

## UNKNOWN fields (explicit)

| field | why | consequence |
| --- | --- | --- |
| `repeated_unchanged_state / new_evidence_or_progress` | no producer writes them; neither string appears in report_producer, quality_evidence or agent_crew_adapter at this commit | a recorded canary outcome cannot mechanically change the round recommendation; the link is reported as no_producer, not as closed |
| `tokenomics_shadow_receipts.canary_*` | quota-core reads task_attribution only; it never reads the canary columns | the canary outcome is joinable by task_id for a human reader, but is not an input to the emitted contract |
| `independent_review_correct` | tasks.verdict is measured by agent_crew but is not on quota-core's read path | quality_preserving stays False for the lineage regardless of verdict |
| `orchestration_waste.duplicate_tokens` | no producer attributes tokens to a waste class | avoided_duplicate_rework is counted in tasks and in the standing review's own token rows, never as a contract-side waste figure |
| `currency cost` | quota-core ships no pricing table by design | all economics below are token counts, never money |

## Decision record for kind `suppress_identical_sha_rereview`

Producer: quota_core.context_economics.contract_emitter.build_contract (read-only; emitter path cfba4e8)

- BEFORE captured at 2026-09-23T23:29:58+00:00; fleet histogram of `recommended_max_review_fix_rounds`: {'1': 31, '3': 595}
- BEFORE `decision_count`: 626
- AFTER: not captured in this pass

Consumption claim: **NOT_ESTABLISHED_YET** — pass 1 captures the BEFORE decision only; the canary window (this task's own review cascade) has not run.

## Lineage

| task | type | status | verdict | reviewed_sha | error |
| --- | --- | --- | --- | --- | --- |
| `sev0-tokenomics-feedback-loop-x` | implement | in_progress | — | `—` | — |

## How to reproduce

```bash
python3.12 scripts/tokenomics_feedback_record.py sev0-tokenomics-feedback-loop-x
```

Re-running after the pinned task's review cascade is terminal reloads the BEFORE snapshot from the committed JSON, captures AFTER, and emits the diff. Every read is `sqlite mode=ro`; nothing is published.

### Pass-2 branch verification

- method: the pass-2 branch was exercised before pass 1 was committed, against a sqlite backup COPY of tasks.db in /tmp seeded with one synthetic suppressed review; the live DB was opened mode=ro only and the copy was deleted afterwards
- result: pass=2, consumption=SEE_DIFF, diff emitted with the canary receipt row cited; the synthetic suppressed review still received recommended_max_review_fix_rounds=3, which is the no_producer finding below reproduced end to end
- verified at: 2026-09-23T23:31Z
