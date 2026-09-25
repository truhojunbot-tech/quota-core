"""Compare a candidate reader contract with a reference, including all decisions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# Only build identity and clock fields may vary across equivalent producers.
ALLOWED_PATHS = {
    "provenance.producer_commit",
    "provenance.produced_at",
    "produced_at",
    "generated_at",
}


def differences(expected, actual, path=""):
    if path in ALLOWED_PATHS:
        return []
    if type(expected) is not type(actual):
        return [(path, expected, actual)]
    if isinstance(expected, dict):
        result = []
        for key in sorted(set(expected) | set(actual)):
            child = f"{path}.{key}" if path else key
            if key not in expected or key not in actual:
                result.append((child, expected.get(key, "<missing>"), actual.get(key, "<missing>")))
            else:
                result.extend(differences(expected[key], actual[key], child))
        return result
    if isinstance(expected, list):
        result = []
        if len(expected) != len(actual):
            result.append((f"{path}.length", len(expected), len(actual)))
        for index, (before, after) in enumerate(zip(expected, actual)):
            result.extend(differences(before, after, f"{path}[{index}]"))
        return result
    return [] if expected == actual else [(path, expected, actual)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--live", type=Path, help="older live contract for input-drift comparison")
    parser.add_argument("--drift-task", help="the one task allowed to differ from the older live input")
    args = parser.parse_args()
    reference = json.loads(args.reference.read_text())
    candidate = json.loads(args.candidate.read_text())
    if not isinstance(reference.get("decisions"), list) or not isinstance(candidate.get("decisions"), list):
        print("ERROR: both contracts must have a decisions list")
        return 1
    before = {item["task_id"]: item for item in reference["decisions"]}
    after = {item["task_id"]: item for item in candidate["decisions"]}
    print("allowed_differences:", ", ".join(sorted(ALLOWED_PATHS)))
    print("shape:", type(candidate["decisions"]).__name__, "contract_version:", candidate.get("contract_version"))
    print("task_sets:", len(before), len(after), "matching:", set(before) == set(after))
    errors = differences({key: value for key, value in reference.items() if key != "decisions"},
                         {key: value for key, value in candidate.items() if key != "decisions"})
    identical = 0
    for task_id in sorted(set(before) & set(after)):
        changes = differences(before[task_id], after[task_id], f"decisions.{task_id}")
        if changes:
            errors.extend(changes)
        else:
            identical += 1
    print("identical_decisions:", f"{identical}/{len(before)}")
    for path, old, new in errors[:100]:
        print("DIFFERENCE", path, "reference=", repr(old), "candidate=", repr(new))
    if len(errors) > 100:
        print("additional_differences:", len(errors) - 100)
    if set(before) != set(after):
        print("missing_tasks:", sorted(set(before) - set(after)))
        print("extra_tasks:", sorted(set(after) - set(before)))
    same_input_ok = (not errors and set(before) == set(after) and identical == len(before)
                     and len(before) == len(reference["decisions"])
                     and len(after) == len(candidate["decisions"]))
    if args.live is None:
        return 0 if same_input_ok else 1
    if not args.drift_task:
        parser.error("--drift-task is required with --live")
    live = json.loads(args.live.read_text())
    if not isinstance(live.get("decisions"), list):
        print("ERROR: live contract must have a decisions list")
        return 1
    old = {item["task_id"]: item for item in live["decisions"]}
    print("live_task_sets:", len(old), len(before), len(after), "matching:", set(old) == set(before) == set(after))
    branch_diffs = {task_id: differences(old[task_id], before[task_id], f"decisions.{task_id}")
                    for task_id in sorted(set(old) & set(before))}
    candidate_diffs = {task_id: differences(old[task_id], after[task_id], f"decisions.{task_id}")
                       for task_id in sorted(set(old) & set(after))}
    branch_changed = {task_id for task_id, changes in branch_diffs.items() if changes}
    candidate_changed = {task_id for task_id, changes in candidate_diffs.items() if changes}
    print("live_identical_decisions: branch", f"{len(old) - len(branch_changed)}/{len(old)}",
          "candidate", f"{len(old) - len(candidate_changed)}/{len(old)}")
    print("drift_task:", args.drift_task)
    print("live_diffs_side_by_side:")
    for task_id in sorted(branch_changed | candidate_changed):
        print("task_id:", task_id)
        branch_lines = branch_diffs.get(task_id, [])
        candidate_lines = candidate_diffs.get(task_id, [])
        for index in range(max(len(branch_lines), len(candidate_lines))):
            print("  branch:   ", repr(branch_lines[index] if index < len(branch_lines) else None))
            print("  candidate:", repr(candidate_lines[index] if index < len(candidate_lines) else None))
    live_metadata = {key: value for key, value in live.items() if key != "decisions"}
    branch_metadata = {key: value for key, value in reference.items() if key != "decisions"}
    candidate_metadata = {key: value for key, value in candidate.items() if key != "decisions"}
    branch_meta_diffs = differences(live_metadata, branch_metadata)
    candidate_meta_diffs = differences(live_metadata, candidate_metadata)
    print("live_metadata_diffs_side_by_side:")
    for index in range(max(len(branch_meta_diffs), len(candidate_meta_diffs))):
        print("  branch:   ", repr(branch_meta_diffs[index] if index < len(branch_meta_diffs) else None))
        print("  candidate:", repr(candidate_meta_diffs[index] if index < len(candidate_meta_diffs) else None))
    drift_ok = (set(old) == set(before) == set(after)
                and len(old) == len(live["decisions"])
                and branch_changed == candidate_changed == {args.drift_task}
                and branch_diffs[args.drift_task] == candidate_diffs[args.drift_task]
                and branch_meta_diffs == candidate_meta_diffs)
    print("same_input_equivalent:", same_input_ok, "live_drift_matches_branch:", drift_ok)
    return 0 if same_input_ok and drift_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
