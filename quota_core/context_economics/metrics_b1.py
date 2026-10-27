"""Read-only B1.3 measurement table (definition version 1).

The caller supplies source paths. Missing producer evidence is reported as
unknown with a reason, never as a measured zero or a passing gate.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .agent_crew_adapter import _iter_jsonl, read_lifecycle_events_jsonl

DEF_VERSION = 1
EVAL_SHA256 = "0bdb218d0c8e6ba3da8202c19201fee52a9605664ebe3ea4fb1d5de92de4ac61"
METRICS = ("M1", "M2", "M3a", "M3b", "M3c", "M4", "M5", "M6", "M7", "M8")
_SECRET_PATTERNS = re.compile(
    r"gh" r"p_[A-Za-z0-9]{36}(?![A-Za-z0-9])|github_pat_[A-Za-z0-9_]{22,}"
    r"|sk-ant-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,}"
    r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}(?![0-9A-Z])"
    r"|(?<![0-9])\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"
    r"|-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"
    r"(?:[\s\S]*?-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----)?"
)


def _redact_secrets(value: str) -> str:
    """Redact common secret-token shapes without importing a private runtime."""
    return _SECRET_PATTERNS.sub("[REDACTED]", value)


def _transcript_project(path: Path) -> str:
    """Derive a project from common encoded transcript directory shapes."""
    raw = path.parent.name
    crew = re.search(r"--agent-crew-worktrees-([^-]+(?:-[^-]+)*)-(?:claude|codex|gemini)$", raw)
    if crew:
        return crew.group(1).replace("_", "-")
    instance = re.search(r"(?:^|-)instances-(.+)$", raw)
    if instance:
        return instance.group(1).replace("_", "-")
    return raw.strip("-").replace("_", "-")


def _transcript_requests(path: Path, since: float, until: float) -> tuple[
        list[tuple[float, str, float, float, float]], list[float]]:
    seen: set[str] = set()
    requests = []
    boundaries = []
    try:
        with path.open("rb") as stream:
            for line in stream:
                if b'"usage"' not in line and b'"compactMetadata"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("subtype") == "compact_boundary" and "compactMetadata" in row:
                    at = _time(row.get("timestamp"))
                    if at is not None and since <= at < until:
                        boundaries.append(at)
                if row.get("type") != "assistant" or not isinstance(row.get("message"), dict):
                    continue
                usage = row["message"].get("usage")
                if not isinstance(usage, dict):
                    continue
                at = _time(row.get("timestamp"))
                identity = row["message"].get("id") or row.get("uuid")
                if at is None or not since - 7200 <= at < until + 7200 or not isinstance(identity, str) or identity in seen:
                    continue
                parts = [_nonnegative(usage.get(key)) for key in
                         ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
                if any(part is None for part in parts):
                    continue
                seen.add(identity)
                requests.append((at, identity, *parts))
    except OSError:
        return [], []
    return sorted(requests), sorted(set(boundaries))


def _lifecycle_transitions(lifecycle_log: Path, since: float,
                           until: float) -> dict[str, list[float]]:
    by_project: dict[str, list[float]] = defaultdict(list)
    for path in (Path(str(lifecycle_log) + ".1"), lifecycle_log):
        try:
            stream = path.open("rb")
        except OSError:
            continue
        with stream:
            for line in stream:
                if b'"success"' not in line or not (b'"RENEW"' in line or b'"compact"' in line):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                at = _time(event.get("timestamp"))
                if at is None or not since <= at < until:
                    continue
                if event.get("event_type") == "success" and event.get("action") in {"RENEW", "compact"}:
                    project = str(event.get("project") or "").replace("_", "-")
                    by_project[project].append(at)
    return by_project


def _session_cache(bot: str, since: float, until: float, transcript_root: Path,
                   transitions: list[float]) -> tuple[float | None, int, str, int, int]:
    """Use renewal_measure's project/time join; ambiguous matches remain unknown."""
    if not transcript_root.is_dir():
        return None, 0, f"transcript_root_unavailable:{transcript_root}", 0, len(transitions)
    candidates = []
    for path in transcript_root.glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime < since - 7200:
                continue
        except OSError:
            continue
        if _transcript_project(path) == bot.replace("_", "-"):
            requests, boundaries = _transcript_requests(path, since, until)
            if requests or boundaries:
                candidates.append((path, requests, boundaries))
    cached = total = 0.0
    joined = 0
    transcript_boundaries = [(at, path, requests) for path, requests, boundaries in candidates
                             for at in boundaries]
    all_transition_times = sorted(set(transitions + [at for at, _, _ in transcript_boundaries]))
    if not all_transition_times:
        return None, 0, "no_context_transition_in_window", 0, 0
    for at, path, requests in transcript_boundaries:
        next_transition = min((stamp for stamp in all_transition_times if stamp > at), default=until)
        selected = [row for row in requests if at < row[0] < min(next_transition, until)]
        amount = sum(sum(row[2:5]) for row in selected)
        if amount <= 0:
            continue
        cached += sum(row[4] for row in selected)
        total += amount
        joined += 1
    for at in sorted(set(transitions)):
        if any(abs(at - boundary) <= 120 for boundary, _, _ in transcript_boundaries):
            continue
        matches = []
        for path, requests, _ in candidates:
            following = [row for row in requests if at < row[0] <= at + 7200]
            if following:
                matches.append((following[0][0] - at, path, following))
        if not matches:
            continue
        matches.sort(key=lambda item: item[0])
        if len(matches) > 1 and matches[0][0] == matches[1][0]:
            continue
        _, _, following = matches[0]
        next_transition = min((stamp for stamp in all_transition_times if stamp > at), default=until)
        selected = [row for row in following if row[0] < min(next_transition, until)]
        amount = sum(sum(row[2:5]) for row in selected)
        if amount <= 0:
            continue
        cached += sum(row[4] for row in selected)
        total += amount
        joined += 1
    return (cached / total if total else None, joined,
            "no_unambiguous_positive_transcript_generation_join" if not joined else "",
            joined, len(all_transition_times))


