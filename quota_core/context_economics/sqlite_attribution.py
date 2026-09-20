"""Read-only loader for generic task-attribution SQLite telemetry."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .correlate import correlate_task_economics
from .schema import TaskEconomicsRecord, attribution_from_dict

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def read_task_attribution_sqlite(
    db_path: str | Path,
    table: str = "task_attribution",
) -> list[TaskEconomicsRecord]:
    """Return normalized records from a task-attribution table, read-only.

    The table is expected to expose a ``task_id`` and may expose any public
    attribution columns, including the post-#334 token/cache fields. Missing
    columns and SQL ``NULL`` values remain unknown; this loader never derives
    a zero or a token total. A missing, unreadable, or incompatible database
    yields no records so an optional reporting input cannot disrupt callers.
    """

    path = Path(db_path).expanduser()
    if not path.is_file() or not _SAFE_IDENTIFIER.fullmatch(table):
        return []
    try:
        # `mode=ro` is intentional: the source can be a live runtime database.
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        try:
            columns = {
                row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')
            }
            if "task_id" not in columns:
                return []
            rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY task_id').fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()

    attributions = []
    for row in rows:
        raw = dict(row)
        if not raw.get("task_id"):
            continue
        # `status` is a common storage spelling; preserve an explicit outcome
        # when supplied because it is richer and may carry a failure reason.
        if "outcome" not in raw and "status" in raw:
            raw["outcome"] = raw["status"]
        raw.setdefault("runtime", "task_attribution_sqlite")
        try:
            attributions.append(attribution_from_dict(raw))
        except (TypeError, ValueError):
            continue
    return correlate_task_economics(attributions, [])
