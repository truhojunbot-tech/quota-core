"""Build evidence/sev0/tokenomics-matched-evidence.{md,json}.

Read-only over every source database (sqlite mode=ro URIs). Writes only the two
evidence files. No collector, no synthetic task, no live write.
"""
from __future__ import annotations
import collections, hashlib, json, statistics, sqlite3, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/tmp/phaseb-me")
from quota_core.context_economics.contract_emitter import build_contract, producer_commit
from quota_core.context_economics.report_producer import policy_contract_sha256

AC_DB = "/home/truhojun/.agent_crew/agent_crew/tasks.db"
OUT = Path("/tmp/phaseb-me/evidence/sev0")
PUBLISHED_SHA = "c255965b9e2a53b62ae903d6387332fef6ee21ba426f4a45fc5ba3de4b61cb5f"
UNKNOWN = "UNKNOWN"


def ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


db = ro(AC_DB)
tasks = {r["task_id"]: dict(r) for r in db.execute("select * from tasks")}
attr = {r["task_id"]: dict(r) for r in db.execute("select * from task_attribution")}
receipts = {r["task_id"]: dict(r) for r in db.execute("select * from tokenomics_shadow_receipts")}
for t in tasks.values():
    try:
        t["ctx"] = json.loads(t["context"] or "{}")
    except Exception:
        t["ctx"] = {}
    try:
        t["err"] = json.loads(t["error_info"] or "{}") or {}
    except Exception:
        t["err"] = {}

contract = build_contract([AC_DB])
decisions = {d["task_id"]: d for d in contract["decisions"]}

# ---------------------------------------------------------------- receipt plane
rcpt_by_source = collections.Counter(r["shadow_decision_source"] or UNKNOWN for r in receipts.values())
rcpt_by_sha = collections.Counter(r["shadow_contract_sha"] or UNKNOWN for r in receipts.values())
rcpt_by_reason = collections.Counter(r["shadow_reason"] or UNKNOWN for r in receipts.values())
fresh = [r for r in receipts.values()
         if r["shadow_decision_source"] == "quota_core_contract"
         and r["shadow_contract_sha"] == PUBLISHED_SHA]
with_recommendation = [r for r in receipts.values()
                       if (r["shadow_recommendation_json"] or "null") not in ("null", None)
                       or (r["recommendation_json"] or "null") not in ("null", None)]

# ------------------------------------------------------------------- join layer
def elapsed(tid):
    a = attr.get(tid, {})
    s, c = a.get("started_at") or 0, a.get("completed_at") or 0
    return round(c - s, 1) if s > 0 and c > 0 and c >= s else None


rows = []
for tid, d in decisions.items():
    a, t = attr.get(tid, {}), tasks.get(tid, {})
    reason = t.get("err", {}).get("reason")
    rows.append({
        "task_id": tid,
        "risk_tier": d["risk_tier"],
        "recommended_max_review_fix_rounds": d["recommended_max_review_fix_rounds"],
        "recommended_provider_tier": d["recommended_provider_tier"],
        "recommended_session_treatment": d["recommended_session_treatment"],
        "recommended_cache_treatment": d["recommended_cache_treatment"],
        "recommended_soft_budget_present": d["recommended_soft_budget"] is not None,
        "quality_preserving": d["quality_preserving"],
        "human_gate_required": d["human_gate_required"],
        "confidence": d["confidence"],
        "override_reasons": d["override_reasons"],
        "actual_status": t.get("status"),
        "actual_outcome": a.get("outcome") or None,
        "terminal_success": (t.get("status") == "completed") if t.get("status") else None,
        "verdict": t.get("verdict"),
        "task_type": a.get("task_type") or t.get("task_type"),
        "agent": a.get("agent") or None,
        "model": a.get("model") or None,
        "no_artifact": reason == "no_artifact",
        "timeout": reason == "dispatcher_timeout" or t.get("status") == "timed_out",
        "no_result_submitted": reason == "no_result_submitted",
        "provider_capacity_failure": reason in ("agy_quota_exhausted", "transient_claude_429_max_retries"),
        "retry_of": a.get("retry_of") or None,
        "fallback_of": a.get("fallback_of") or None,
        "elapsed_seconds": elapsed(tid),
        "provider_session_id": a.get("provider_session_id") or None,
        "context_policy": a.get("context_policy") or None,
        "context_generation": a.get("context_generation"),
        "cache_read_tokens": a.get("cache_read_tokens"),
        "cache_write_tokens": a.get("cache_write_tokens"),
        "uncached_input_tokens": a.get("uncached_input_tokens"),
        "output_tokens": a.get("output_tokens"),
        "reasoning_tokens": a.get("reasoning_tokens"),
        "reviewed_sha": t.get("ctx", {}).get("reviewed_sha"),
        "fix_round": t.get("ctx", {}).get("fix_round"),
        "has_shadow_receipt": tid in receipts,
    })