def _time(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return None
    return None


def _nonnegative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _row(bot: str, metric: str, source: str | None, n: int | None, value: object,
         reason: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"bot": bot, "metric": metric,
                                 "def_version": DEF_VERSION, "source": source,
                                 "n": n, "value": value}
    if value is None:
        result["reason"] = reason or "source_evidence_unavailable"
    return result


def _receipts(db_path: Path, since: float, until: float) -> list[dict[str, object]]:
    if not db_path.is_file():
        return []
    try:
        with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            rows = conn.execute(
                "SELECT economics_json FROM tokenomics_shadow_receipts "
                "WHERE created_at >= ? AND created_at < ?", (since, until)
            ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for (raw,) in rows:
        try:
            value = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _task_ids(db_path: Path, since: float, until: float) -> set[str] | None:
    if not db_path.is_file():
        return None
    try:
        with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            rows = conn.execute("SELECT task_id FROM tasks WHERE created_at >= ? AND created_at < ?",
                                (since, until)).fetchall()
    except sqlite3.Error:
        return None
    return {row[0] for row in rows if isinstance(row[0], str)}


def _served_item_metric(bot: str, events_file: Path, memory_db: Path | None,
                        since: float, until: float) -> dict[str, object]:
    events = [event for event in _iter_jsonl(events_file)
              if event.get("event_type") == "context_pack_built" and event.get("shadow") is not True
              and (at := _time(event.get("ts"))) is not None and since <= at < until]
    if not events:
        result = _row(bot, "M7", str(events_file) if events_file.is_file() else None,
                      0, None, "no_nonshadow_served_item_receipts_in_window")
        result["join_coverage"] = None
        return result
    if memory_db is None:
        return _row(bot, "M7", None, None, None, "memory_db_not_supplied:--memory-db")
    if not memory_db.is_file():
        return _row(bot, "M7", None, None, None, f"memory_db_unavailable:{memory_db}")
    counts = {"superseded": 0, "cross_project": 0, "secret_matches": 0, "total": 0}
    joined = missing = 0
    try:
        with closing(sqlite3.connect(f"{memory_db.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            wanted = {key for event in events
                      for key in (event.get("result_ids") if isinstance(event.get("result_ids"), list) else [])
                      if isinstance(key, str)}
            memory_rows: dict[str, list[tuple[object, ...]]] = defaultdict(list)
            # The primary key begins with layer, not key. One read-only scan is
            # far cheaper than a full table scan per served item.
            for key, value, scope, superseded_by, invalidated_at in conn.execute(
                    "SELECT key, value, scope, superseded_by, invalidated_at FROM adr001_memory"):
                if key in wanted:
                    memory_rows[key].append((value, scope, superseded_by, invalidated_at))
            for event in events:
                ids = event.get("result_ids")
                selected_count = event.get("selected_count")
                if not isinstance(ids, list) or not isinstance(selected_count, int) or \
                        isinstance(selected_count, bool) or selected_count != len(ids):
                    missing += 1
                    continue
                flags = event.get("superseded")
                for index, key in enumerate(ids):
                    if not isinstance(key, str):
                        missing += 1
                        continue
                    matches = memory_rows.get(key, [])
                    if len(matches) != 1:
                        missing += 1
                        continue
                    value, raw_scope, superseded_by, invalidated_at = matches[0]
                    try:
                        scope = json.loads(raw_scope) if raw_scope else {}
                    except ValueError:
                        scope = {}
                    if not isinstance(scope, dict) or not isinstance(value, str):
                        missing += 1
                        continue
                    joined += 1
                    counts["superseded"] += bool(superseded_by or invalidated_at or
                        (isinstance(flags, list) and index < len(flags) and flags[index] is True))
                    project = scope.get("project")
                    counts["cross_project"] += bool(project and project not in (bot, event.get("project")))
                    counts["secret_matches"] += _redact_secrets(value) != value
    except sqlite3.Error as exc:
        return _row(bot, "M7", None, None, None, f"served_item_db_error:{type(exc).__name__}")
    counts["total"] = sum(counts.values())
    if missing:
        result = _row(bot, "M7", f"{events_file};{memory_db}:adr001_memory", joined,
                      None, f"unresolved_served_item_receipts:{missing}")
        result["known_violation_counts"] = counts
        result["join_coverage"] = joined / (joined + missing)
        return result
    result = _row(bot, "M7", f"{events_file};{memory_db}:adr001_memory", joined, counts)
    result["join_coverage"] = 1.0
    return result


def _json_object(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _github_followup(bot: str, issue: object, pr_number: object,
                     approved_at: float, end: float, *,
                     cache: dict[str, list[object] | None] | None = None,
                     search_from: float | None = None) -> bool | None:
    """Read-only corroboration: PR history, same-issue fixes, and local revert log.

    ``None`` is deliberate when one of these sources cannot be checked. It is
    never promoted to an observed clean 72-hour window.
    """
    if not isinstance(issue, int) or isinstance(issue, bool) or not isinstance(pr_number, int):
        return None
    # A project may itself be an owner/repo slug. The hosting account is
    # deliberately not inferred from a private fleet configuration.
    repo = bot
    if cache is None:
        cache = {}
    try:
        if repo not in cache:
            floor = datetime.fromtimestamp(search_from or approved_at, timezone.utc).date()
            listing = subprocess.run(
                ["gh", "pr", "list", "-R", repo, "--state", "all", "--limit", "1000",
                 "--search", f"created:>={floor.isoformat()}",
                 "--json", "number,title,body,createdAt"], capture_output=True, text=True,
                timeout=20, check=False)
            if listing.returncode:
                cache[repo] = None
            else:
                prs = json.loads(listing.stdout)
                # A capped listing cannot prove that no matching follow-up exists.
                cache[repo] = prs if isinstance(prs, list) and len(prs) < 1000 else None
        prs = cache[repo]
        if prs is None:
            return None
        for item in prs:
            at = _time(item.get("createdAt"))
            if at is None or not approved_at < at <= end or item.get("number") == pr_number:
                continue
            body = str(item.get("title") or "") + " " + str(item.get("body") or "")
            if re.search(rf"(?<!\d)#{issue}(?!\d)", body) and re.search(r"\bfix|follow.?up|revert\b", body, re.I):
                return True
        events = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{pr_number}/events", "--paginate"],
            capture_output=True, text=True, timeout=20, check=False)
        if events.returncode:
            return None
        history = json.loads(events.stdout)
        if any(event.get("event") == "reopened" and
               (at := _time(event.get("created_at"))) is not None and approved_at < at <= end
               for event in history if isinstance(event, dict)):
            return True
        local_repos = os.environ.get("QUOTA_CORE_LOCAL_REPOS")
        if not local_repos:
            return None
        repo_path = Path(local_repos).expanduser() / bot
        if not (repo_path / ".git").exists():
            return None
        log = subprocess.run(
            ["git", "-C", str(repo_path), "log", "--all", "--format=%s%n%b",
             f"--since={datetime.fromtimestamp(approved_at, timezone.utc).isoformat()}",
             f"--until={datetime.fromtimestamp(end, timezone.utc).isoformat()}",
             "--grep=Revert", "-i"], capture_output=True, text=True, timeout=20, check=False)
        if log.returncode:
            return None
        return bool(re.search(rf"(?<!\d)#{pr_number}(?!\d)", log.stdout))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _review_followup_metric(bot: str, db: Path, since: float, until: float) -> dict[str, object]:
    if not db.is_file():
        return _row(bot, "M8", None, 0, None, "no_tasks_db")
    try:
        with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
            receipt_columns = {row[1] for row in conn.execute("PRAGMA table_info(tokenomics_shadow_receipts)")}
            needed_tasks = {"task_id", "task_type", "context", "verdict", "result_posted_at", "created_at", "receipt_id"}
            needed_receipts = {"task_id", "shadow_recommendation_json", "actual_execution_json"}
            if not needed_tasks <= task_columns or not needed_receipts <= receipt_columns:
                return _row(bot, "M8", str(db), None, None, "review_followup_columns_unavailable")
            tasks = {row["task_id"]: dict(row) for row in conn.execute("SELECT * FROM tasks")}
            receipts = {row["task_id"]: dict(row) for row in
                        conn.execute("SELECT * FROM tokenomics_shadow_receipts")}
    except sqlite3.Error as exc:
        return _row(bot, "M8", str(db), None, None, f"review_followup_db_error:{type(exc).__name__}")
    known = correct = eligible = unknown = 0
    github_cache: dict[str, list[object] | None] = {}
    for review in tasks.values():
        approved_at = _time(review.get("result_posted_at"))
        if review.get("task_type") != "review" or review.get("verdict") != "approve" or \
                approved_at is None or not since - 72 * 3600 <= approved_at < until - 72 * 3600:
            continue
        eligible += 1
        parent_id = _json_object(review.get("context")).get("prev_task_id")
        root = tasks.get(parent_id) if isinstance(parent_id, str) else None
        root_receipt = receipts.get(parent_id) if isinstance(parent_id, str) else None
        if not root or not root.get("receipt_id") or not root_receipt or \
                not _json_object(root_receipt.get("shadow_recommendation_json")) \
                or not _json_object(root_receipt.get("actual_execution_json")):
            unknown += 1
            continue
        end = approved_at + 72 * 3600
        followups = [task for task in tasks.values()
                     if _json_object(task.get("context")).get("prev_task_id") in
                        (parent_id, review.get("task_id"))
                     and (task.get("task_type") == "implement" or
                          (task.get("task_type") == "test" and
                           (task.get("status") in ("failed", "needs_human") or
                            task.get("verdict") in ("fail", "failed"))))
                     and (at := _time(task.get("created_at"))) is not None
                     and approved_at < at <= end]
        if followups:
            # A linked fix or attributable test failure is a definitive negative
            # even if external PR history is unavailable.
            known += 1
            continue
        root_context = _json_object(root.get("context"))
        external = _github_followup(bot, root_context.get("issue_number"),
                                    root.get("pr_number"), approved_at, end,
                                    cache=github_cache, search_from=since - 72 * 3600)
        if external is None:
            unknown += 1
            continue
        known += 1
        correct += not external
    result = _row(bot, "M8", str(db) + ":tasks,tokenomics_shadow_receipts", known,
                  correct / known if known else None,
                  "no_complete_matured_review_followup_join_in_window")
    result["eligible"] = eligible
    result["unknown"] = unknown
    return result


def _percentile95(values: list[float]) -> float:
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * 0.95) - 1)]


