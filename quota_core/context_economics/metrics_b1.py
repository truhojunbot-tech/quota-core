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
        runtime = importlib.import_module("agent_crew.memory_runtime")
        if Path(hybrid.__file__).resolve() != (package_root / "agent_crew" / "memory_hybrid.py").resolve():
            raise ImportError(f"agent_crew_src_mismatch:LEMMALOG_AGENT_CREW_SRC={src}")
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
                    memory_db: str | Path | None = None) -> list[dict[str, object]]:
    """Compute one source-attributed row per B1.3 metric per bot.

    The window is half-open [since, until). A missing producer or pre-A2
    retrieval path yields a reasoned null. This never writes a source.
    """
    if until <= since:
        raise ValueError("until must be greater than since")
    root, hook_path = Path(crew_root).expanduser(), Path(hook_log).expanduser()
    hook_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in _iter_jsonl(hook_path):
        bot = event.get("bot")
        at = _time(event.get("ts"))
        if (isinstance(bot, str) and bot and at is not None and since <= at < until
                and event.get("identity_source") != "headless_oneshot"):
            hook_rows[bot].append(event)
    bot_names = set(bots or []) | set(hook_rows)
    if root.is_dir():
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
            else:
                try:
                    eval_set = json.loads(contents)
                    db_path = Path(memory_db).expanduser() if memory_db is not None else Path.home() / ".agent_crew/memory/adr001_memory.db"
                    eval_scores = _replay_frozen_eval(eval_set, db_path)
                    eval_source = f"{path};{db_path}"
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
        # Quota's currently deployed quota_cache.json files record pace and
        # limits, not per-session cache-read/write/input token components.
        # Leave this sibling axis unknown until a real usage cache supplies it.
        m1["bot_session_value"] = None
        m1["bot_session_reason"] = "quota_usage_cache_lacks_session_token_components"
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
        result.append(_row(bot, "M7", None, None, None,
                           "not_measured:served_item_join_not_implemented"))
        result.append(_row(bot, "M8", None, None, None,
                           "not_measured:review_followup_window_not_implemented"))
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
    parser.add_argument("--crew-root", type=Path, default=Path.home() / ".agent_crew")
    parser.add_argument("--hook-log", type=Path, required=True)
    parser.add_argument("--eval-path", type=Path)
    parser.add_argument("--memory-db", type=Path,
                        help="ADR-001 memory DB to snapshot read-only for frozen EVAL replay")
    parser.add_argument("--table", action="store_true")
    args = parser.parse_args(argv)
    since, until = _time(args.since), _time(args.until)
    if since is None or until is None:
        parser.error("--since and --until must be epochs or ISO-8601 timestamps")
    rows = compute_metrics(since, until, args.crew_root, args.hook_log,
                           eval_path=args.eval_path, memory_db=args.memory_db)
    print(format_table(rows) if args.table else json.dumps(rows, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
