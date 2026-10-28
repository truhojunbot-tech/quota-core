"""Hourly B1.3 snapshot writer with a settle-window recompute (issue #99).

Each run writes ``<YYYYMMDDTHH>Z.json`` and ``.txt`` for the last complete UTC
hour H, and recomputes H-1..H-``recompute_hours`` in place. Receipts created
inside an hour can complete their ``economics_json`` after that hour's first
snapshot; the recompute lets those late receipts land without a new store or
schedule. Every file records ``computed_at``; writes are temp file + rename.

All source paths are caller-supplied. Sources are only read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from .metrics_b1 import compute_metrics, format_table

RECOMPUTE_HOURS = 3
RETENTION = timedelta(days=14)
HOUR_FILE = re.compile(r"^(\d{8}T\d{2})Z\.(json|txt)$")


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_windows(now: datetime, recompute_hours: int = RECOMPUTE_HOURS
                 ) -> list[tuple[datetime, datetime]]:
    """Last complete UTC hour first, then the ``recompute_hours`` before it."""
    until = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return [(until - timedelta(hours=back + 1), until - timedelta(hours=back))
            for back in range(recompute_hours + 1)]


@contextmanager
def hook_input(current: Path, scratch_dir: Path) -> Iterator[Path]:
    """Join the rotated (``<path>.1``) and current segments in chronological order."""
    rotated = Path(str(current) + ".1")
    segments = [path for path in (rotated, current) if path.is_file()]
    if not segments or segments == [current]:
        yield current  # A missing log keeps metrics_b1's normal missing-source reason.
        return
    with tempfile.NamedTemporaryFile(dir=scratch_dir, prefix=".hook-", suffix=".tmp",
                                     delete=False) as temp:
        joined = Path(temp.name)
        try:
            for index, path in enumerate(segments):
                last_byte = b""
                with path.open("rb") as source:
                    while block := source.read(1024 * 1024):
                        temp.write(block)
                        last_byte = block[-1:]
                if index < len(segments) - 1 and last_byte not in (b"", b"\n"):
                    temp.write(b"\n")
            temp.flush()
            yield joined
        finally:
            joined.unlink(missing_ok=True)


def _write(path: Path, body: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".metrics-", suffix=".tmp", delete=False) as temp:
        staged = Path(temp.name)
        try:
            temp.write(body)
            temp.flush()
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)


def render(rows: list[dict[str, object]], computed_at: str) -> tuple[str, str]:
    """JSON and table bodies; ``computed_at`` is the only run-dependent field."""
    stamped = [{**row, "computed_at": computed_at} for row in rows]
    return (json.dumps(stamped, sort_keys=True) + "\n",
            f"computed_at: {computed_at}\n{format_table(rows)}\n")


def _prune(output_dir: Path, cutoff: datetime) -> None:
    for path in output_dir.iterdir():
        match = HOUR_FILE.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        try:
            hour = datetime.strptime(match.group(1), "%Y%m%dT%H").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if hour < cutoff:
            path.unlink()


def run(now: datetime, output_dir: str | Path, crew_root: str | Path, hook_log: str | Path,
        *, memory_db: str | Path | None = None, transcript_root: str | Path | None = None,
        lifecycle_log: str | Path | None = None,
        recompute_hours: int = RECOMPUTE_HOURS,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> int:
    """Write H and rewrite H-1..H-n. Returns 0, or 1 if any window failed.

    A failed recompute keeps the earlier snapshot; only a window with no file
    yet gets an error file, so a transient failure never erases a good table.
    """
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    windows = hour_windows(now, recompute_hours)
    status = 0
    with hook_input(Path(hook_log).expanduser().resolve(), output) as hooks:
        for since, until in windows:
            stem = since.strftime("%Y%m%dT%H") + "Z"
            json_path, table_path = output / f"{stem}.json", output / f"{stem}.txt"
            try:
                rows = compute_metrics(since.timestamp(), until.timestamp(), crew_root, hooks,
                                       memory_db=memory_db, transcript_root=transcript_root,
                                       lifecycle_log=lifecycle_log)
                computed_at = _stamp(clock())
                body, table = render(rows, computed_at)
                _write(json_path, body)
                _write(table_path, table)
            except Exception as error:  # one bad window must not cost the others
                status = 1
                reason = str(error) or type(error).__name__
                print(f"metrics_b1_hourly {stem}: {reason}", file=sys.stderr)
                if json_path.exists():
                    continue
                computed_at = _stamp(clock())
                _write(json_path, json.dumps([{
                    "bot": None, "metric": "error", "value": None, "reason": reason,
                    "since": _stamp(since), "until": _stamp(until),
                    "computed_at": computed_at}], sort_keys=True) + "\n")
                _write(table_path, f"computed_at: {computed_at}\nmetrics_b1 error: {reason}\n")
    _prune(output, windows[0][0] - RETENTION)
    return status


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write the last complete UTC hour and recompute the hours before it")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--crew-root", type=Path, required=True)
    parser.add_argument("--hook-log", type=Path, required=True,
                        help="current hook JSONL; a rotated '<path>.1' is joined before it")
    parser.add_argument("--memory-db", type=Path)
    parser.add_argument("--transcript-root", type=Path)
    parser.add_argument("--lifecycle-log", type=Path)
    parser.add_argument("--recompute-hours", type=int, default=RECOMPUTE_HOURS)
    args = parser.parse_args(argv)
    if args.recompute_hours < 0:
        parser.error("--recompute-hours must be >= 0")
    started = time.monotonic()
    status = run(now or datetime.now(timezone.utc), args.output_dir, args.crew_root,
                 args.hook_log, memory_db=args.memory_db, transcript_root=args.transcript_root,
                 lifecycle_log=args.lifecycle_log, recompute_hours=args.recompute_hours)
    print(f"metrics_b1_hourly: {args.recompute_hours + 1} windows in "
          f"{time.monotonic() - started:.1f}s", file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
