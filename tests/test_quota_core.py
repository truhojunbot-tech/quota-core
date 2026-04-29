from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from quota_core.adapters.claude import normalize_claude_quota
from quota_core.config import config_from_mapping, load_config, validate_config, write_default_config
from quota_core.runtime import runtime_env
from quota_core.snapshot import AggregateBreakdown, NormalizedSnapshot, SnapshotWindow, snapshot_to_dict, validate_snapshot_dict
from quota_core.cli import scan_config, write_dashboard, write_demo, write_scan
from quota_core.dashboard.renderer import render_page


REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_TARGETS = (
    REPO_ROOT / "quota_core",
    REPO_ROOT / "docs",
    REPO_ROOT / "README.md",
    REPO_ROOT / "pyproject.toml",
)
PRIVATE_IMPORT_ROOTS = {
    "ops_runtime_config",
    "claude_monitor",
    "codex_monitor",
    "gemini_monitor",
    "hourly_report",
    "limit_state_alert",
    "quota_watchdog",
}


class ConfigTests(unittest.TestCase):
    def test_write_and_load_default_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.toml"
            write_default_config(path)
            config = load_config(path)
            self.assertEqual(sorted(config.providers), ["claude", "codex", "gemini"])
            self.assertEqual(config.dashboard.host, "127.0.0.1")
            self.assertEqual(config.dashboard.port, 8088)

    def test_missing_paths_are_warnings(self):
        config = config_from_mapping(
            {
                "providers": {
                    "demo": {
                        "enabled": True,
                        "paths": {"missing": "/definitely/missing/quota-core-test"},
                    }
                }
            }
        )
        warnings = validate_config(config)
        self.assertEqual(len(warnings), 1)
        self.assertIn("does not exist", warnings[0])
        self.assertNotIn("/definitely/missing", warnings[0])


class RuntimeTests(unittest.TestCase):
    def test_runtime_env_matches_configured_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            child = Path(temp_dir) / "child"
            child.mkdir()
            env = runtime_env(str(child), {"demo-bot": (temp_dir,)}, base_env={})
            self.assertEqual(env.get("LLM_USAGE_CLASS"), "runtime")
            self.assertEqual(env.get("BOT_NAME"), "demo-bot")

    def test_runtime_env_preserves_explicit_env(self):
        env = runtime_env("/tmp", {"demo-bot": ("/tmp",)}, base_env={"BOT_NAME": "manual"})
        self.assertEqual(env.get("BOT_NAME"), "manual")
        self.assertIsNone(env.get("LLM_USAGE_CLASS"))