def _score_eval_cases(eval_set: Mapping[str, object], storage: object,
                      scope_factory: object) -> dict[str, tuple[int, int]]:
    """Count frozen EVAL hits in the rendered head plus first five middle rows."""
    scores = {}
    for metric, group in (("M3a", "owner_cases"), ("M3b", "task_cases")):
        cases = eval_set[group]
        if not isinstance(cases, list):
            raise ValueError(f"frozen_eval_set_invalid:{group}")
        hits = 0
        for case in cases:
            if not isinstance(case, dict):
                raise ValueError(f"frozen_eval_set_invalid:{group}")
            scope = scope_factory(fleet="fleet", project=case["project"])
            response = storage.retrieve_ranked(scope, case["query"], "implementer", 20, 16000)
            served = response["head"] + response["middle"][:5]
            if metric == "M3a":
                keys = {row.get("key") for row in served if isinstance(row, dict)}
                hits += any(key in keys for key in case["expected"])
            else:
                text = " ".join(json.dumps(row, sort_keys=True, ensure_ascii=False)
                                for row in served if isinstance(row, dict)).casefold()
                tokens = set(re.findall(r"[a-z0-9]+", text))
                hits += any(isinstance(item, dict) and isinstance(item.get("ref"), str)
                            and (wanted := re.findall(r"[a-z0-9]+", item["ref"].casefold()))
                            and all(token in tokens for token in wanted)
                            for item in case["expected"])
        scores[metric] = (hits, len(cases))
    return scores


