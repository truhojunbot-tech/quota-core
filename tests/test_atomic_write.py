"""quota-core#115: outputs other processes read are replaced atomically.

A reader must see either the previous artifact or the new one, never a
truncated file.  Each call site must also keep producing the exact bytes the
old ``Path.write_text`` call produced.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quota_core import cli
from quota_core.atomic_io import atomic_write_text
from quota_core.context_economics import report_producer
from quota_core.dashboard.renderer import render_page
from quota_core.snapshot import snapshot_from_dict


def _interrupt_fsync(_fd: int) -> None:
    raise OSError("simulated crash before the new content is durable")


def _shadow_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE task_attribution (
                task_id TEXT PRIMARY KEY, created_at REAL, outcome TEXT, status TEXT,
                task_type TEXT, provider TEXT, model TEXT, uncached_input_tokens INTEGER,
                cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                output_tokens INTEGER, reasoning_tokens INTEGER
            )"""
        )
        conn.execute(
            "INSERT INTO task_attribution VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("task-a", 100, "completed", "completed", "implement", "example-provider",
             "example-model", 10, 0, 0, 5, 0),
        )


class AtomicWriteHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def test_interrupted_write_keeps_old_content_and_leaves_no_temp(self):
        target = self.dir / "artifact.json"
        target.write_text("old\n")
        with patch("os.fsync", _interrupt_fsync):
            with self.assertRaises(OSError):
                atomic_write_text(target, "new\n")
        self.assertEqual(target.read_text(), "old\n")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["artifact.json"])

    def test_encode_failure_mid_write_keeps_old_content(self):
        # A lone surrogate cannot be encoded: Path.write_text would already
        # have truncated the target when this raises.
        legacy = self.dir / "legacy.json"
        legacy.write_text("old\n", encoding="utf-8")
        with self.assertRaises(UnicodeEncodeError):
            legacy.write_text("x" * 100_000 + "\ud800", encoding="utf-8")
        self.assertEqual(legacy.read_text(encoding="utf-8"), "")

        target = self.dir / "artifact.json"
        target.write_text("old\n", encoding="utf-8")
        with self.assertRaises(UnicodeEncodeError):
            atomic_write_text(target, "x" * 100_000 + "\ud800", encoding="utf-8")
        self.assertEqual(target.read_text(encoding="utf-8"), "old\n")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["artifact.json", "legacy.json"])

    def test_interrupted_first_write_creates_nothing(self):
        target = self.dir / "fresh.json"
        with patch("os.fsync", _interrupt_fsync):
            with self.assertRaises(OSError):
                atomic_write_text(target, "new\n")
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_bytes_match_path_write_text_for_encoding_and_newline(self):
        text = "line één\nline two\r\nend"
        for kwargs in ({}, {"encoding": "utf-8"}, {"encoding": "latin-1"},
                       {"encoding": "utf-8", "newline": "\r\n"}, {"newline": ""}):
            with self.subTest(**kwargs):
                reference = self.dir / "reference.txt"
                actual = self.dir / "actual.txt"
                reference.write_text(text, **kwargs)
                actual.write_text("previous content that is longer than the new one" * 100)
                atomic_write_text(actual, text, **kwargs)
                self.assertEqual(actual.read_bytes(), reference.read_bytes())

    def test_new_file_mode_matches_path_write_text(self):
        reference = self.dir / "reference.txt"
        actual = self.dir / "actual.txt"
        reference.write_text("x")
        atomic_write_text(actual, "x")
        self.assertEqual(stat.S_IMODE(actual.stat().st_mode), stat.S_IMODE(reference.stat().st_mode))

    def test_existing_file_mode_is_preserved(self):
        target = self.dir / "shared.html"
        target.write_text("old")
        os.chmod(target, 0o644)
        atomic_write_text(target, "new")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        os.chmod(target, 0o600)
        atomic_write_text(target, "newer")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_symlink_target_is_written_like_path_write_text(self):
        real = self.dir / "real.json"
        link = self.dir / "link.json"
        real.write_text("old")
        link.symlink_to(real)
        atomic_write_text(link, "new")
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), "new")


