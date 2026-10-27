"""#74: task-row evidence for stale and misrouted review cascades."""
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from quota_core.context_economics.agent_crew_adapter import read_task_cascade_signals
from quota_core.context_economics.analytics import compare_context_policies, context_policy_cohort, orchestration_cascade_summary
from quota_core.context_economics.correlate import attach_task_cascade_validity
from quota_core.context_economics.schema import TaskEconomicsRecord, TaskTokenTelemetry, TokenComponents, task_economics_to_dict
from quota_core.context_economics.sqlite_attribution import read_task_attribution_sqlite
from quota_core.context_economics.policy import QualityEvidence, recommend_task_policy
from quota_core.context_economics.report_producer import main as produce_report


def test_production_shaped_cascade_signals_are_not_context_policy_samples(tmp_path):
    fixture = Path(__file__).parent / "fixtures/agent_crew/cascade_validity.json"
    rows = json.loads(fixture.read_text())
    db = tmp_path / "tasks.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_type TEXT, branch TEXT, context TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?)", [
        (r["task_id"], r["task_type"], r["branch"], json.dumps(r["context"])) for r in rows])
    conn.execute("CREATE TABLE task_attribution (task_id TEXT PRIMARY KEY, task_type TEXT, context_policy TEXT, outcome TEXT)")
    conn.executemany("INSERT INTO task_attribution VALUES (?, ?, ?, ?)", [
        (r["task_id"], r["task_type"], "fresh", "completed") for r in rows])
    conn.commit()
    conn.close()
    signals = read_task_cascade_signals(db)
    loaded = {r.task_id: r for r in read_task_attribution_sqlite(db)}
    assert loaded["review-42-old"].orchestration_validity == "stale"
    assert loaded["review-branch-mismatch"].orchestration_validity == "misrouted"
    ids = ["review-42-old", "fix-review-42-old-r1", "review-42-new-head",
           "review-branch-mismatch", "fix-review-branch-mismatch-r1", "review-impl-sample-corrected"]
    records = [TaskEconomicsRecord(task_id=task_id, runtime="cli", context_policy="fresh",
               task_type="implement" if task_id.startswith("fix-") else "review",
               outcome="success", tokens=TokenComponents(provider_total=10)) for task_id in ids]
    results = attach_task_cascade_validity(records, signals)
    assert [r.orchestration_validity for r in results] == [
        "stale", "stale", "valid", "misrouted", "misrouted", "valid"]
    assert results[0].orchestration_origin_task_id == results[1].orchestration_origin_task_id
    assert results[3].orchestration_origin_task_id == results[4].orchestration_origin_task_id
    assert all(context_policy_cohort(r) != "fresh" for r in (results[0], results[1], results[3], results[4]))
    assert compare_context_policies(results)["fresh"]["count"] == 2
    assert compare_context_policies(results)["fresh+orchestration_stale"]["orchestration"]["counts"]["stale"] == 2
    assert any("misrouted" in note for note in results[4].attribution_notes)
    assert task_economics_to_dict(results[4])["orchestration_validity"] == "misrouted"
    summary = orchestration_cascade_summary(results)
    assert summary["counts"] == {"valid": 2, "stale": 2, "misrouted": 2, "unknown": 0}
    assert summary["waste_task_count"] == 4
    assert summary["waste_known_tokens"] == 40
    assert orchestration_cascade_summary([results[0], results[0]])["waste_task_count"] == 1
    signals["test-review-branch-mismatch"] = {
        "task_type": "test", "branch": "main",
        "context": {"prev_task_id": "review-branch-mismatch"},
    }
    [test_row] = attach_task_cascade_validity([
        TaskEconomicsRecord(task_id="test-review-branch-mismatch", runtime="cli", task_type="test",
                            context_policy="fresh")], signals)
    assert test_row.orchestration_validity == "misrouted"
    assert context_policy_cohort(test_row) != "fresh"
    round_review, round_fix = attach_task_cascade_validity([
        TaskEconomicsRecord(task_id="review-round-sample-r1", runtime="cli", task_type="review", context_policy="resume"),
        TaskEconomicsRecord(task_id="fix-review-round-sample-r1-r1", runtime="cli", task_type="implement", context_policy="resume"),
    ], signals)
    assert round_review.orchestration_validity == "unknown"
    assert round_fix.orchestration_validity == "unknown"
    assert context_policy_cohort(round_review) == "resume"
    assert orchestration_cascade_summary([round_review, round_fix])["waste_task_count"] == 0


def test_missing_signal_is_unknown_not_valid():
    record = TaskEconomicsRecord(task_id="old-review", runtime="cli", task_type="review", context_policy="fresh")
    [out] = attach_task_cascade_validity([record], {})
    assert out.orchestration_validity == "unknown"
    assert context_policy_cohort(out) == "fresh"
    assert orchestration_cascade_summary([out])["counts"]["valid"] == 0


def test_missing_review_signal_does_not_change_shadow_policy_decision():
    """#74 is an attribution axis, not a policy-input or threshold change."""
    record = TaskEconomicsRecord(
        task_id="older-review", runtime="cli", task_type="review",
        context_policy="resume", outcome="success",
        tokens=TokenComponents(provider_total=120),
    )
    before = recommend_task_policy(record).to_dict()
    [joined] = attach_task_cascade_validity([record], {})
    assert joined.orchestration_validity == "unknown"
    assert context_policy_cohort(joined) == "resume"
    assert recommend_task_policy(joined).to_dict() == before


