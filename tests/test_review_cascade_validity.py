"""#74: task-row evidence for stale and misrouted review cascades."""
import json
import sqlite3
from pathlib import Path

from quota_core.context_economics.agent_crew_adapter import read_task_cascade_signals
from quota_core.context_economics.analytics import compare_context_policies, context_policy_cohort, orchestration_cascade_summary
from quota_core.context_economics.correlate import attach_task_cascade_validity
from quota_core.context_economics.schema import TaskEconomicsRecord, TokenComponents, task_economics_to_dict
from quota_core.context_economics.sqlite_attribution import read_task_attribution_sqlite


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
    assert loaded["review-5652-old"].orchestration_validity == "stale"
    assert loaded["review-9aeb0354"].orchestration_validity == "misrouted"
    ids = ["review-5652-old", "fix-review-5652-old-r1", "review-5652-28419e96",
           "review-9aeb0354", "fix-review-9aeb0354-r1", "review-impl-watch-corrected"]
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
    signals["test-review-9aeb0354"] = {
        "task_type": "test", "branch": "main",
        "context": {"prev_task_id": "review-9aeb0354"},
    }
    [test_row] = attach_task_cascade_validity([
        TaskEconomicsRecord(task_id="test-review-9aeb0354", runtime="cli", task_type="test",
                            context_policy="fresh")], signals)
    assert test_row.orchestration_validity == "misrouted"
    assert context_policy_cohort(test_row) != "fresh"
    round_review, round_fix = attach_task_cascade_validity([
        TaskEconomicsRecord(task_id="review-harvest-499-r1", runtime="cli", task_type="review", context_policy="resume"),
        TaskEconomicsRecord(task_id="fix-review-harvest-499-r1-r1", runtime="cli", task_type="implement", context_policy="resume"),
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
