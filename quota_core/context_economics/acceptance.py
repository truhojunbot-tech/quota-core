"""Read-only #80 closing-criteria checker for organic shadow reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, Sequence

from .quality_evidence import NOT_RECORDED

PASS = "PASS"
FAIL = "FAIL"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
RISK_FIELDS = ("safety_or_live_change", "broad_architecture_change", "bounded_routine_fix", "human_gate_required")
TOKEN_FIELDS = ("uncached_input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens", "reasoning_tokens")
MIN_ARTIFACTS, MIN_MEASURED_TOKEN_ARTIFACTS, MIN_RUNTIMES = 50, 30, 2
MIN_COVERAGE, MAX_SINGLE_TIER_SHARE = 0.80, 0.90


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _result(status: str, **details: object) -> dict[str, object]:
    return {"status": status, **details}


def _post_cutoff(report: Mapping[str, object], since: int | None) -> tuple[list[dict[str, object]], list[dict[str, object]], int | None]:
    rows = [dict(value) for value in _mapping(report.get("decisions")).values() if isinstance(value, Mapping)]
    if since is None:
        observed = [row["created_at"] for row in rows if isinstance(row.get("created_at"), int) and any(value != NOT_RECORDED for value in _mapping(row.get("evidence_provenance")).values())]
        since = min(observed) if observed else None
    untimestamped = [row for row in rows if not isinstance(row.get("created_at"), int)]
    return ([row for row in rows if isinstance(row.get("created_at"), int) and row["created_at"] >= since], untimestamped, since) if since is not None else ([], untimestamped, None)


def _decision(row: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping(row.get("policy_decision"))


def _recorded(row: Mapping[str, object], field: str) -> bool:
    value = _mapping(row.get("evidence_provenance")).get(field)
    if field in RISK_FIELDS:
        return value == "producer_declared:explicit/high"
    return value not in (None, NOT_RECORDED, "applicable_but_missing", "not_applicable_no_retrieval")


def check_acceptance(
    report: Mapping[str, object], since: int | None = None,
    rerun_bytes_equal: bool | None = None,
) -> dict[str, object]:
    """Evaluate #80 closing criteria with ``INSUFFICIENT_DATA`` for unknowns."""
    rows, untimestamped, cutoff = _post_cutoff(report, since)
    criteria: dict[str, dict[str, object]] = {}
    contract = _mapping(report.get("policy_contract"))
    measured = sum(
        any(
            _mapping(_mapping(_decision(row).get("evidence")).get("token_observations")).get(field) is not None
            for field in TOKEN_FIELDS
        )
        for row in rows
    )
    identities = sorted({
        _mapping(_decision(row).get("provenance")).get("provider")
        for row in rows
        if isinstance(_mapping(_decision(row).get("provenance")).get("provider"), str)
        and _mapping(_decision(row).get("provenance")).get("provider")
    })
    s1_ok = len(rows) >= MIN_ARTIFACTS and measured >= MIN_MEASURED_TOKEN_ARTIFACTS and len(identities) >= MIN_RUNTIMES
    criteria["S1"] = _result(PASS if s1_ok else INSUFFICIENT_DATA, artifact_count=len(rows), untimestamped_artifact_count=len(untimestamped), measured_token_artifact_count=measured, runtime_count=len(identities), runtimes=identities, runtime_identity_field="policy_decision.provenance.provider", thresholds={"artifacts": MIN_ARTIFACTS, "measured_tokens": MIN_MEASURED_TOKEN_ARTIFACTS, "runtimes": MIN_RUNTIMES})
    recall_states = {state: sum(_mapping(row.get("evidence_provenance")).get("recall_applicability") == state for row in rows) for state in ("observed_true", "observed_false", "applicable_but_missing", "not_applicable_no_retrieval", "applicability_unknown")}
    applicable = len(rows) - recall_states["not_applicable_no_retrieval"]
    recall = recall_states["observed_true"] + recall_states["observed_false"]
    recall_coverage = recall / applicable if applicable else None
    criteria["C1"] = _result(INSUFFICIENT_DATA if not applicable else PASS if recall_coverage >= MIN_COVERAGE else FAIL, recorded_count=recall, total_count=applicable, coverage=recall_coverage, recall_state_counts=recall_states, threshold=MIN_COVERAGE)
    coverage = {field: sum(_recorded(row, field) for row in rows) / len(rows) if rows else None for field in RISK_FIELDS}
    criteria["C2"] = _result(INSUFFICIENT_DATA if not rows else PASS if all(value is not None and value >= MIN_COVERAGE for value in coverage.values()) else FAIL, coverage=coverage, threshold=MIN_COVERAGE)
    declared = [row for row in rows if all(_recorded(row, field) for field in RISK_FIELDS)]
    tiers: dict[str, int] = {}
    for row in declared:
        tier = _decision(row).get("risk_tier")
        if isinstance(tier, str): tiers[tier] = tiers.get(tier, 0) + 1
    largest = max(tiers.values()) / len(declared) if tiers else None
    declarations: dict[str, int] = {}
    for row in declared:
        kind = _mapping(row.get("risk_declaration")).get("kind")
        if isinstance(kind, str): declarations[kind] = declarations.get(kind, 0) + 1
    declarations_uniform = len(declarations) == 1 and sum(declarations.values()) == len(declared)
    d1_ok = declarations_uniform or (len(tiers) >= 2 and largest is not None and largest <= MAX_SINGLE_TIER_SHARE)
    criteria["D1"] = _result(INSUFFICIENT_DATA if not declared else PASS if d1_ok else FAIL, declared_count=len(declared), tier_distribution=tiers, declaration_distribution=declarations, declarations_uniform=declarations_uniform, largest_tier_share=largest, threshold=MAX_SINGLE_TIER_SHARE)
    complete = [row for row in rows if _mapping(_decision(row).get("evidence")).get("required_context_recalled") is True and _mapping(_decision(row).get("evidence")).get("independent_review_correct") is True]
    sessions = sum(_decision(row).get("recommended_session_treatment") != "insufficient_evidence" for row in complete)
    caches = sum(_decision(row).get("recommended_cache_treatment") != "insufficient_evidence" for row in complete)
    criteria["D2"] = _result(INSUFFICIENT_DATA if not complete else PASS if sessions and caches else FAIL, complete_quality_count=len(complete), non_insufficient_session_count=sessions, non_insufficient_cache_count=caches)
    low = [row for row in rows if _mapping(row.get("risk_declaration")).get("kind") in {"bounded_routine", "non_production"}]
    non_gated = sum(_decision(row).get("human_gate_required") is False for row in low)
    criteria["D3"] = _result(INSUFFICIENT_DATA if not low else PASS if non_gated else FAIL, declared_bounded_or_non_production_count=len(low), non_gated_count=non_gated)
    safety_rows = rows + untimestamped
    unassessed = [row for row in safety_rows if any(not _recorded(row, field) for field in RISK_FIELDS)]
    v1 = all(_decision(row).get("risk_tier") == "safety_or_live" and _decision(row).get("recommended_session_treatment") == "insufficient_evidence" for row in unassessed)
    criteria["V1"] = _result(INSUFFICIENT_DATA if not unassessed else PASS if v1 else FAIL, checked_count=len(unassessed), untimestamped_checked_count=sum(row in untimestamped for row in unassessed))
    regressions = [row for row in safety_rows if _mapping(_decision(row).get("evidence")).get("independent_review_correct") is False or _mapping(_decision(row).get("evidence")).get("required_context_recalled") is False]
    v2 = all(_decision(row).get("quality_preserving") is False and _decision(row).get("recommended_session_treatment") == "insufficient_evidence" and _decision(row).get("recommended_cache_treatment") == "insufficient_evidence" for row in regressions)
    criteria["V2"] = _result(INSUFFICIENT_DATA if not regressions else PASS if v2 else FAIL, checked_count=len(regressions), untimestamped_checked_count=sum(row in untimestamped for row in regressions))
    decisions = _mapping(report.get("decisions"))
    cost_totals_absent = all("total" not in _mapping(_decision(row).get("component_costs")) for row in rows)
    unknowns_preserved = all(
        _mapping(_decision(row).get("evidence")).get("required_context_recalled") is None
        for row in rows
        if not _recorded(row, "required_context_recalled")
    )
    structural_v3 = report.get("mode") == "shadow" and list(decisions) == sorted(decisions) and cost_totals_absent and unknowns_preserved
    v3_status = FAIL if not structural_v3 or rerun_bytes_equal is False else INSUFFICIENT_DATA if rerun_bytes_equal is None else PASS
    criteria["V3"] = _result(v3_status, checked_count=len(rows), mode=report.get("mode"), decisions_sorted=list(decisions) == sorted(decisions), component_cost_totals_absent=cost_totals_absent, unknowns_preserved_as_null=unknowns_preserved, rerun_byte_identical=rerun_bytes_equal)
    statuses = [item["status"] for item in criteria.values()]
    safety_failures = [name for name in ("V1", "V2", "V3") if criteria[name]["status"] == FAIL]
    if safety_failures:
        overall, action = "DO_NOT_CLOSE", "fix_safety_invariant_in_quota_core"
    elif FAIL in statuses or INSUFFICIENT_DATA in statuses:
        overall, action = "NOT_YET", "wait_for_coverage_or_differentiation_and_rerun"
    else:
        overall, action = "READY_TO_CLOSE", "attach_summary_and_close_issue"
    return {"checker_version": "1.0", "mode": "read_only_acceptance_check", "overall_verdict": overall, "recommended_action": action, "safety_failure_criteria": safety_failures, "cutoff_created_at": cutoff, "policy_contract": {"id": contract.get("id"), "sha256": contract.get("sha256")}, "criteria": criteria}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--since", type=int)
    parser.add_argument("--rerun-report", help="same-input rerun report, compared byte-for-byte")
    args = parser.parse_args(argv)
    report_path = Path(args.report)
    rerun_path = Path(args.rerun_report) if args.rerun_report else None
    if rerun_path is not None and report_path.resolve() == rerun_path.resolve():
        parser.error("--rerun-report must be a distinct file from --report")
    try: report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error: parser.error(f"could not read report: {error}")
    if not isinstance(report, Mapping): parser.error("report must be a JSON object")
    rerun_equal = None
    if rerun_path is not None:
        try: rerun_equal = report_path.read_bytes() == rerun_path.read_bytes()
        except OSError as error: parser.error(f"could not read rerun report: {error}")
    print(json.dumps(check_acceptance(report, args.since, rerun_equal), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
