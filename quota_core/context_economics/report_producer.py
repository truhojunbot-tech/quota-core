"""Deterministic, read-only producer for organic shadow-policy artifacts.

The producer is deliberately a reporting boundary: it reads runtime
``task_attribution`` tables through SQLite ``mode=ro`` and writes only the
caller-selected JSON artifact.  It never changes a runtime database or
enforces a recommendation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .policy import POLICY_CONTRACT_VERSION, policy_contract_schema
from .quality_evidence import (
    derive_progress_evidence, derive_quality_evidence, ingest_attribution_quality_evidence,
)
from dataclasses import replace
from .schema import TaskEconomicsRecord, parse_flexible_timestamp
from .shadow_report import shadow_comparison_report
from .sqlite_attribution import read_task_attribution_sqlite

REPORT_SCHEMA_VERSION = "1.0"
REPORT_CONTRACT_ID = "https://quota-core.dev/contracts/organic-shadow-report/1.0"
NOT_COVERED = "NOT_COVERED"
COVERED = "COVERED"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOGGER = logging.getLogger(__name__)

# The outer report is intentionally small and stable.  Its policy-decision
# payload is pinned by the separately versioned policy contract it cites.
REPORT_CONTRACT_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": REPORT_CONTRACT_ID,
    "type": "object",
    "required": [
        "report_schema_version", "report_contract_id", "mode", "policy_version",
        "policy_contract", "decision_count", "watermark", "decisions",
    ],
    "properties": {
        "report_schema_version": {"const": REPORT_SCHEMA_VERSION},
        "report_contract_id": {"const": REPORT_CONTRACT_ID},
        "mode": {"const": "shadow"},
        "policy_version": {"type": "string"},
        "policy_contract": {
            "type": "object",
            "required": ["id", "sha256"],
            "properties": {
                "id": {"type": "string"},
                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
            "additionalProperties": False,
        },
        "decision_count": {"type": "integer", "minimum": 0},
        "watermark": {
            "type": "object",
            "required": ["created_at", "task_ids_at_created_at", "null_created_at_rechecked"],
            "properties": {
                "created_at": {"type": ["integer", "null"]},
                "task_ids_at_created_at": {"type": "array", "items": {"type": "string"}},
                "null_created_at_rechecked": {"const": True},
            },
            "additionalProperties": False,
        },
        "decisions": {"type": "object", "additionalProperties": {"type": "object"}},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True)
class _SourcedRecord:
    record: TaskEconomicsRecord
    created_at: int | None
    source_key: str


def policy_contract_sha256() -> str:
    """Return the canonical hash of the policy schema this report cites."""
    encoded = json.dumps(
        policy_contract_schema(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def report_contract_schema() -> dict[str, object]:
    """Return a copyable descriptor of the outer rolling-report contract."""
    # JSON round-trip keeps this dependency-free and prevents caller mutation.
    return json.loads(json.dumps(REPORT_CONTRACT_SCHEMA))


def _readonly_created_at_by_task(
    db_path: str | Path, table: str
) -> dict[str, int | None]:
    """Read task creation watermarks without ever opening the source writable."""
    path = Path(db_path).expanduser()
    if not path.is_file() or not _SAFE_IDENTIFIER.fullmatch(table):
        return {}
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        conn.row_factory = sqlite3.Row
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if "task_id" not in columns:
            return {}
        selected = "task_id, created_at" if "created_at" in columns else "task_id"
        rows = conn.execute(
            f'SELECT {selected} FROM "{table}" ORDER BY task_id'
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    result: dict[str, int | None] = {}
    for row in rows:
        task_id = row["task_id"]
        if not isinstance(task_id, str) or not task_id:
            continue
        raw = row["created_at"] if "created_at" in row.keys() else None
        try:
            value = parse_flexible_timestamp(raw)
        except (TypeError, ValueError):
            value = None
        result[task_id] = value
    return result


def read_organic_task_records(
    db_paths: Iterable[str | Path], table: str = "task_attribution"
) -> dict[str, _SourcedRecord]:
    """Read and deterministically de-duplicate public attribution rows.

    If two input databases contain the same task ID, the later ``created_at``
    wins.  Ties use the normalized input path only as an internal tie-breaker;
    source paths are never emitted in the public report.  A missing/NULL
    timestamp remains unknown and is re-evaluated on each rolling run rather
    than being treated as a zero timestamp.
    """
    selected: dict[str, _SourcedRecord] = {}
    unique_paths = sorted({str(Path(path).expanduser()) for path in db_paths})
    for path in unique_paths:
        created = _readonly_created_at_by_task(path, table)
        source_records = read_task_attribution_sqlite(path, table)
        _LOGGER.info("read %d attribution record(s) from %s", len(source_records), path)
        if not source_records:
            _LOGGER.warning("no readable attribution records from %s", path)
        for record in source_records:
            candidate = _SourcedRecord(record, created.get(record.task_id), path)
            existing = selected.get(record.task_id)
            candidate_key = (
                candidate.created_at is not None,
                candidate.created_at if candidate.created_at is not None else -1,
                candidate.source_key,
            )
            existing_key = (
                existing is not None and existing.created_at is not None,
                existing.created_at if existing and existing.created_at is not None else -1,
                existing.source_key if existing else "",
            )
            if existing is None or candidate_key > existing_key:
                selected[record.task_id] = candidate
    return selected


def _artifact_evidence(
    db_path: str, task_ids: list[str]
) -> tuple[dict[str, object], dict[str, dict[str, str]]]:
    """Derive only structured evidence available in each source database."""
    derived = derive_quality_evidence(db_path, task_ids)
    ingested = ingest_attribution_quality_evidence(db_path, task_ids)
    for task_id, incoming in ingested.items():
        current = derived.get(task_id)
        if current is None:
            derived[task_id] = incoming
            continue
        derived[task_id] = type(current)(
            task_id,
            replace(
                current.evidence,
                safety_or_live_change=incoming.evidence.safety_or_live_change,
                broad_architecture_change=incoming.evidence.broad_architecture_change,
                bounded_routine_fix=incoming.evidence.bounded_routine_fix,
                human_gate_required=incoming.evidence.human_gate_required,
                required_context_recalled=incoming.evidence.required_context_recalled,
                recall_not_applicable=incoming.evidence.recall_not_applicable,
            ),
            {**current.provenance, **incoming.provenance},
        )
    # Progress / unchanged-state from the review lineage the producer recorded.
    for task_id, progress in derive_progress_evidence(db_path, task_ids).items():
        current = derived.get(task_id)
        if current is None:
            derived[task_id] = progress
            continue
        derived[task_id] = type(current)(
            task_id,
            replace(
                current.evidence,
                new_evidence_or_progress=progress.evidence.new_evidence_or_progress,
                repeated_unchanged_state=progress.evidence.repeated_unchanged_state,
            ),
            {**current.provenance, **progress.provenance},
        )
    return (
        {task_id: item.evidence for task_id, item in derived.items()},
        {task_id: item.provenance for task_id, item in derived.items()},
    )


def _evidence_fingerprint(evidence: object, provenance: Mapping[str, str]) -> str:
    """Fingerprint all derived evidence so later facts refresh old artifacts."""
    values = asdict(evidence) if hasattr(evidence, "__dataclass_fields__") else {}
    encoded = json.dumps(
        {"evidence": values, "provenance": dict(provenance)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _existing_decisions(existing_report: Mapping[str, object] | None) -> dict[str, dict[str, object]]:
    if not isinstance(existing_report, Mapping):
        return {}
    raw = existing_report.get("decisions")
    if not isinstance(raw, Mapping):
        return {}
    return {
        task_id: dict(artifact)
        for task_id, artifact in raw.items()
        if isinstance(task_id, str) and isinstance(artifact, Mapping)
    }


def _prior_watermark(existing_report: Mapping[str, object] | None) -> int | None:
    if not isinstance(existing_report, Mapping):
        return None
    watermark = existing_report.get("watermark")
    if not isinstance(watermark, Mapping):
        return None
    value = watermark.get("created_at")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_new_since_watermark(
    task_id: str,
    item: _SourcedRecord,
    decisions: Mapping[str, object],
    watermark: int | None,
    evidence_fingerprint: str,
) -> bool:
    """Refresh new attribution rows *or* later evidence for an existing task."""
    if task_id not in decisions:
        return True
    existing = decisions[task_id]
    if (
        not isinstance(existing, Mapping)
        or not isinstance(existing.get("risk_facts"), Mapping)
        or existing.get("evidence_fingerprint") != evidence_fingerprint
    ):
        return True
    if item.created_at is None or watermark is None:
        return True
    return item.created_at >= watermark


def produce_shadow_report(
    db_paths: Iterable[str | Path],
    existing_report: Mapping[str, object] | None = None,
    table: str = "task_attribution",
) -> dict[str, object]:
    """Build an idempotent rolling, recommendation-only report keyed by ID.

    The producer upserts new records, records at the prior watermark or later,
    and existing records whose derived evidence changed. The inclusive
    comparison makes tied timestamps safe, while rows with an unknown timestamp
    are re-read on every run. This deliberately trades a small amount of
    read-only work for never silently missing an organic task or a later review
    verdict because its producer did not record a task creation timestamp.
    """
    records = read_organic_task_records(db_paths, table)
    decisions = _existing_decisions(existing_report)
    watermark = _prior_watermark(existing_report)

    by_source: dict[str, list[_SourcedRecord]] = {}
    evidence_by_source: dict[str, tuple[dict[str, object], dict[str, dict[str, str]]]] = {}
    for source in sorted({item.source_key for item in records.values()}):
        source_items = [item for item in records.values() if item.source_key == source]
        evidence_by_source[source] = _artifact_evidence(
            source, sorted(item.record.task_id for item in source_items)
        )
        evidence, provenance = evidence_by_source[source]
        for item in source_items:
            task_id = item.record.task_id
            fingerprint = _evidence_fingerprint(evidence[task_id], provenance[task_id])
            if _is_new_since_watermark(task_id, item, decisions, watermark, fingerprint):
                by_source.setdefault(source, []).append(item)

    for source, items in sorted(by_source.items()):
        evidence, provenance = evidence_by_source[source]
        report = shadow_comparison_report(
            [item.record for item in sorted(items, key=lambda value: value.record.task_id)],
            evidence_by_task=evidence,
            evidence_provenance=provenance,
        )
        for artifact in report["artifacts"]:
            if not isinstance(artifact, dict):
                raise RuntimeError("shadow report emitted a non-object artifact")
            task_id = artifact.get("task_id")
            if not isinstance(task_id, str):
                raise RuntimeError("shadow report emitted an artifact without a task ID")
            artifact["created_at"] = records[task_id].created_at
            artifact["evidence_fingerprint"] = _evidence_fingerprint(
                evidence[task_id], provenance[task_id]
            )
            decisions[task_id] = artifact

    known_times = [item.created_at for item in records.values() if item.created_at is not None]
    current_watermark = max(known_times) if known_times else None
    tied_task_ids = sorted(
        task_id for task_id, item in records.items()
        if item.created_at == current_watermark and current_watermark is not None
    )
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "report_contract_id": REPORT_CONTRACT_ID,
        "mode": "shadow",
        "policy_version": POLICY_CONTRACT_VERSION,
        "policy_contract": {
            "id": policy_contract_schema()["$id"],
            "sha256": policy_contract_sha256(),
        },
        "decision_count": len(decisions),
        "watermark": {
            "created_at": current_watermark,
            "task_ids_at_created_at": tied_task_ids,
            "null_created_at_rechecked": True,
        },
        "decisions": {task_id: decisions[task_id] for task_id in sorted(decisions)},
    }


def decision_for(report: Mapping[str, object], task_id: str) -> dict[str, object]:
    """Return a decision artifact, or an explicit non-silent coverage result."""
    decisions = report.get("decisions") if isinstance(report, Mapping) else None
    artifact = decisions.get(task_id) if isinstance(decisions, Mapping) else None
    if isinstance(artifact, Mapping):
        return {"status": COVERED, "task_id": task_id, "artifact": dict(artifact)}
    return {"status": NOT_COVERED, "task_id": task_id}


def _load_existing(path: Path) -> Mapping[str, object] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, Mapping) else None


def _db_paths_from_args(values: Sequence[str], list_path: str | None) -> list[str]:
    paths = list(values)
    if list_path:
        try:
            configured = json.loads(Path(list_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            _LOGGER.warning("could not read --db-list %s: %s", list_path, error)
            configured = []
        if isinstance(configured, list):
            paths.extend(item for item in configured if isinstance(item, str))
        else:
            _LOGGER.warning("--db-list %s must contain a JSON array of paths", list_path)
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    """Run the on-demand/rolling producer; ``--out`` is the rolling state."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", action="append", default=[], help="read-only SQLite attribution DB (repeatable)")
    parser.add_argument("--db-list", help="JSON list of SQLite attribution DB paths")
    parser.add_argument("--table", default="task_attribution")
    parser.add_argument("--out", required=True, help="report JSON; prior output is the rolling state")
    parser.add_argument(
        "--contract-out",
        help="also emit the reader-compatible policy contract atomically to this path",
    )
    parser.add_argument("--lookup-task", help="also print a COVERED/NOT_COVERED lookup result")
    args = parser.parse_args(argv)
    db_paths = _db_paths_from_args(args.db, args.db_list)
    if not db_paths:
        parser.error("at least one --db or --db-list entry is required")
    out = Path(args.out)
    report = produce_shadow_report(db_paths, _load_existing(out), args.table)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.contract_out:
        # Imported here so the rolling producer keeps no import-time dependency
        # on the emitter, which in turn reads this module.
        from .contract_emitter import emit_contract

        _, contract_sha = emit_contract(args.contract_out, db_paths, args.table)
        print(json.dumps(
            {"contract_out": str(args.contract_out), "contract_sha256": contract_sha},
            sort_keys=True,
        ))
    if args.lookup_task:
        print(json.dumps(decision_for(report, args.lookup_task), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via module CLI
    raise SystemExit(main())
