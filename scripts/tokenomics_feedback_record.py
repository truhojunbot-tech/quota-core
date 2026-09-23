"""Record the SEV-0 §12 tokenomics feedback loop for one pinned canary task.

Read-only over every runtime source (SQLite ``mode=ro`` URIs) and over the
policy producer: the emitter at ``quota_core.context_economics.contract_emitter``
is re-run, never re-published.  The only writes are the two evidence files.

The artifact answers one question and refuses to answer it twice: does the
outcome of an *applied* policy recommendation reach the *next* decision the
producer emits for the same recommendation kind?  That needs two observations
of the same decision record separated by the canary window, so the generator is
deliberately re-runnable and stores its own BEFORE snapshot:

* **pass 1** — run before/while the pinned task's review cascade is open.
  Outcome fields that the cascade has not produced yet are written as
  ``PENDING`` (not ``UNKNOWN``: pending is a thing that can still arrive).
  The decision record for the recommendation kind is captured as ``before``.
* **pass 2** — run after the cascade is terminal.  The stored ``before``
  snapshot is reloaded from the committed artifact, the producer is re-run to
  capture ``after``, and the diff is emitted with the outcome rows that are
  claimed to have driven it cited by task id.

⛔This generator never decides that the loop closed.  It prints the diff and
  the outcome rows next to each other and labels the link ``mechanical`` only
  where a producer actually reads the outcome field in question.  Where no
  producer exists the link is labelled ``no_producer`` and the fields are
  listed in ``unknown_fields`` — an unwired channel reported as a closed loop
  would be exactly the "estimate presented as measurement" this incident is about.
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from quota_core.context_economics.contract_emitter import (  # noqa: E402
    build_contract, producer_commit,
)
from quota_core.context_economics.report_producer import (  # noqa: E402
    policy_contract_sha256,
)

AC_DB = "/home/truhojun/.agent_crew/agent_crew/tasks.db"
OUT_DIR = REPO / "evidence" / "sev0"
STEM = "tokenomics-feedback-loop"

UNKNOWN = "UNKNOWN"
PENDING = "PENDING"

#: The one canary recommendation kind (agent_crew ``tokenomics_canary``
#: 3be7afc, ``RECOMMENDATION_KIND``).
KIND = "suppress_identical_sha_rereview"

#: The contract fields that carry that kind.  ``recommended_max_review_fix_rounds``
#: is the envelope a suppression narrows; the two rationale strings are the only
#: places the contract records *why* the envelope moved.
KIND_FIELDS = ("recommended_max_review_fix_rounds",)
KIND_RATIONALE = (
    "new_evidence_extends_review_fix_envelope",
    "unchanged_state_does_not_extend_review_fix_envelope",
)

#: The QualityEvidence fields that would let a canary outcome move
#: ``recommended_max_review_fix_rounds``.  Grepped to nothing in every producer
#: in this repo at the commit below, which is why the link is not mechanical.
KIND_INPUT_FIELDS = ("repeated_unchanged_state", "new_evidence_or_progress")

CAPACITY_REASONS = ("agy_quota_exhausted", "transient_claude_429_max_retries")
SUPPRESSED_REASON = "tokenomics_canary_suppressed_identical_sha_rereview"


def ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _json(text, default=None):
    try:
        value = json.loads(text or "null")
    except (TypeError, ValueError):
        return default
    return default if value is None else value


def load_runtime(db_path: str) -> dict:
    db = ro(db_path)
    tasks = {}
    for row in db.execute("select * from tasks"):
        item = dict(row)
        item["ctx"] = _json(item.get("context"), {}) or {}
        item["err"] = _json(item.get("error_info"), {}) or {}
        item["findings_parsed"] = _json(item.get("findings"), []) or []
        tasks[item["task_id"]] = item
    attribution = {r["task_id"]: dict(r) for r in db.execute("select * from task_attribution")}
    receipts = {r["task_id"]: dict(r)
                for r in db.execute("select * from tokenomics_shadow_receipts")}
    authz = [dict(r) for r in db.execute(
        "select * from authorization_receipts order by row_id")]
    counts = {
        "tasks": len(tasks),
        "task_attribution": len(attribution),
        "tokenomics_shadow_receipts": len(receipts),
        "authorization_receipts": len(authz),
    }
    db.close()
    return {"tasks": tasks, "attribution": attribution, "receipts": receipts,
            "authz": authz, "counts": counts}


def lineage_of(tasks: dict, pinned: str) -> list[str]:
    """Task ids descended from ``pinned`` through the review/fix cascade.

    The cascade records its parent in ``context.prev_task_id`` — the same field
    the canary's pin is compared against — so that is the edge walked here.  The
    id-substring fallback catches a cascade task whose context was truncated;
    it can only ever add tasks named after the pinned one, never unrelated work.
    """
    members = {pinned}
    changed = True
    while changed:
        changed = False
        for task_id, task in tasks.items():
            if task_id in members:
                continue
            parent = (task["ctx"].get("prev_task_id") or "").strip()
            if parent in members or pinned in task_id:
                members.add(task_id)
                changed = True
    return sorted(members, key=lambda t: tasks[t].get("created_at") or 0)


def dist(values: list) -> dict:
    known = [v for v in values if v is not None]
    if not known:
        return {"sum": UNKNOWN, "known": 0, "n": len(values)}
    return {"sum": sum(known), "min": min(known), "max": max(known),
            "known": len(known), "n": len(values)}


def kind_projection(contract: dict, lineage: list[str]) -> dict:
    """Project the contract down to the one recommendation kind under test.

    Per-task for the lineage (so a diff names the task that moved) and a fleet
    aggregate (so a lineage-local change can be told apart from a producer-wide
    one).
    """
    decisions = {d["task_id"]: d for d in contract["decisions"]}
    per_task = {}
    for task_id in lineage:
        decision = decisions.get(task_id)
        if decision is None:
            per_task[task_id] = {"present": False}
            continue
        per_task[task_id] = {
            "present": True,
            "risk_tier": decision["risk_tier"],
            **{f: decision[f] for f in KIND_FIELDS},
            "kind_rationale": [r for r in decision["rationale"] if r in KIND_RATIONALE],
            "quality_preserving": decision["quality_preserving"],
            "outcome": decision["evidence"]["outcome"],
            "override_reasons": decision["override_reasons"],
            "token_observations": decision["evidence"]["token_observations"],
        }
    rounds = collections.Counter(
        str(d["recommended_max_review_fix_rounds"]) for d in contract["decisions"])
    fired = collections.Counter()
    for decision in contract["decisions"]:
        for rationale in decision["rationale"]:
            if rationale in KIND_RATIONALE:
                fired[rationale] += 1
    return {
        "kind": KIND,
        "contract_fields": list(KIND_FIELDS),
        "per_task": per_task,
        "fleet": {
            "decision_count": contract["decision_count"],
            "recommended_max_review_fix_rounds_histogram": dict(sorted(rounds.items())),
            "kind_rationale_fired": {r: fired.get(r, 0) for r in KIND_RATIONALE},
        },
    }


def diff_projection(before: dict, after: dict) -> dict:
    """Field-level diff of two kind projections, with no interpretation."""
    changes = {"per_task": {}, "fleet": {}}
    task_ids = sorted(set(before.get("per_task", {})) | set(after.get("per_task", {})))
    for task_id in task_ids:
        was, now = before.get("per_task", {}).get(task_id), after.get("per_task", {}).get(task_id)
        if was == now:
            continue
        keys = sorted(set(was or {}) | set(now or {}))
        changes["per_task"][task_id] = {
            key: {"before": (was or {}).get(key, UNKNOWN), "after": (now or {}).get(key, UNKNOWN)}
            for key in keys if (was or {}).get(key) != (now or {}).get(key)
        }
    for key in sorted(set(before.get("fleet", {})) | set(after.get("fleet", {}))):
        was, now = before.get("fleet", {}).get(key), after.get("fleet", {}).get(key)
        if was != now:
            changes["fleet"][key] = {"before": was, "after": now}
    changes["changed"] = bool(changes["per_task"] or changes["fleet"])
    return changes


def canary_rows(receipts: dict, lineage: list[str]) -> list[dict]:
    rows = []
    for task_id in lineage:
        receipt = receipts.get(task_id)
        if not receipt or receipt.get("canary_resolved_at") is None:
            continue
        rows.append({
            "task_id": task_id,
            "canary_decision_source": receipt["canary_decision_source"],
            "canary_applied": bool(receipt["canary_applied"]),
            "canary_reason": receipt["canary_reason"],
            "canary_counterfactual": receipt["canary_counterfactual"],
            "canary_cea_receipt_id": receipt["canary_cea_receipt_id"],
            "canary_resolved_at": receipt["canary_resolved_at"],
            "canary_recommendation": _json(receipt["canary_recommendation_json"], {}),
        })
    return rows


def build(pinned: str, db_path: str, previous: dict | None) -> dict:
    runtime = load_runtime(db_path)
    tasks, attribution, receipts = runtime["tasks"], runtime["attribution"], runtime["receipts"]
    if pinned not in tasks:
        raise SystemExit(f"pinned task {pinned} not present in {db_path}")

    lineage = lineage_of(tasks, pinned)
    reviews = [t for t in lineage if (tasks[t].get("task_type") or "") == "review"]
    terminal = {"completed", "failed", "cancelled", "timed_out"}
    open_tasks = [t for t in lineage if (tasks[t].get("status") or "") not in terminal]
    canary = canary_rows(receipts, lineage)
    applied = [row for row in canary if row["canary_applied"]]
    suppressed = [t for t in lineage
                  if tasks[t]["err"].get("reason") == SUPPRESSED_REASON]

    # A pass-1 artifact is one where the canary window is still open: the
    # cascade has produced no resolved canary receipt AND some task is live.
    is_pass_2 = bool(canary) and not open_tasks
    pass_number = 2 if is_pass_2 else 1
    outcome_state = "OBSERVED" if is_pass_2 else PENDING

    contract = build_contract([db_path])
    projection = kind_projection(contract, lineage)

    cea = [r for r in runtime["authz"] if r["task_id"] == pinned]
    cea_receipt_id = cea[-1]["receipt_id"] if cea else UNKNOWN

    def attr(task_id, column):
        return (attribution.get(task_id) or {}).get(column)

    review_attr = {c: dist([attr(t, c) for t in reviews])
                   for c in ("cache_read_tokens", "cache_write_tokens",
                             "uncached_input_tokens", "output_tokens", "reasoning_tokens")}
    capacity = [
        {"task_id": t, "reason": tasks[t]["err"].get("reason"), "status": tasks[t].get("status")}
        for t in lineage if tasks[t]["err"].get("reason") in CAPACITY_REASONS
    ]

    unknown_fields = [
        {"field": " / ".join(KIND_INPUT_FIELDS),
         "where": "quota_core.context_economics.policy.QualityEvidence — the only inputs that move "
                  "recommended_max_review_fix_rounds",
         "why": "no producer writes them; neither string appears in report_producer, "
                "quality_evidence or agent_crew_adapter at this commit",
         "consequence": "a recorded canary outcome cannot mechanically change the round "
                        "recommendation; the link is reported as no_producer, not as closed"},
        {"field": "tokenomics_shadow_receipts.canary_*",
         "where": "agent_crew tasks.db",
         "why": "quota-core reads task_attribution only; it never reads the canary columns",
         "consequence": "the canary outcome is joinable by task_id for a human reader, "
                        "but is not an input to the emitted contract"},
        {"field": "independent_review_correct",
         "where": "decision.evidence.independent_review_correct",
         "why": "tasks.verdict is measured by agent_crew but is not on quota-core's read path",
         "consequence": "quality_preserving stays False for the lineage regardless of verdict"},
        {"field": "orchestration_waste.duplicate_tokens",
         "where": "decision.orchestration_waste",
         "why": "no producer attributes tokens to a waste class",
         "consequence": "avoided_duplicate_rework is counted in tasks and in the standing "
                        "review's own token rows, never as a contract-side waste figure"},
        {"field": "currency cost",
         "where": "decision.component_costs.components",
         "why": "quota-core ships no pricing table by design",
         "consequence": "all economics below are token counts, never money"},
    ]

    record = {
        "policy_recommendation": {
            "kind": KIND,
            "source": "agent_crew.tokenomics_canary 3be7afc (DECISION_SOURCE='quota_core_contract'), "
                      "admitted by the matched evidence at quota-core 869a3cf",
            "statement": "do not dispatch a review to a commit on which a request_changes verdict "
                         "already stands for the same (PR or branch, reviewed_sha)",
            "armed_via": "AGENT_CREW_TOKENOMICS_CANARY_TASK_ID",
            "pinned_task_id": pinned,
            "scope": "one task: only reviews whose context.prev_task_id equals the pin",
            "contract_field_carrying_the_kind": list(KIND_FIELDS),
            "emitted_recommendation_for_pinned_task": projection["per_task"].get(pinned),
        },
        "authorization_receipt_id": {
            "cea_receipt_id": cea_receipt_id,
            "decision": cea[-1]["decision"] if cea else UNKNOWN,
            "reason": _json(cea[-1]["reason"], UNKNOWN) if cea else UNKNOWN,
            "states_recorded": [r["state"] for r in cea] or UNKNOWN,
            "enforced": False,
            "note": "the enqueue receipt decided BLOCK (ALREADY_COMPLETED) with enforcement off; "
                    "the task was admitted anyway and the /start gate answered go:true. Recorded "
                    "as observed, not as an authorization the loop relied on.",
            "canary_receipt_ids": [row["canary_cea_receipt_id"] for row in canary] or PENDING,
        },
        "actual_action": {
            "state": outcome_state,
            "reviews_in_lineage": reviews,
            "canary_evaluations_recorded": len(canary),
            "suppressions_applied": len(applied),
            "suppressed_review_task_ids": suppressed or ([] if is_pass_2 else PENDING),
            "open_lineage_tasks": open_tasks,
            "note": "pass 1 runs inside the canary window: the pinned task's own review cascade "
                    "has not been dispatched yet, so no canary evaluation exists to report."
                    if not is_pass_2 else
                    "cascade terminal; every canary evaluation recorded at dispatch is listed above.",
        },
        "counterfactual_baseline": {
            "state": outcome_state,
            "method": "the canary records its own counterfactual at dispatch "
                      "(tokenomics_shadow_receipts.canary_counterfactual): the review that would "
                      "have been dispatched on the unchanged sha",
            "counterfactuals": [row["canary_counterfactual"] for row in applied] or PENDING,
            "baseline_window_reference": "evidence/sev0/tokenomics-matched-evidence.json "
                                         "candidate_canary.cohorts.after_standing_request_changes "
                                         "— 11 organic tasks, 0 approve",
        },
        "quality_result": {
            "state": outcome_state,
            "lineage_verdicts": {t: tasks[t].get("verdict") for t in reviews},
            "lineage_statuses": {t: tasks[t].get("status") for t in lineage},
            "reused_verdict_count": sum(
                1 for row in applied
                if (row["canary_recommendation"] or {}).get("standing_verdict")),
            "quality_preserving_in_contract": {
                t: projection["per_task"].get(t, {}).get("quality_preserving") for t in lineage},
            "caveat": "quality_preserving is contract-side and stays False while "
                      "independent_review_correct has no producer (see unknown_fields).",
        },
        "token_cache_economics": {
            "state": outcome_state,
            "unit": "tokens (no currency — quota-core ships no pricing table)",
            "lineage_review_tasks": review_attr,
            "avoided_at_suppression": (
                {"method": "tokens a suppressed review did not spend cannot be measured directly; "
                           "the observable proxy is the standing review's own token rows",
                 "standing_review_task_ids": [
                     (row["canary_recommendation"] or {}).get("standing_review_task_id")
                     for row in applied]}
                if applied else PENDING),
        },
        "provider_capacity_economics": {
            "state": outcome_state,
            "capacity_failures_in_lineage": capacity,
            "reviewer_dispatches_avoided": len(applied) if is_pass_2 else PENDING,
            "note": "each applied suppression is one reviewer invocation not spent against the "
                    "claude weekly limit; the figure is a count of dispatches, not of tokens.",
        },
        "retry_round_reduction": {
            "state": outcome_state,
            "fix_rounds_observed": dict(collections.Counter(
                str(tasks[t]["ctx"].get("fix_round")) for t in lineage
                if "fix_round" in tasks[t]["ctx"])) or PENDING,
            "review_round_count": len(reviews),
            "recommended_max_review_fix_rounds_for_pinned_task":
                projection["per_task"].get(pinned, {}).get("recommended_max_review_fix_rounds", UNKNOWN),
            "reduction_measured": (len(applied) if is_pass_2 else PENDING),
            "caveat": "a suppression removes a review dispatch from the round, it does not lower "
                      "the contract's recommended_max_review_fix_rounds (no producer for the "
                      "inputs that would).",
        },
        "avoided_duplicate_rework": {
            "state": outcome_state,
            "duplicate_reviews_avoided": len(applied) if is_pass_2 else PENDING,
            "findings_reused_instead_of_regenerated": sum(
                len((row["canary_recommendation"] or {}).get("standing_findings", []) or [])
                for row in applied) if is_pass_2 else PENDING,
            "reused_from_task_ids": [
                (row["canary_recommendation"] or {}).get("standing_review_task_id")
                for row in applied] or PENDING,
        },
        "rollback_status": {
            "state": "ARMED_AND_REVERSIBLE",
            "mechanism": "unset AGENT_CREW_TOKENOMICS_CANARY_TASK_ID",
            "restart_required": False,
            "reason": "the pin is read per dispatch (tokenomics_canary.canary_pin), so unsetting it "
                      "restores shadow behaviour on the next dispatch with no state to unwind",
            "state_written_by_the_canary": "tokenomics_shadow_receipts.canary_* only (evidence); "
                                           "plus the terminal result of any suppressed review",
            "rolled_back": False,
            "published_contract_changed": False,
            "note": "this generator publishes nothing: it re-runs the producer read-only and writes "
                    "two files under evidence/sev0/.",
        },
        "unknown_fields": unknown_fields,
    }

    before = (previous or {}).get("decision_record", {}).get("before")
    decision_record = {
        "kind": KIND,
        "producer": "quota_core.context_economics.contract_emitter.build_contract (read-only; "
                    "emitter path cfba4e8)",
        "before": before or projection,
        "before_captured_at": ((previous or {}).get("decision_record", {}) or {}).get(
            "before_captured_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "after": projection if (is_pass_2 and before) else None,
        "diff": diff_projection(before, projection) if (is_pass_2 and before) else None,
        "consumption_claim": (
            {"status": "NOT_ESTABLISHED_YET",
             "why": "pass 1 captures the BEFORE decision only; the canary window (this task's own "
                    "review cascade) has not run."}
            if not (is_pass_2 and before) else
            {"status": "SEE_DIFF",
             "outcome_rows_cited": [
                 {"table": "tokenomics_shadow_receipts", "task_id": row["task_id"],
                  "canary_applied": row["canary_applied"], "canary_reason": row["canary_reason"],
                  "canary_resolved_at": row["canary_resolved_at"]} for row in canary],
             "mechanical_link": "no_producer",
             "explanation": "any change in the diff comes from rows the producer DOES read "
                            "(task_attribution outcome/token columns, new lineage tasks entering "
                            "the contract), never from the canary columns; "
                            "see unknown_fields[0]."}),
    }

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "artifact": "sev0-tokenomics-feedback-loop",
        "mode": "evidence_only_no_enforcement",
        "pass": pass_number,
        "pinned_task_id": pinned,
        "produced_at": now,
        "provenance": {
            "generator": "scripts/tokenomics_feedback_record.py",
            "producer_commit": producer_commit(),
            "policy_contract_sha256": policy_contract_sha256(),
            "canary_implementation": "agent_crew 3be7afc src/agent_crew/tokenomics_canary.py",
            "gate": "canary executed 2026-09-23T23:25:37Z; :8105 restarted onto agent_crew fd4f4cb "
                    "with AGENT_CREW_TOKENOMICS_CANARY_TASK_ID=" + pinned,
            "sources": [{"path_basename": Path(db_path).name, "access": "sqlite mode=ro",
                         "row_counts": runtime["counts"]}],
            "writes": [f"evidence/sev0/{STEM}.json", f"evidence/sev0/{STEM}.md"],
            "publishes": [],
            "pass_2_verification": {
                "method": "the pass-2 branch was exercised before pass 1 was committed, against a "
                          "sqlite backup COPY of tasks.db in /tmp seeded with one synthetic "
                          "suppressed review; the live DB was opened mode=ro only and the copy was "
                          "deleted afterwards",
                "result": "pass=2, consumption=SEE_DIFF, diff emitted with the canary receipt row "
                          "cited; the synthetic suppressed review still received "
                          "recommended_max_review_fix_rounds=3, which is the no_producer finding "
                          "below reproduced end to end",
                "verified_at": "2026-09-23T23:31Z",
            },
        },
        "lineage": {
            "task_ids": lineage,
            "reviews": reviews,
            "open_tasks": open_tasks,
            "canary_receipts_resolved": len(canary),
            "rows": [{"task_id": t, "task_type": tasks[t].get("task_type"),
                      "status": tasks[t].get("status"), "verdict": tasks[t].get("verdict"),
                      "reviewed_sha": tasks[t]["ctx"].get("reviewed_sha"),
                      "fix_round": tasks[t]["ctx"].get("fix_round"),
                      "error_reason": tasks[t]["err"].get("reason"),
                      "created_at": tasks[t].get("created_at")} for t in lineage],
        },
        "canary_receipt_rows": canary,
        "feedback_record": record,
        "decision_record": decision_record,
    }


def to_markdown(payload: dict) -> str:
    record = payload["feedback_record"]
    decision = payload["decision_record"]
    lines = [
        f"# SEV-0 §12 — tokenomics feedback loop (pass {payload['pass']})",
        "",
        f"- pinned task: `{payload['pinned_task_id']}`",
        f"- produced at: {payload['produced_at']}",
        f"- producer commit: `{payload['provenance']['producer_commit']}`",
        f"- policy contract sha256: `{payload['provenance']['policy_contract_sha256']}`",
        f"- sources: {payload['provenance']['sources'][0]['path_basename']} (sqlite mode=ro)",
        f"- publishes: none — writes `{STEM}.json` and `{STEM}.md` only",
        "",
        "## Recorded fields",
        "",
        "| field | state | value |",
        "| --- | --- | --- |",
    ]
    for key in ("policy_recommendation", "authorization_receipt_id", "actual_action",
                "counterfactual_baseline", "quality_result", "token_cache_economics",
                "provider_capacity_economics", "retry_round_reduction",
                "avoided_duplicate_rework", "rollback_status"):
        value = record[key]
        state = value.get("state", "RECORDED") if isinstance(value, dict) else "RECORDED"
        summary = {
            "policy_recommendation": lambda v: v["statement"],
            "authorization_receipt_id": lambda v: f"`{v['cea_receipt_id']}` decision={v['decision']} enforced={v['enforced']}",
            "actual_action": lambda v: f"{v['canary_evaluations_recorded']} canary evaluation(s), {v['suppressions_applied']} applied",
            "counterfactual_baseline": lambda v: str(v["counterfactuals"]),
            "quality_result": lambda v: f"verdicts={v['lineage_verdicts']}",
            "token_cache_economics": lambda v: f"lineage review cache_read={v['lineage_review_tasks']['cache_read_tokens']}",
            "provider_capacity_economics": lambda v: f"reviewer dispatches avoided={v['reviewer_dispatches_avoided']}",
            "retry_round_reduction": lambda v: f"reviews={v['review_round_count']}, reduction={v['reduction_measured']}",
            "avoided_duplicate_rework": lambda v: f"duplicate reviews avoided={v['duplicate_reviews_avoided']}",
            "rollback_status": lambda v: v["mechanism"],
        }[key](value)
        lines.append(f"| `{key}` | {state} | {summary} |")
    lines += [
        "",
        "## UNKNOWN fields (explicit)",
        "",
        "| field | why | consequence |",
        "| --- | --- | --- |",
    ]
    for item in record["unknown_fields"]:
        lines.append(f"| `{item['field']}` | {item['why']} | {item['consequence']} |")
    lines += [
        "",
        f"## Decision record for kind `{KIND}`",
        "",
        f"Producer: {decision['producer']}",
        "",
        f"- BEFORE captured at {decision['before_captured_at']}; "
        f"fleet histogram of `recommended_max_review_fix_rounds`: "
        f"{decision['before']['fleet']['recommended_max_review_fix_rounds_histogram']}",
        f"- BEFORE `decision_count`: {decision['before']['fleet']['decision_count']}",
        f"- AFTER: {'captured' if decision['after'] else 'not captured in this pass'}",
        "",
        f"Consumption claim: **{decision['consumption_claim']['status']}** — "
        f"{decision['consumption_claim'].get('why') or decision['consumption_claim'].get('explanation')}",
        "",
        "## Lineage",
        "",
        "| task | type | status | verdict | reviewed_sha | error |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in payload["lineage"]["rows"]:
        sha = (row["reviewed_sha"] or "")[:9] or "—"
        lines.append(f"| `{row['task_id']}` | {row['task_type']} | {row['status']} | "
                     f"{row['verdict'] or '—'} | `{sha}` | {row['error_reason'] or '—'} |")
    lines += [
        "",
        "## How to reproduce",
        "",
        "```bash",
        f"python3.12 scripts/tokenomics_feedback_record.py {payload['pinned_task_id']}",
        "```",
        "",
        "Re-running after the pinned task's review cascade is terminal reloads the BEFORE "
        "snapshot from the committed JSON, captures AFTER, and emits the diff. Every read is "
        "`sqlite mode=ro`; nothing is published.",
        "",
        "### Pass-2 branch verification",
        "",
        f"- method: {payload['provenance']['pass_2_verification']['method']}",
        f"- result: {payload['provenance']['pass_2_verification']['result']}",
        f"- verified at: {payload['provenance']['pass_2_verification']['verified_at']}",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", help="the pinned canary (implement) task id")
    parser.add_argument("--db", default=AC_DB, help="agent_crew tasks.db (read-only)")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    json_path, md_path = out_dir / f"{STEM}.json", out_dir / f"{STEM}.md"
    previous = _json(json_path.read_text(encoding="utf-8"), None) if json_path.exists() else None

    payload = build(args.task_id, args.db, previous)
    document = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    json.loads(document)  # fail loudly rather than commit an unparseable artifact

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(document, encoding="utf-8")
    md_path.write_text(to_markdown(payload), encoding="utf-8")
    print(json.dumps({
        "pass": payload["pass"],
        "lineage_tasks": len(payload["lineage"]["task_ids"]),
        "canary_receipts_resolved": payload["lineage"]["canary_receipts_resolved"],
        "decision_count": payload["decision_record"]["before"]["fleet"]["decision_count"],
        "consumption": payload["decision_record"]["consumption_claim"]["status"],
        "json": str(json_path), "md": str(md_path),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