class SnapshotTests(unittest.TestCase):
    def test_claude_legacy_payload_normalizes(self):
        payload = {
            "fetched_at": 1770000000,
            "five_hour": {
                "utilization": 0.5,
                "resets_at": 1770003600,
                "tokens_used": 1000,
                "by_project": {
                    "demo": {
                        "tokens": 800,
                        "share_pct": 80.0,
                        "requests": 2,
                        "models": {"sonnet": 800},
                    }
                },
                "runtime_tokens_used": 200,
                "runtime_requests": 1,
                "runtime_by_project": {
                    "bot": {
                        "tokens": 200,
                        "share_pct": 20.0,
                        "requests": 1,
                        "models": {"sonnet": 200},
                    }
                },
            },
        }
        snapshot = snapshot_to_dict(normalize_claude_quota(payload))
        self.assertEqual(validate_snapshot_dict(snapshot), ())
        self.assertEqual(snapshot["windows"]["five_hour"]["runtime"]["total_tokens"], 200)

    def test_invalid_snapshot_reports_errors(self):
        errors = validate_snapshot_dict({"source": "", "sampled_at": "bad", "windows": []})
        self.assertGreaterEqual(len(errors), 3)

    def test_dashboard_distinguishes_local_history_from_quota(self):
        snapshot = NormalizedSnapshot(
            source="claude",
            sampled_at=1770000000,
            windows={
                "local_all": SnapshotWindow(
                    total_tokens=1000,
                    requests=2,
                    by_project={
                        "demo": AggregateBreakdown(total_tokens=800, requests=1, share_pct=80.0),
                    },
                    cache_state="live",
                )
            },
        )
        page = render_page([snapshot])
        self.assertIn("Operations briefing", page)
        self.assertIn("local history", page)
        self.assertIn("top-heavy 80%", page)
        self.assertNotIn("<dt>Utilization</dt><dd>0.0%</dd>", page)

    def test_dashboard_surfaces_operational_quota_context(self):
        now = int(time.time())
        snapshot = NormalizedSnapshot(
            source="claude",
            sampled_at=now,
            windows={
                "five_hour": SnapshotWindow(
                    window_start=now - 3600,
                    window_end=now,
                    resets_at=now + 4 * 3600,
                    utilization=0.72,
                    total_tokens=7200,
                    requests=7,
                    by_project={
                        "demo": AggregateBreakdown(total_tokens=6480, requests=6, share_pct=90.0),
                        "alpha-app": AggregateBreakdown(total_tokens=720, requests=1, share_pct=10.0),
                    },
                    by_model={
                        "sonnet": AggregateBreakdown(total_tokens=7200, requests=7, share_pct=100.0),
                    },
                    cache_state="live",
                ),
                "seven_day": SnapshotWindow(
                    window_start=now - 2 * 24 * 3600,
                    window_end=now,
                    resets_at=now + 5 * 24 * 3600,
                    utilization=0.34,
                    total_tokens=34000,
                    requests=31,
                    by_project={
                        "weekly-app": AggregateBreakdown(total_tokens=20400, requests=19, share_pct=60.0),
                        "archive-app": AggregateBreakdown(total_tokens=13600, requests=12, share_pct=40.0),
                    },
                    by_model={
                        "sonnet": AggregateBreakdown(total_tokens=34000, requests=31, share_pct=100.0),
                    },
                    cache_state="live",
                )
            },
        )
        page = render_page([snapshot])
        self.assertIn("Highest pressure", page)
        self.assertIn("Quota matrix", page)
        self.assertIn("Reset schedule", page)
        self.assertIn("5 hour</span><strong>72.0%", page)
        self.assertIn("qc-strip-bar-warm", page)
        self.assertIn("5 hour usage", page)
        self.assertIn("7 day usage", page)
        self.assertIn("qc-quota-split", page)
        self.assertEqual(page.count("<h4>Apps</h4>"), 2)
        self.assertIn("Window range", page)
        self.assertIn("Top model", page)
        self.assertIn("demo 90.0%", page)
        self.assertIn("weekly-app", page)
        self.assertIn("sonnet 100.0%", page)

    def test_dashboard_pairs_current_quota_with_seven_day_when_no_five_hour(self):
        now = int(time.time())
        snapshot = NormalizedSnapshot(
            source="gemini",
            sampled_at=now,
            windows={
                "current_quota": SnapshotWindow(
                    window_start=now - 2 * 3600,
                    window_end=now,
                    resets_at=now + 22 * 3600,
                    utilization=0.12,
                    total_tokens=1200,
                    requests=12,
                    by_project={
                        "gemini-live": AggregateBreakdown(total_tokens=1200, requests=12, share_pct=100.0),
                    },
                    cache_state="live",
                ),
                "seven_day": SnapshotWindow(
                    window_start=None,
                    window_end=now,
                    resets_at=None,
                    utilization=0.0,
                    total_tokens=4600,
                    requests=46,
                    by_project={
                        "gemini-week": AggregateBreakdown(total_tokens=3680, requests=36, share_pct=80.0),
                    },
                    cache_state="live",
                ),
            },
        )
        page = render_page([snapshot])
        self.assertIn("Current quota usage", page)
        self.assertIn("7 day usage", page)
        self.assertIn("qc-quota-split", page)
        self.assertEqual(page.count("<h4>Apps</h4>"), 2)
        self.assertIn("gemini-live", page)
        self.assertIn("gemini-week", page)

    def test_claude_local_scanner_reads_jsonl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "demo-project"
            project_dir.mkdir()
            record = {
                "timestamp": "2026-04-28T00:00:00Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 5,
                        "cache_creation_input_tokens": 0,
                    },
                },
            }
            (project_dir / "session.jsonl").write_text(json.dumps(record) + "\n")
            config = config_from_mapping(
                {
                    "providers": {
                        "claude": {
                            "enabled": True,
                            "paths": {"projects_dir": temp_dir},
                        }
                    }
                }
            )
            snapshots = scan_config(config)
            self.assertEqual(len(snapshots), 1)
            snapshot = snapshot_to_dict(snapshots[0])
            self.assertEqual(validate_snapshot_dict(snapshot), ())
            window = snapshot["windows"]["local_all"]
            self.assertEqual(window["total_tokens"], 35)
            self.assertEqual(window["requests"], 1)
            self.assertEqual(window["by_project"]["demo-project"]["models"]["claude-sonnet-4-6"], 35)

    def test_claude_local_scanner_redacts_missing_path(self):
        config = config_from_mapping(
            {
                "providers": {
                    "claude": {
                        "enabled": True,
                        "paths": {"projects_dir": "/tmp/quota-core-private-missing-path"},
                    }
                }
            }
        )
        snapshot = snapshot_to_dict(scan_config(config)[0])
        self.assertEqual(snapshot["warnings"], ["claude projects_dir does not exist"])
        self.assertNotIn("/tmp/quota-core-private-missing-path", json.dumps(snapshot))

    def test_codex_local_scanner_reads_state_db(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_db = Path(temp_dir) / "state_5.sqlite"
            with sqlite3.connect(state_db) as conn:
                conn.execute("CREATE TABLE threads (cwd TEXT, model TEXT, tokens_used INTEGER)")
                conn.execute(
                    "INSERT INTO threads (cwd, model, tokens_used) VALUES (?, ?, ?)",
                    (str(Path(temp_dir) / "demo-codex"), "openai/gpt-5.4", 42),
                )
                conn.execute(
                    "INSERT INTO threads (cwd, model, tokens_used) VALUES (?, ?, ?)",
                    (str(Path(temp_dir) / "demo-codex"), "openai/gpt-5.4", 8),
                )
                conn.execute(
                    "INSERT INTO threads (cwd, model, tokens_used) VALUES (?, ?, ?)",
                    (str(Path(temp_dir) / "ignored"), "openai/gpt-5.4", 0),
                )
            config = config_from_mapping(
                {
                    "providers": {
                        "codex": {
                            "enabled": True,
                            "paths": {"state_db": str(state_db)},
                        }
                    }
                }
            )
            snapshots = scan_config(config)
            self.assertEqual(len(snapshots), 1)
            snapshot = snapshot_to_dict(snapshots[0])
            self.assertEqual(validate_snapshot_dict(snapshot), ())
            window = snapshot["windows"]["local_all"]
            self.assertEqual(window["total_tokens"], 50)
            self.assertEqual(window["requests"], 2)
            self.assertNotIn("ignored", window["by_project"])
            self.assertEqual(window["by_project"]["demo-codex"]["models"]["gpt-5.4"], 50)

    def test_codex_local_scanner_redacts_missing_path(self):
        config = config_from_mapping(
            {
                "providers": {
                    "codex": {
                        "enabled": True,
                        "paths": {"state_db": "/tmp/quota-core-private-state.sqlite"},
                    }
                }
            }
        )
        snapshot = snapshot_to_dict(scan_config(config)[0])
        self.assertEqual(snapshot["warnings"], ["codex state_db does not exist"])
        self.assertNotIn("/tmp/quota-core-private-state.sqlite", json.dumps(snapshot))

    def test_gemini_local_scanner_reads_sessions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / "demo-gemini" / "chats"
            session_dir.mkdir(parents=True)
            session = {
                "messages": [
                    {
                        "type": "gemini",
                        "timestamp": "2026-04-28T00:00:00Z",
                        "model": "models/gemini-2.5-pro",
                        "tokens": {"input": 11, "output": 12, "cached": 3, "thoughts": 4},
                    },
                    {
                        "type": "user",
                        "timestamp": "2026-04-28T00:00:01Z",
                        "model": "models/gemini-2.5-pro",
                        "tokens": {"total": 999},
                    }
                ]
            }
            (session_dir / "session-1.json").write_text(json.dumps(session))
            (session_dir / "session-2.json").write_text(
                json.dumps(
                    {
                        "messages": [
                            {
                                "type": "gemini",
                                "timestamp": "2026-04-28T00:00:02Z",
                                "model": "models/gemini-2.5-flash",
                                "tokens": {"total": 7, "input": 999},
                            }
                        ]
                    }
                )
            )
            config = config_from_mapping(
                {
                    "providers": {
                        "gemini": {
                            "enabled": True,
                            "paths": {"tmp_dir": temp_dir},
                        }
                    }
                }
            )
            snapshots = scan_config(config)
            self.assertEqual(len(snapshots), 1)
            snapshot = snapshot_to_dict(snapshots[0])
            self.assertEqual(validate_snapshot_dict(snapshot), ())
            window = snapshot["windows"]["local_all"]
            self.assertEqual(window["total_tokens"], 37)
            self.assertEqual(window["requests"], 2)
            self.assertEqual(window["by_project"]["demo-gemini"]["models"]["gemini-2.5-pro"], 30)
            self.assertEqual(window["by_project"]["demo-gemini"]["models"]["gemini-2.5-flash"], 7)

    def test_gemini_local_scanner_redacts_missing_path(self):
        config = config_from_mapping(
            {
                "providers": {
                    "gemini": {
                        "enabled": True,
                        "paths": {"tmp_dir": "/tmp/quota-core-private-gemini-tmp"},
                    }
                }
            }
        )
        snapshot = snapshot_to_dict(scan_config(config)[0])
        self.assertEqual(snapshot["warnings"], ["gemini tmp_dir does not exist"])
        self.assertNotIn("/tmp/quota-core-private-gemini-tmp", json.dumps(snapshot))


class PublicSplitGuardTests(unittest.TestCase):
    def test_quota_core_does_not_import_private_ops(self):
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "quota_core").rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                module = None
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module = alias.name.split(".", 1)[0]
                        if module in PRIVATE_IMPORT_ROOTS:
                            offenders.append(f"{path.relative_to(REPO_ROOT)} imports {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module.split(".", 1)[0]
                    if module in PRIVATE_IMPORT_ROOTS:
                        offenders.append(f"{path.relative_to(REPO_ROOT)} imports {node.module}")
        self.assertEqual(offenders, [])

    def test_public_targets_do_not_contain_private_markers(self):
        denied = (
            "/home/" + "truhojun",
            "641923" + "6710",
            "TELE" + "GRAM",
            "tele" + "gram",
            "bot" + "0:",
            "gh" + "p_",
        )
        offenders: list[str] = []
        for target in PUBLIC_TARGETS:
            files = [target] if target.is_file() else sorted(target.rglob("*"))
            for path in files:
                if not path.is_file() or path.suffix in {".pyc", ".sqlite", ".db"}:
                    continue
                try:
                    text = path.read_text(errors="ignore")
                except OSError:
                    continue
                for marker in denied:
                    if marker in text:
                        offenders.append(f"{path.relative_to(REPO_ROOT)} contains private marker")
                        break
        self.assertEqual(offenders, [])


class CliFlowTests(unittest.TestCase):
    def test_demo_writes_html_without_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "demo.html"
            write_demo(output)
            text = output.read_text()
            self.assertIn("quota_core demo", text)
            self.assertIn("demo-project", text)

    def test_scan_then_dashboard_without_private_ops(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            snapshot_path = Path(temp_dir) / "snapshot.json"
            dashboard_path = Path(temp_dir) / "dashboard.html"
            projects_dir = Path(temp_dir) / "claude-projects"
            projects_dir.mkdir()
            config_path.write_text(
                """
[providers.claude]
enabled = true

[providers.claude.paths]
projects_dir = "{projects_dir}"

[providers.codex]
enabled = false

[providers.gemini]
enabled = false
""".format(projects_dir=projects_dir)
            )
            write_scan(config_path, snapshot_path)
            payload = json.loads(snapshot_path.read_text())
            self.assertIn("snapshots", payload)
            self.assertGreaterEqual(len(payload["snapshots"]), 1)
            write_dashboard(snapshot_path, dashboard_path)
            self.assertIn("quota_core dashboard", dashboard_path.read_text())


if __name__ == "__main__":
    unittest.main()