class CallSiteTests(unittest.TestCase):
    """Every #115 call site: identical bytes, and old content survives a crash."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def _assert_bytes(self, target: Path, expected_text: str, **kwargs: str) -> None:
        reference = self.dir / f"reference-{target.name}"
        reference.write_text(expected_text, **kwargs)
        self.assertEqual(target.read_bytes(), reference.read_bytes())

    def _assert_crash_keeps_old(self, target: Path, run) -> None:
        target.write_text("previous artifact\n")
        before = set(self.dir.iterdir())
        with patch("os.fsync", _interrupt_fsync):
            with self.assertRaises(OSError):
                run()
        self.assertEqual(target.read_text(), "previous artifact\n")
        self.assertEqual(set(self.dir.iterdir()), before)

    # cli.py:182
    def test_write_demo(self):
        target = self.dir / "demo.html"
        cli.write_demo(target)
        fixture = Path(cli.__file__).resolve().parent / "fixtures" / "normalized" / "claude-live.json"
        expected = render_page([snapshot_from_dict(json.loads(fixture.read_text()))], title="quota_core demo")
        self._assert_bytes(target, expected)
        self._assert_crash_keeps_old(target, lambda: cli.write_demo(target))

    def _config(self) -> Path:
        projects = self.dir / "projects"
        projects.mkdir(exist_ok=True)
        config = self.dir / "config.toml"
        config.write_text(
            f'[providers.claude]\nenabled = true\n\n[providers.claude.paths]\nprojects_dir = "{projects}"\n'
            "\n[providers.codex]\nenabled = false\n\n[providers.gemini]\nenabled = false\n"
        )
        return config

    # cli.py:197
    def test_write_scan(self):
        config = self._config()
        target = self.dir / "snapshot.json"
        with patch.object(time, "time", return_value=1_700_000_000):
            cli.write_scan(config, target)
            loaded = cli.load_config(config)
            expected = json.dumps({
                "snapshots": [cli.snapshot_to_dict(s) for s in cli.scan_config(loaded)],
                "warnings": list(cli.validate_config(loaded)),
            }, indent=2)
            self._assert_bytes(target, expected)
            self._assert_crash_keeps_old(target, lambda: cli.write_scan(config, target))

    # cli.py:238
    def test_write_dashboard(self):
        snapshot = self.dir / "snapshot.json"
        with patch.object(time, "time", return_value=1_700_000_000):
            cli.write_scan(self._config(), snapshot)
        target = self.dir / "dashboard.html"
        cli.write_dashboard(snapshot, target)
        expected = render_page(cli.load_snapshot_file(snapshot), title="quota_core dashboard")
        self._assert_bytes(target, expected)
        self._assert_crash_keeps_old(target, lambda: cli.write_dashboard(snapshot, target))

    # cli.py:285 -- ensure_ascii=False, so non-ASCII must keep the old encoding.
    def test_verify_session_parity_output(self):
        root = self.dir / "transcripts-é"
        root.mkdir()
        official = {"totals": {"total_tokens": 0}, "sessions": 0}
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(official), stderr="")
        target = self.dir / "parity.json"

        def run():
            return cli.verify_session_parity(
                official_analyzer=self.dir / "analyze.mjs", transcript_root=root,
                since="all", max_file_bytes=1_000_000, output=target,
            )

        with patch.object(cli.subprocess, "run", return_value=completed):
            summary = run()
            self._assert_bytes(target, json.dumps(summary, ensure_ascii=False, indent=2))
            self._assert_crash_keeps_old(target, run)

    # cli.py:165
    def test_context_economics_shadow_report_output(self):
        db = self.dir / "attribution.sqlite"
        _shadow_db(db)
        target = self.dir / "shadow.json"
        argv = ["context-economics-shadow-report", "--database", str(db), "--output", str(target)]
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(cli.main(argv), 0)
        self._assert_bytes(target, stdout.getvalue())
        with redirect_stdout(io.StringIO()):
            self._assert_crash_keeps_old(target, lambda: cli.main(argv))

    # report_producer.py:634
    def test_report_producer_out(self):
        db = self.dir / "attribution.sqlite"
        _shadow_db(db)
        target = self.dir / "organic.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(report_producer.main(["--db", str(db), "--out", str(target)]), 0)
        report = json.loads(target.read_text(encoding="utf-8"))
        self._assert_bytes(target, json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        # The producer reads --out back as rolling state, so a corrupt prior
        # file is not a fair "old content"; restore the valid report first.
        previous = target.read_bytes()
        before = set(self.dir.iterdir())
        with patch("os.fsync", _interrupt_fsync), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                report_producer.main(["--db", str(db), "--out", str(target)])
        self.assertEqual(target.read_bytes(), previous)
        self.assertEqual(set(self.dir.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
