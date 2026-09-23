"""Atomic emitter turning the organic shadow policy into the reader contract.

This module adds no policy and no metric.  It reads the same organic
attribution rows the rolling report producer already reads, runs the existing
:func:`shadow_policy_report`, attaches a ``provenance`` block, and writes the
result atomically.  Consumers of the policy contract (agent_crew #342) read
only ``contract_version``/``mode``/``decisions`` and ignore unknown keys, so
``provenance`` travels with the artifact without changing its shape.

Everything here stays recommendation-only: the emitter never enforces a
decision, never opens a source database writable, and never mutates runtime
state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .policy import (
    POLICY_CONTRACT_VERSION,
    QualityEvidence,
    policy_contract_schema,
    shadow_policy_report,
)
from .report_producer import (
    REPORT_CONTRACT_ID,
    _artifact_evidence,
    _db_paths_from_args,
    policy_contract_sha256,
    read_organic_task_records,
)

_LOGGER = logging.getLogger(__name__)

#: Reader-visible reason strings, mirrored from the agent_crew #342 adapter.
TASK_DECISION_UNAVAILABLE = "task_decision_unavailable"


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def producer_commit() -> str | None:
    """Return the commit this emitter ran from, or ``None`` when unknown.

    An explicit environment override wins so that a packaged (non-git) install
    can still state its provenance honestly instead of inventing one.
    """
    override = (os.getenv("QUOTA_CORE_PRODUCER_COMMIT") or "").strip()
    if override:
        return override
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def _source_provenance(
    records: Mapping[str, object], db_paths: Iterable[str | Path]
) -> list[dict[str, object]]:
    """Describe each source database by the rows it actually contributed.

    ``sha256_of_rows_or_rowcount`` is the canonical hash of the contributing
    rows; ``row_count`` is carried alongside it so a reviewer can compare two
    artifacts without recomputing anything.  ``watermark`` is the rolling
    ``created_at`` watermark for that source, or ``None`` when every
    contributing row has an unknown timestamp.
    """
    by_source: dict[str, list[tuple[str, object]]] = {}
    for task_id in sorted(records):
        item = records[task_id]
        by_source.setdefault(item.source_key, []).append((task_id, item))

    entries: list[dict[str, object]] = []
    for source in sorted({str(Path(path).expanduser()) for path in db_paths}):
        contributed = by_source.get(source, [])
        rows = [
            {
                "task_id": task_id,
                "created_at": item.created_at,
                "record": asdict(item.record) if is_dataclass(item.record) else None,
            }
            for task_id, item in contributed
        ]
        known_times = [
            item.created_at for _, item in contributed if item.created_at is not None
        ]
        entries.append({
            "path_basename": Path(source).name,
            "sha256_of_rows_or_rowcount": _canonical_sha256(rows),
            "row_count": len(rows),
            "watermark": max(known_times) if known_times else None,
        })
    return entries


def build_contract(
    db_paths: Iterable[str | Path],
    table: str = "task_attribution",
    produced_at: str | None = None,
) -> dict[str, object]:
    """Build the reader-compatible policy contract plus its provenance block.

    The decision payload is exactly what :func:`shadow_policy_report` already
    produces for the same organic records; only the ``provenance`` key is new.
    """
    db_paths = list(db_paths)
    records = read_organic_task_records(db_paths, table)

    evidence_by_task: dict[str, QualityEvidence] = {}
    for source in sorted({item.source_key for item in records.values()}):
        task_ids = sorted(
            task_id for task_id, item in records.items() if item.source_key == source
        )
        evidence, _provenance = _artifact_evidence(source, task_ids)
        evidence_by_task.update(
            {task_id: evidence[task_id] for task_id in task_ids if task_id in evidence}
        )

    ordered = [records[task_id].record for task_id in sorted(records)]
    contract = shadow_policy_report(ordered, evidence_by_task)
    contract["provenance"] = {
        "report_contract_id": REPORT_CONTRACT_ID,
        "policy_contract": {
            "id": policy_contract_schema()["$id"],
            "sha256": policy_contract_sha256(),
        },
        "producer_commit": producer_commit(),
        "produced_at": produced_at or datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "source_dbs": _source_provenance(records, db_paths),
        "decision_count": contract["decision_count"],
    }
    return contract


def write_contract_atomically(path: str | Path, contract: Mapping[str, object]) -> str:
    """Write ``contract`` so readers only ever see a complete artifact.

    The temporary file lives in the destination directory so ``os.replace`` is
    a same-filesystem rename; a failure mid-write leaves the previous artifact
    (or no artifact) intact rather than a truncated one.
    """
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(contract, indent=2, sort_keys=True) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=destination.parent,
        prefix=f".{destination.name}.", suffix=".tmp", delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def emit_contract(
    path: str | Path,
    db_paths: Iterable[str | Path],
    table: str = "task_attribution",
    produced_at: str | None = None,
) -> tuple[dict[str, object], str]:
    """Build and atomically write the contract; return it with its file SHA."""
    contract = build_contract(db_paths, table, produced_at)
    contract_sha = write_contract_atomically(path, contract)
    _LOGGER.info(
        "wrote %d shadow decision(s) to %s (sha256 %s)",
        contract["decision_count"], path, contract_sha,
    )
    return contract, contract_sha


def resolve_decision(contract: Mapping[str, object], task_id: str) -> dict[str, object]:
    """Resolve one task the way the contract's reader does, for verification.

    This mirrors the consumer's matching rule (top-level ``contract_version``
    and ``mode``, then a ``decisions`` entry whose ``task_id`` matches) so the
    emitter can be checked without importing any orchestrator.
    """
    if (
        not isinstance(contract, Mapping)
        or contract.get("contract_version") != POLICY_CONTRACT_VERSION
        or contract.get("mode") != "shadow"
    ):
        return {"decision_source": "baseline", "reason": "policy_unavailable"}
    decisions = contract.get("decisions")
    if not isinstance(decisions, list):
        return {"decision_source": "baseline", "reason": "policy_unavailable"}
    match = next(
        (item for item in decisions
         if isinstance(item, Mapping) and item.get("task_id") == task_id),
        None,
    )
    if match is None:
        return {"decision_source": "baseline", "reason": TASK_DECISION_UNAVAILABLE}
    return {
        "decision_source": "quota_core_contract",
        "policy_version": contract["contract_version"],
        "recommendation": dict(match),
        "reason": "shadow_only",
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Emit the contract for the organic records in the given databases."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", action="append", default=[], help="read-only SQLite attribution DB (repeatable)")
    parser.add_argument("--db-list", help="JSON list of SQLite attribution DB paths")
    parser.add_argument("--table", default="task_attribution")
    parser.add_argument("--out", required=True, help="contract JSON to write atomically")
    parser.add_argument("--lookup-task", help="also print how the reader resolves this task ID")
    args = parser.parse_args(argv)
    db_paths = _db_paths_from_args(args.db, args.db_list)
    if not db_paths:
        parser.error("at least one --db or --db-list entry is required")
    contract, contract_sha = emit_contract(args.out, db_paths, args.table)
    print(json.dumps({"out": str(args.out), "contract_sha256": contract_sha,
                      "decision_count": contract["decision_count"]}, sort_keys=True))
    if args.lookup_task:
        print(json.dumps(resolve_decision(contract, args.lookup_task), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via module CLI
    raise SystemExit(main())