by_id = {r["task_id"]: r for r in rows}


def rate(sel, key):
    known = [r for r in sel if r[key] is not None]
    if not known:
        return {"value": UNKNOWN, "known": 0, "n": len(sel)}
    hits = sum(1 for r in known if r[key])
    return {"value": round(hits / len(known), 4), "hits": hits, "known": len(known), "n": len(sel)}


def dist(sel, key):
    vals = [r[key] for r in sel if r[key] is not None]
    if not vals:
        return {"median": UNKNOWN, "p90": UNKNOWN, "sum": UNKNOWN, "known": 0, "n": len(sel)}
    s = sorted(vals)
    return {"median": s[len(s) // 2], "p90": s[int(0.9 * (len(s) - 1))], "sum": sum(vals),
            "known": len(vals), "n": len(sel)}


def profile(sel):
    rv = [r for r in sel if r["task_type"] == "review"]
    tv = [r for r in sel if r["task_type"] == "test"]
    return {
        "n": len(sel),
        "terminal_success": rate(sel, "terminal_success"),
        "independent_review_verdict_present": rate([{**r, "v": r["verdict"] is not None} for r in rv], "v") if rv else {"value": UNKNOWN, "known": 0, "n": 0},
        "independent_review_approve_share": (
            {"value": round(sum(1 for r in rv if r["verdict"] == "approve") / len([r for r in rv if r["verdict"]]), 4),
             "known": len([r for r in rv if r["verdict"]]), "n": len(rv)}
            if any(r["verdict"] for r in rv) else {"value": UNKNOWN, "known": 0, "n": len(rv)}),
        "independent_test_verdict_present": (
            {"value": round(sum(1 for r in tv if r["verdict"] is not None) / len(tv), 4), "known": len(tv), "n": len(tv)}
            if tv else {"value": UNKNOWN, "known": 0, "n": 0}),
        "stale_or_missing_artifact_incidence": rate(sel, "no_artifact"),
        "timeout_incidence": rate(sel, "timeout"),
        "no_result_submitted_incidence": rate(sel, "no_result_submitted"),
        "provider_capacity_failure_incidence": rate(sel, "provider_capacity_failure"),
        "retry_count": {"value": UNKNOWN, "note": "retry_of empty on all 610 attribution rows (category C)"},
        "fallback_count": sum(1 for r in sel if r["fallback_of"]),
        "elapsed_seconds": dist(sel, "elapsed_seconds"),
        "cache_read_tokens": dist(sel, "cache_read_tokens"),
        "output_tokens": dist(sel, "output_tokens"),
        "reasoning_tokens": dist(sel, "reasoning_tokens"),
        "provider_lineage": dict(collections.Counter(r["agent"] or UNKNOWN for r in sel)),
        "session_lineage_known": sum(1 for r in sel if r["provider_session_id"]),
        "context_policy": dict(collections.Counter(r["context_policy"] or UNKNOWN for r in sel)),
    }


KINDS = ["recommended_max_review_fix_rounds", "recommended_provider_tier",
         "recommended_session_treatment", "recommended_cache_treatment",
         "recommended_soft_budget_present", "risk_tier"]
kinds = {}
for field in KINDS:
    buckets = collections.defaultdict(list)
    for r in rows:
        buckets[str(r[field])].append(r)
    kinds[field] = {k: profile(v) for k, v in sorted(buckets.items())}

overall = profile(rows)
overrides = collections.Counter()
for d in decisions.values():
    for o in d["override_reasons"]:
        overrides[o] += 1

# ---------------------------------------------------- redundant review analysis
reviews = [r for r in rows if r["task_type"] == "review" and r["reviewed_sha"]]
groups = collections.defaultdict(list)
for r in reviews:
    groups[(str(tasks[r["task_id"]].get("pr_number")), r["reviewed_sha"])].append(r)
for v in groups.values():
    v.sort(key=lambda r: tasks[r["task_id"]]["created_at"])

naive, after_terminal, after_standing_block, flips = [], [], [], []
for key, v in groups.items():
    naive += v[1:]
    seen_terminal = False
    standing = None
    for r in v:
        if seen_terminal:
            after_terminal.append(r)
            if r["verdict"] and r["verdict"] != standing:
                flips.append({"reviewed_sha": r["reviewed_sha"][:8], "from": standing,
                              "to": r["verdict"], "task_id": r["task_id"]})
        if standing == "request_changes":
            after_standing_block.append(r)
        if r["actual_status"] == "completed" and r["verdict"]:
            seen_terminal, standing = True, r["verdict"]


def cohort(sel, name):
    return {
        "name": name,
        "n": len(sel),
        "task_ids": sorted(r["task_id"] for r in sel),
        "verdicts": dict(collections.Counter(r["verdict"] or UNKNOWN for r in sel)),
        "statuses": dict(collections.Counter(r["actual_status"] or UNKNOWN for r in sel)),
        "agents": dict(collections.Counter(r["agent"] or UNKNOWN for r in sel)),
        "cache_read_tokens": dist(sel, "cache_read_tokens"),
        "output_tokens": dist(sel, "output_tokens"),
        "elapsed_seconds": dist(sel, "elapsed_seconds"),
        "produced_approve": sum(1 for r in sel if r["verdict"] == "approve"),
    }


canary = {
    "candidate_named_by_owner": "no-progress / redundant review-fix round suppression",
    "detection_field": "tasks.context.reviewed_sha",
    "review_tasks_total": len(reviews),
    "review_tasks_without_reviewed_sha": sum(1 for r in rows if r["task_type"] == "review") - len(reviews),
    "distinct_reviewed_sha_groups": len(groups),
    "groups_reviewed_more_than_once": sum(1 for v in groups.values() if len(v) > 1),
    "cohorts": {
        "naive_same_sha_rereview": cohort(naive, "every review after the first on an identical SHA"),
        "after_terminal_verdict": cohort(after_terminal, "re-review after a prior review of the same SHA reached a terminal verdict"),
        "after_standing_request_changes": cohort(after_standing_block, "re-review dispatched while request_changes already stood on the identical SHA"),
    },
    "verdict_flips_on_unchanged_sha": flips,
    "fix_round_rows": dict(collections.Counter(
        str(t["ctx"].get("fix_round")) for t in tasks.values() if "fix_round" in t.get("ctx", {}))),
}

# ------------------------------------------------------- metric classification
METRICS = [
    ("terminal outcome / success", "A", "task_attribution.outcome → record.outcome; emitted as decision.evidence.outcome", "608/610 known", "join only"),
    ("token components (uncached_input, cache_write, cache_read, output)", "A", "task_attribution.*_tokens → decision.evidence.token_observations", "174/610 known; 436 UNKNOWN", "join only"),
    ("reasoning tokens", "A", "task_attribution.reasoning_tokens → token_observations.reasoning_tokens", "74/610 known", "join only"),
    ("provider / session / context lineage", "A", "task_attribution.agent, provider_session_id, context_policy, context_generation → decision.provenance + current_behavior", "session 402/610; context_policy 607/610", "join only"),
    ("fallback_of", "A", "task_attribution.fallback_of → decision.evidence.fallback_of", "2/610 set", "join only"),
    ("risk facts (safety_or_live / architecture / routine / human gate)", "A", "task_attribution risk columns → _risk() tier", "127/610 assessed; 483 UNKNOWN", "join only"),
    ("independent review verdict", "B", "tasks.verdict — measured by agent_crew, but quota-core reads only task_attribution, so it never reaches independent_review_correct", "174/254 review tasks carry a verdict", "join only (add tasks.verdict to the attribution read path)"),
    ("elapsed wall time per task", "B", "task_attribution.started_at/completed_at exist and are populated; the contract emits no duration field", "605/610 known", "join only"),
    ("stale / missing-artifact incidence", "B", "tasks.error_info.reason='no_artifact' is recorded; orchestration_waste.stale_tokens is emitted as null for all 610", "5/610", "join only for the incidence form"),
    ("provider capacity failure", "B", "tasks.error_info.reason in {agy_quota_exhausted, transient_claude_429_max_retries}", "45/610", "join only"),
    ("redundant review detection (reviewed_sha)", "B", "tasks.context.reviewed_sha exists; not read by quota-core", "149/254 review tasks", "join only"),
    ("review→fix round index", "B", "tasks.context.fix_round exists", "31 tasks (1:18, 2:7, 3:6)", "join only"),
    ("required_context_recalled / recall_not_applicable", "C", "column exists on task_attribution but no producer writes it", "1/610 set; gates 609/610 decisions off", "gap — do NOT build here"),
    ("independent test verdict", "C", "tasks.verdict is NULL for all 97 test tasks; the tester role never writes one", "0/97", "gap — do NOT build here"),
    ("retry_of", "C", "column exists on task_attribution; empty string on all 610 rows", "0/610", "gap — do NOT build here"),
    ("context_growth_tokens", "C", "contract field exists; no producer", "0/610", "gap — do NOT build here"),
    ("new_evidence_or_progress / repeated_unchanged_state", "C", "the two QualityEvidence fields that drive round adjustment have no producer; neither rationale string fired in 610 decisions", "0/610", "gap — do NOT build here"),
    ("orchestration_waste stale/misrouted/duplicate TOKENS", "C", "emitted as null for all 610; no producer attributes tokens to a waste class", "0/610", "gap — do NOT build here"),
    ("model identity", "C", "task_attribution.model empty on 406/610", "204/610", "gap — do NOT build here"),
    ("per-round token attribution to a PR / review lineage", "D", "no table links token spend to a review-fix lineage; would need a new producer", "n/a", "gap — do NOT build here"),
    ("currency cost", "D", "quota-core ships no pricing table by design; component_costs is all-null without caller-supplied ProviderPricing", "0/610", "gap — do NOT build here"),
]

produced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
src_rows = db.execute("select count(*) from tasks").fetchone()[0]

payload = {
    "artifact": "sev0-tokenomics-matched-evidence",
    "mode": "evidence_only_no_enforcement",
    "produced_at": produced_at,
    "provenance": {
        "gate": "Phase B published 2026-09-23T20:46:34Z (quota-core#80 5802779806, agent_crew#342 5802782579); owner #51 5792288011 §10",
        "published_contract_file_sha": PUBLISHED_SHA,
        "policy_contract_sha256_at_this_commit": policy_contract_sha256(),
        "producer_commit": producer_commit(),
        "sources": [
            {"path_basename": "tasks.db", "access": "sqlite mode=ro",
             "tasks_rows": src_rows, "task_attribution_rows": len(attr),
             "tokenomics_shadow_receipts_rows": len(receipts)},
        ],
        "contract_replay": {
            "method": "build_contract() re-run read-only over the same organic task_attribution rows",
            "decision_count": contract["decision_count"],
            "source_dbs": contract["provenance"]["source_dbs"],
            "caveat": "decisions are a deterministic replay of the pure contract, NOT recommendations that were emitted to and consumed by a live dispatch",
        },
        "writes": ["evidence/sev0/tokenomics-matched-evidence.json", "evidence/sev0/tokenomics-matched-evidence.md"],
    },
    "receipt_plane": {
        "receipts_total": len(receipts),
        "baseline_receipts": rcpt_by_source.get("baseline", 0),
        "fresh_receipts_matching_published_contract_sha": len(fresh),
        "receipts_carrying_any_recommendation": len(with_recommendation),
        "by_shadow_decision_source": dict(rcpt_by_source),
        "by_shadow_contract_sha": dict(rcpt_by_sha),
        "by_shadow_reason": dict(rcpt_by_reason),
        "receipts_joinable_to_a_replayed_decision": sum(1 for t in receipts if t in decisions),
        "finding": ("ZERO receipts carry shadow_decision_source='quota_core_contract' and ZERO carry the "
                    f"published contract sha {PUBLISHED_SHA[:12]}…; all 164 resolved receipts carry an earlier "
                    "artifact sha and the reason 'task_decision_unavailable'. No recommendation was ever "
                    "delivered to a task, so an emitted-recommendation vs actual comparison is not available."),
    },
    "population": {
        "replayed_decisions": len(rows),
        "shadow_receipts": len(receipts),
        "receipts_joined_to_a_decision": sum(1 for r in rows if r["has_shadow_receipt"]),
        "receipts_not_joined": len(receipts) - sum(1 for r in rows if r["has_shadow_receipt"]),
    },
    "admitted_actions": {
        "quality_preserving_true": sum(1 for r in rows if r["quality_preserving"]),
        "quality_preserving_false": sum(1 for r in rows if not r["quality_preserving"]),
        "override_reasons": dict(overrides),
        "finding": ("quality_preserving is False for 610/610 decisions. Every quality-gated recommendation "
                    "(session_treatment, cache_treatment) therefore reads 'insufficient_evidence' for the "
                    "whole population, and no cost-reducing action is admitted anywhere in the organic set."),
    },
    "overall_quality_floor": overall,
    "per_recommendation_kind": kinds,
    "counterfactual_baseline": {
        "method": ("The counterfactual baseline is the observed behaviour itself: for every one of the 610 "
                   "decisions the contract either preserves current treatment or withholds a recommendation "
                   "for insufficient evidence. There is therefore no recommendation-vs-actual divergence to "
                   "measure on the quality floor, and the actual/counterfactual delta is exactly zero by "
                   "construction, not by measurement."),
        "divergent_decisions": 0,
        "note": ("recommended_soft_budget_present splits the population by outcome=='success' (see policy.py "
                 "_envelope gating), so its two profiles restate that split and are NOT independent evidence."),
    },
    "candidate_canary": canary,
    "metric_classification": [
        {"metric": m, "class": c, "where": w, "coverage": cov, "action": act}
        for (m, c, w, cov, act) in METRICS
    ],
    "rows": rows,
}
# Per-task rows are written one compact object per line: the file stays
# auditable row-by-row without a 610-row pretty-print dominating the artifact.
# ``override_reasons`` is aggregated in ``admitted_actions`` instead of repeated
# on every row.
per_task = [{k: v for k, v in r.items() if k != "override_reasons"}
            for r in sorted(payload.pop("rows"), key=lambda r: r["task_id"])]
body = json.dumps(payload, indent=2, sort_keys=True)
rows_json = "[\n" + ",\n".join(
    "    " + json.dumps(r, sort_keys=True) for r in per_task) + "\n  ]"
document = body[:-2].rstrip() + ',\n  "rows": ' + rows_json + "\n}\n"
json.loads(document)  # fail loudly rather than commit an unparseable artifact

OUT.mkdir(parents=True, exist_ok=True)
(OUT / "tokenomics-matched-evidence.json").write_text(document, encoding="utf-8")
print("json written", (OUT / "tokenomics-matched-evidence.json").stat().st_size)
print("naive", len(naive), "after_terminal", len(after_terminal),
      "standing_block", len(after_standing_block), "flips", len(flips))
