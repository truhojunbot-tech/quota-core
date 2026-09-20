"""Read-only loader for generic task-attribution SQLite telemetry."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .correlate import correlate_task_economics
from .schema import TaskEconomicsRecord, attribution_from_dict

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TERMINAL_STATUS_PREFIXES = {
    "success", "completed", "done", "ok",
    "failed", "error", "cancelled", "canceled",
}


def _terminal_outcome_from_status(status: object) -> str | None:
    """Return a terminal status string, or ``None`` for unfinished state.

    A table's ``status`` is often operational state rather than a result. In
    particular, dispatcher-timeout and human/blocking states are unresolved:
    reporting them as an ``unknown`` terminal outcome would cause failure-rate
    analytics to count work that is not complete. Preserve only status values
    whose normalized prefix is an explicit success/failure terminal state.
    """

    if not isinstance(status, str) or not status.strip():
        return None
    prefix = status.split(":", 1)[0].strip().lower()
    return status if prefix in _TERMINAL_STATUS_PREFIXES else None


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
        # `status` is a common storage spelling. An explicit, non-empty
        # outcome wins; a null/empty outcome can use only a *terminal* status.
        # Unfinished status values deliberately remain outcome=None.
        if raw.get("outcome") in (None, "") and raw.get("status"):
            raw["outcome"] = _terminal_outcome_from_status(raw["status"])
        raw.setdefault("runtime", "task_attribution_sqlite")
        try:
            attributions.append(attribution_from_dict(raw))
        except (TypeError, ValueError):
            continue
    return correlate_task_economics(attributions, [])
