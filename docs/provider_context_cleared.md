# Context-clearing interventions (quota-core#72)

**Status: production-sample pending.** The consumer path below is implemented
and tested. It has never been run against organic data, and nothing it produces
may be presented as measured auto-clear economics until it has. See
[What is still missing](#what-is-still-missing).

## The problem

An orchestrator can clear a provider session's context when its window crosses a
threshold. It does so by sending keystrokes to the session, and it records the
result honestly:

| producer `outcome` | what it actually establishes |
|---|---|
| `attempted` | the keystrokes were **delivered**. Nothing about whether the provider acted on them. |
| `send_failed` | the send itself failed, so the intervention did not land. |

`attempted` is the interesting one. A successful send is not a successful clear
— but the same send sets the reset flag on the task, so the **next** dispatch is
attributed `context_policy="fresh"` either way.

quota-core#70's policy stratification groups by `context_policy`. Without the
distinction, a dispatch that may never have been cleared is counted alongside
contexts that genuinely started empty, and a resume-vs-fresh comparison silently
becomes an average over an unknown mixture — with nothing left in the data to
reveal it. That is the contamination this work exists to stop.

## The three states, kept apart

| `context_clear_status` | meaning | cohort |
|---|---|---|
| `None` | no clearing row was joined — **unknown**, not "no intervention happened" | its own `context_policy` |
| `"attempted"` | sent, provider completion unconfirmed | `<policy>+auto_clear_unconfirmed` |
| `"confirmed"` | a provider-native signal confirmed the clear | its own `context_policy` (genuinely fresh) |
| `"failed"` | the keystrokes never landed | its own `context_policy` |

`"confirmed"` has no producer today. It is carried now so that introducing such a
signal later does not reclassify rows already written as `attempted` — a test
pins that.

An outcome this version does not recognise maps to `status=None` and keeps the
producer's string in `raw_outcome`. It is never folded into `attempted`: a
guessed state is indistinguishable from a measured one in every cohort
downstream.

## The path

```
provider_context_cleared (JSONL)
  → provider_context_clearing_from_event        parse one row
  → provider_context_clearings_from_events      one clearing per dispatch
  → attach_provider_context_clearings           join on task_id + context identity
  → context_policy_cohort / compare_context_policies
```

Two properties this path is built on, both inherited from quota-core#70:

- **Joined on identity, never on timing.** The producer already knows which
  dispatch it cleared. A shared `task_id` whose context identity *contradicts*
  the record is refused with a note rather than merged.
- **The join never edits `context_policy`.** The producer decides policy; this
  only records what intervention was observed alongside it. In particular a
  `send_failed` clearing cannot make anything look fresh.

Both `context_clear_status` and `context_clear_outcome` cross the persistence
boundary in `task_economics_to_dict`. quota-core#70's review caught the opposite
mistake — fields joined in memory and dropped on write, so in-process tests
passed and only the written artifact was wrong — and a test here asserts a
reader working from the artifact alone reaches the same cohort.

## What is still missing

1. **No organic sample.** Every fixture is shaped from the producer's emit site,
   not captured from a live run. Until post-deployment clearing rows exist and
   have been run through this path, the cohort split is a mechanism with no
   measurement behind it.
2. **No confirmation signal.** `"confirmed"` cannot occur until a provider
   exposes something that actually establishes the context was cleared. Until
   then every real intervention lands in the unconfirmed cohort, which is
   correct but means that cohort is not yet a useful comparison group — it is a
   quarantine.
3. **Therefore: no E2E economics claim.** The question this was meant to help
   answer — what a context clear actually costs or saves — stays open. quota-core#72
   should remain open for the organic-sample follow-up after this lands.