def test_only_known_invalid_work_adds_waste_without_changing_recommendations():
    evidence = QualityEvidence(
        safety_or_live_change=False, broad_architecture_change=False,
        bounded_routine_fix=True, human_gate_required=False,
        independent_review_correct=True, required_context_recalled=True,
    )
    record = TaskEconomicsRecord(
        task_id="review-sample", runtime="cli", task_type="review",
        context_policy="resume", outcome="success",
        task_telemetry=TaskTokenTelemetry(
            uncached_input_tokens=2, cache_write_tokens=3,
            cache_read_tokens=5, output_tokens=7, reasoning_tokens=11,
        ),
    )
    before = recommend_task_policy(record, evidence).to_dict()
    for validity, field in (("stale", "stale_tokens"), ("misrouted", "misrouted_tokens")):
        decision = recommend_task_policy(replace(record, orchestration_validity=validity), evidence).to_dict()
        assert decision["orchestration_waste"] == {
            "stale_tokens": 17 if validity == "stale" else None,
            "misrouted_tokens": 17 if validity == "misrouted" else None,
            "duplicate_tokens": None,
        }
        decision["orchestration_waste"] = before["orchestration_waste"]
        assert decision == before
    assert recommend_task_policy(replace(record, orchestration_validity="valid"), evidence).to_dict() == before
    assert recommend_task_policy(replace(record, orchestration_validity="unknown"), evidence).to_dict() == before
    partial = replace(record, orchestration_validity="stale", task_telemetry=replace(
        record.task_telemetry, cache_read_tokens=None))
    assert recommend_task_policy(partial, evidence).orchestration_waste["stale_tokens"] is None
    provider_total = replace(record, orchestration_validity="misrouted",
                             tokens=TokenComponents(provider_total=23),
                             task_telemetry=TaskTokenTelemetry())
    assert recommend_task_policy(provider_total, evidence).orchestration_waste["misrouted_tokens"] == 23
    measured_zero = replace(record, orchestration_validity="stale",
                            task_telemetry=TaskTokenTelemetry(
                                uncached_input_tokens=0, cache_write_tokens=0,
                                cache_read_tokens=0, output_tokens=0))
    assert recommend_task_policy(measured_zero, evidence).orchestration_waste["stale_tokens"] == 0
    malformed = replace(record, orchestration_validity="stale",
                        task_telemetry=replace(record.task_telemetry, output_tokens=-1))
    assert recommend_task_policy(malformed, evidence).orchestration_waste["stale_tokens"] is None


def test_sqlite_cascade_tokens_reach_emitted_contract(tmp_path):
    db = tmp_path / "synthetic.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, task_type TEXT, branch TEXT, context TEXT)")
        tasks = [
            ("review-stale", "review", "feature", {"review_publication": "stale"}),
            ("review-stale-partial", "review", "feature", {"review_publication": "stale"}),
            ("fix-stale", "implement", "feature", {"prev_task_id": "review-stale"}),
            ("impl-parent", "implement", "feature", {"result_branch": "feature", "result_commit": "sha-a"}),
            ("review-misrouted", "review", "other", {"prev_task_id": "impl-parent", "reviewed_sha": "sha-a"}),
            ("test-misrouted", "test", "other", {"prev_task_id": "review-misrouted"}),
            ("review-unknown", "review", "feature", {}),
        ]
        conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?)",
                         [(task_id, kind, branch, json.dumps(context))
                          for task_id, kind, branch, context in tasks])
        conn.execute("""CREATE TABLE task_attribution (
            task_id TEXT PRIMARY KEY, created_at REAL, task_type TEXT,
            context_policy TEXT, outcome TEXT, uncached_input_tokens INTEGER,
            cache_write_tokens INTEGER, cache_read_tokens INTEGER,
            output_tokens INTEGER, reasoning_tokens INTEGER)""")
        for task_id, kind, _branch, _context in tasks:
            if task_id == "impl-parent":
                continue
            values = (2, 3, None, 7, 11) if task_id in ("review-unknown", "review-stale-partial") else (2, 3, 5, 7, 11)
            conn.execute("INSERT INTO task_attribution VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (task_id, 100.25, kind, "resume", "completed", *values))
    report_path, contract_path = tmp_path / "report.json", tmp_path / "contract.json"
    assert produce_report(["--db", str(db), "--out", str(report_path),
                           "--contract-out", str(contract_path)]) == 0
    decisions = {row["task_id"]: row for row in json.loads(contract_path.read_text())["decisions"]}
    for task_id in ("review-stale", "fix-stale"):
        assert decisions[task_id]["orchestration_waste"]["stale_tokens"] == 17
    for task_id in ("review-misrouted", "test-misrouted"):
        assert decisions[task_id]["orchestration_waste"]["misrouted_tokens"] == 17
    assert decisions["review-stale-partial"]["orchestration_waste"]["stale_tokens"] is None
    assert decisions["review-unknown"]["orchestration_waste"] == {
        "stale_tokens": None, "misrouted_tokens": None, "duplicate_tokens": None,
    }