def _replay_frozen_eval(eval_set: Mapping[str, object], memory_db: Path) -> dict[str, tuple[int, int]]:
    """Replay on a snapshot: HybridMemoryStorage may migrate or embed its DB."""
    if not memory_db.is_file():
        raise FileNotFoundError(f"memory_db_unavailable:{memory_db}")
    raw_src = os.environ.get("LEMMALOG_AGENT_CREW_SRC")
    if not raw_src:
        raise ImportError("agent_crew_src_unavailable:LEMMALOG_AGENT_CREW_SRC")
    src = Path(raw_src).expanduser()
    package_root = src / "src" if (src / "src" / "agent_crew" / "memory_hybrid.py").is_file() else src
    if not (package_root / "agent_crew" / "memory_hybrid.py").is_file():
        raise ImportError(f"agent_crew_src_unavailable:LEMMALOG_AGENT_CREW_SRC={src}")
    sys.path.insert(0, str(package_root))
    try:
        hybrid = importlib.import_module("agent_crew.memory_hybrid")
        if Path(hybrid.__file__).resolve() != (package_root / "agent_crew" / "memory_hybrid.py").resolve():
            raise ImportError(f"agent_crew_src_mismatch:LEMMALOG_AGENT_CREW_SRC={src}")
        runtime = importlib.import_module("agent_crew.memory_runtime")
    finally:
        sys.path.remove(str(package_root))
    with tempfile.TemporaryDirectory(prefix="quota-core-m3-replay-") as tmp:
        snapshot = Path(tmp) / "adr001_memory.db"
        with closing(sqlite3.connect(f"{memory_db.resolve().as_uri()}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target)
        storage = hybrid.HybridMemoryStorage(str(snapshot))
        return _score_eval_cases(eval_set, storage, runtime.MemoryScope)


def compute_metrics(since: float, until: float, crew_root: str | Path,
                    hook_log: str | Path, *, bots: list[str] | None = None,
                    eval_path: str | Path | None = None,
                    memory_db: str | Path | None = None,
                    transcript_root: str | Path | None = None,
                    lifecycle_log: str | Path | None = None) -> list[dict[str, object]]:
    """Compute one source-attributed row per B1.3 metric per bot.

    The window is half-open [since, until). A missing producer or pre-A2
    retrieval path yields a reasoned null. This never writes a source.
    """
    if until <= since:
        raise ValueError("until must be greater than since")
    root, hook_path = Path(crew_root).expanduser(), Path(hook_log).expanduser()
    transcript_path = Path(transcript_root).expanduser() if transcript_root else None
    lifecycle_path = Path(lifecycle_log).expanduser() if lifecycle_log else None
    lifecycle_by_project = (_lifecycle_transitions(lifecycle_path, since, until)
                            if lifecycle_path is not None else {})
    memory_path = Path(memory_db).expanduser() if memory_db else None
    hook_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in _iter_jsonl(hook_path):
        bot = event.get("bot")
        at = _time(event.get("ts"))
        if (isinstance(bot, str) and bot and at is not None and since <= at < until
                and event.get("identity_source") != "headless_oneshot"):
            hook_rows[bot].append(event)
    bot_names = set(bots) if bots is not None else set(hook_rows)
    if bots is None and root.is_dir():
        bot_names.update(path.name for path in root.iterdir() if (path / "tasks.db").is_file())
    eval_source = str(eval_path) if eval_path else "frozen_eval_set:" + EVAL_SHA256
    eval_reason = "frozen_eval_set_missing:--eval-path"
    eval_scores = None
    if eval_path is not None:
        path = Path(eval_path).expanduser()
        if not path.is_file():
            eval_reason = "frozen_eval_set_missing"
        else:
            contents = path.read_bytes()
            if hashlib.sha256(contents).hexdigest() != EVAL_SHA256:
                eval_reason = "frozen_eval_set_sha256_mismatch"
            elif memory_path is None:
                eval_reason = "memory_db_not_supplied:--memory-db"
            else:
                try:
                    eval_set = json.loads(contents)
                    eval_scores = _replay_frozen_eval(eval_set, memory_path)
                    eval_source = f"{path};{memory_path}"
                except (OSError, ImportError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
                    eval_reason = str(exc) or type(exc).__name__
    result: list[dict[str, object]] = []
    for bot in sorted(bot_names):
        db = root / bot / "tasks.db"
        events_file = root / bot / "context_events.jsonl"
        receipts = _receipts(db, since, until)
        hooks = hook_rows[bot]
        db_source, hook_source = str(db), str(hook_path)

        known = []
        for receipt in receipts:
            parts = [_nonnegative(receipt.get(key)) for key in
                     ("uncached_input_tokens", "cache_write_tokens", "cache_read_tokens")]
            if all(part is not None for part in parts) and sum(parts) > 0:
                known.append(parts)
        total = sum(sum(parts) for parts in known)
        cache_read = sum(parts[2] for parts in known)
        m1 = _row(bot, "M1", db_source + ":tokenomics_shadow_receipts", len(known),
                  cache_read / total if total else None,
                  "no_complete_positive_economics_receipts_in_window")
        session_value, session_n, session_reason, joined, transitions = (
            _session_cache(bot, since, until, transcript_path,
                           lifecycle_by_project.get(bot.replace("_", "-"), []))
            if transcript_path is not None and lifecycle_path is not None else
            (None, 0, "bot_session_sources_not_supplied", 0, 0))
        m1["bot_session_value"] = session_value
        m1["bot_session_n"] = session_n
        m1["bot_session_transition_count"] = transitions
        m1["bot_session_join_coverage"] = joined / transitions if transitions else None
        if session_value is None:
            m1["bot_session_reason"] = session_reason
        result.append(m1)

        heads = {x["head_hash"] for x in hooks if isinstance(x.get("head_hash"), str)
                 and x["head_hash"]}
        result.append(_row(bot, "M2", hook_source, sum(bool(x.get("head_hash")) for x in hooks),
                           len(heads) if heads else None,
                           "no_head_hash_in_window"))
        for metric in ("M3a", "M3b"):
            score = eval_scores.get(metric) if eval_scores is not None else None
            result.append(_row(bot, metric, eval_source, score[1] if score else None,
                               score[0] / score[1] if score and score[1] else None,
                               eval_reason))

        packs = []
        if events_file.is_file():
            for event in read_lifecycle_events_jsonl(events_file):
                at = _time(event.timestamp)
                if (event.event_type == "context_pack_built" and at is not None
                        and since <= at < until and event.task_id):
                    packs.append(event)
        task_ids = _task_ids(db, since, until)
        coverage: dict[str, bool] = {}
        for event in packs:
            middle = event.extra.get("middle_bytes_by_kind") if isinstance(event.extra, Mapping) else None
            if not isinstance(middle, Mapping):
                middle = event.extra.get("tokens_by_category") if isinstance(event.extra, Mapping) else None
            if isinstance(middle, Mapping):
                coverage[event.task_id] = any(_nonnegative(middle.get(kind)) not in (None, 0)
                                              for kind in ("lineage", "failure_pattern"))
        known_tasks = task_ids & coverage.keys() if task_ids is not None else set()
        result.append(_row(bot, "M3c", str(events_file) + ";" + db_source + ":tasks",
                           len(known_tasks),
                           sum(coverage[task_id] for task_id in known_tasks) / len(task_ids)
                           if task_ids and len(known_tasks) == len(task_ids) else None,
                           "not_all_crew_tasks_have_pack_middle_kind_receipts"))

        sizes = [_nonnegative(x.get("bytes_emitted")) for x in hooks]
        sizes = [int(x) for x in sizes if x is not None]
        result.append(_row(bot, "M4", hook_source, len(sizes),
                           {"max_bytes": max(sizes), "over_8900_count": sum(x > 8900 for x in sizes)}
                           if sizes else None, "no_measured_hook_bytes_in_window"))
        trims = [(_nonnegative(x.get("trimmed_bytes")), _nonnegative(x.get("standing_trimmed")))
                 for x in hooks]
        trims = [(int(a), int(b)) for a, b in trims if a is not None and b is not None]
        result.append(_row(bot, "M5", hook_source, len(trims),
                           {"trimmed_bytes_total": sum(a for a, _ in trims),
                            "standing_trimmed_total": sum(b for _, b in trims)}
                           if trims else None, "no_complete_trim_fields_in_window"))
        latencies = [_nonnegative(x.get("retrieve_ranked_latency_ms", x.get("latency_ms")))
                     for x in hooks if x.get("mode") == "ranked"]
        latencies = [x for x in latencies if x is not None]
        result.append(_row(bot, "M6", hook_source, len(latencies),
                           _percentile95(latencies) if latencies else None,
                           "no_retrieve_ranked_latency_receipts_in_window"))
        result.append(_served_item_metric(bot, events_file, memory_path, since, until))
        result.append(_review_followup_metric(bot, db, since, until))
    return result


def format_table(rows: list[dict[str, object]]) -> str:
    """Human-readable hourly report table, with visible reasons for nulls."""
    lines = ["bot | metric | n | value | reason", "--- | --- | ---: | --- | ---"]
    for row in rows:
        value = row["value"]
        shown = json.dumps(value, sort_keys=True) if value is not None else "null"
        lines.append(f"{row['bot']} | {row['metric']} | {row['n']} | {shown} | {row.get('reason', '')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only B1.3 M1–M8 hourly metrics")
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--crew-root", type=Path, required=True)
    parser.add_argument("--hook-log", type=Path, required=True)
    parser.add_argument("--bot", action="append", dest="bots",
                        help="limit output to a named bot (repeatable)")
    parser.add_argument("--eval-path", type=Path)
    parser.add_argument("--memory-db", type=Path,
                        help="ADR-001 memory DB to snapshot read-only for frozen EVAL replay")
    parser.add_argument("--transcript-root", type=Path,
                        help="Claude transcript project directory for bot-session M1")
    parser.add_argument("--lifecycle-log", type=Path,
                        default=None,
                        help="Quota compact/RENEW lifecycle JSONL for bot-session M1")
    parser.add_argument("--table", action="store_true")
    args = parser.parse_args(argv)
    since, until = _time(args.since), _time(args.until)
    if since is None or until is None:
        parser.error("--since and --until must be epochs or ISO-8601 timestamps")
    rows = compute_metrics(since, until, args.crew_root, args.hook_log,
                           bots=args.bots,
                           eval_path=args.eval_path, memory_db=args.memory_db,
                           transcript_root=args.transcript_root,
                           lifecycle_log=args.lifecycle_log)
    print(format_table(rows) if args.table else json.dumps(rows, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
