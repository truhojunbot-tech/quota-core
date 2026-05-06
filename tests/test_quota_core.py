from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from quota_core.adapters.claude import normalize_claude_quota
from quota_core.adapters.projects import normalize_project_name
from quota_core.adapters.gemini import normalize_gemini_usage
from quota_core.config import config_from_mapping, load_config, validate_config, write_default_config
from quota_core.runtime import runtime_env
from quota_core.snapshot import AggregateBreakdown, NormalizedSnapshot, RuntimeBreakdown, SnapshotWindow, snapshot_to_dict, validate_snapshot_dict
from quota_core.cli import scan_config, write_dashboard, write_demo, write_scan
from quota_core.dashboard.renderer import render_page
from quota_core.dashboard.view_model import build_provider_dashboard


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

    def test_agent_crew_worktree_projects_merge_with_parent_project(self):
        payload = {
            "fetched_at": 1770000000,
            "seven_day": {
                "utilization": 0.09,
                "resets_at": 1770003600,
                "tokens_used": 300,
                "by_project": {
                    "demo-app": {
                        "tokens": 180,
                        "requests": 2,
                        "models": {"sonnet": 180},
                        "model_requests": {"sonnet": 2},
                    },
                    "-agent-crew-worktrees-demo-app-claude": {
                        "tokens": 120,
                        "requests": 1,
                        "models": {"sonnet": 120},
                        "model_requests": {"sonnet": 1},
                    },
                },
                "runtime_tokens_used": 100,
                "runtime_by_project": {
                    "-agent-crew-worktrees-alpha-engine-claude": {
                        "tokens": 100,
                        "requests": 1,
                        "models": {"haiku": 100},
                    }
                },
            },
        }
        snapshot = snapshot_to_dict(normalize_claude_quota(payload))
        window = snapshot["windows"]["seven_day"]
        self.assertEqual(validate_snapshot_dict(snapshot), ())
        self.assertEqual(list(window["by_project"]), ["demo-app"])
        self.assertEqual(window["by_project"]["demo-app"]["total_tokens"], 300)
        self.assertEqual(window["by_project"]["demo-app"]["requests"], 3)
        self.assertEqual(window["by_project"]["demo-app"]["models"]["sonnet"], 300)
        self.assertEqual(window["runtime"]["by_project"]["alpha-engine"]["total_tokens"], 100)
        self.assertNotIn("agent-crew-worktrees", json.dumps(window))

    def test_project_name_normalizes_provider_leaf_worktrees(self):
        self.assertEqual(
            normalize_project_name("/workspace/.agent_crew/worktrees/alpha_engine/codex"),
            "alpha-engine",
        )
        self.assertEqual(
            normalize_project_name("/workspace/worktrees/context-forge-core/codex"),
            "context-forge-core",
        )
        self.assertEqual(
            normalize_project_name("/workspace/worktrees/agent_crew/codex"),
            "agent-crew",
        )
        self.assertEqual(
            normalize_project_name("/workspace/worktrees/archive/logs"),
            "logs",
        )
        self.assertEqual(
            normalize_project_name("/workspace/agent_crew/archive/logs"),
            "logs",
        )
        self.assertEqual(
            normalize_project_name("worktrees-context-forge-core-claude"),
            "context-forge-core",
        )

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
        self.assertIn("Provider Details", page)
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
                    runtime=RuntimeBreakdown(
                        total_tokens=1800,
                        requests=3,
                        by_project={
                            "quota-runtime": AggregateBreakdown(total_tokens=1800, requests=3, share_pct=100.0),
                        },
                    ),
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
        self.assertIn("전체 현황", page)
        self.assertIn("setInterval(function(){window.location.reload();},60000)", page)
        self.assertIn("CLAUDE MAX", page)
        self.assertNotIn("Operations report", page)
        self.assertNotIn("Quota report", page)
        self.assertIn("LLM Dashboard", page)
        self.assertIn("Claude Max Quota", page)
        self.assertIn("Provider Details", page)
        self.assertIn("자동 런타임 LLM 사용량", page)
        self.assertIn("qc-runtime-report", page)
        self.assertIn("Claude Runtime", page)
        self.assertIn("quota-runtime", page)
        self.assertNotIn("<h4>Runtime</h4>", page)
        self.assertIn("Claude Max", page)
        self.assertIn("5시간", page)
        self.assertIn("7일", page)
        self.assertIn("시간 후 리셋", page)
        self.assertIn("72.0%", page)
        self.assertIn("qc-quota-split", page)
        self.assertGreaterEqual(page.count("<h4>Apps</h4>"), 2)
        self.assertIn("Window range", page)
        self.assertIn("Top model", page)
        self.assertIn("demo 90.0%", page)
        self.assertIn("weekly-app", page)
        self.assertIn("sonnet 100.0%", page)

    def test_dashboard_timeline_preserves_legacy_history_semantics(self):
        now = int(time.time())
        snapshot = NormalizedSnapshot(
            source="claude",
            sampled_at=now,
            windows={},
            history={
                "usage_timeline": {
                    "unit": "usd",
                    "dates": ["2026-04-28", "2026-04-29", "2026-04-30"],
                    "daily_total": {"2026-04-28": 1.0, "2026-04-29": 2.0, "2026-04-30": 3.0},
                    "datasets": [
                        {
                            "project": "alpha-app",
                            "total_cost": 4.0,
                            "daily": {"2026-04-28": 0.0, "2026-04-29": 1.0, "2026-04-30": 3.0},
                        }
                    ],
                },
                "quota_history": [
                    {"ts": now - 60, "5h_util": 0.47, "7d_util": 0.06},
                    {"ts": now, "5h_util": 0.72, "7d_util": 0.08},
                ],
            },
        )
        page = render_page([snapshot])
        self.assertIn("시계열 사용량", page)
        self.assertIn("Quota 시계열", page)
        self.assertIn("project-line-chart", page)
        self.assertIn("project-line", page)
        self.assertIn("style=\"stroke:#38bdf8\"", page)
        self.assertIn("timeline-project-name", page)
        self.assertIn("<i style=\"background:#38bdf8\"></i>alpha-app", page)
        self.assertIn("<polyline points=", page)
        self.assertIn("mini-sparkline", page)
        self.assertIn("72.0%", page)
        self.assertNotIn("0.7%", page)

    def test_dashboard_view_model_matches_original_report_windows(self):
        now = int(time.time())
        snapshot = NormalizedSnapshot(
            source="codex",
            sampled_at=now,
            windows={
                "five_hour": SnapshotWindow(
                    window_start=now - 3600,
                    window_end=now,
                    resets_at=now + 4 * 3600,
                    utilization=0.21,
                    total_tokens=2100,
                    requests=21,
                    by_project={"short-app": AggregateBreakdown(total_tokens=2100, requests=21, share_pct=100.0)},
                    cache_state="live",
                ),
                "seven_day": SnapshotWindow(
                    window_start=now - 24 * 3600,
                    window_end=now,
                    resets_at=now + 6 * 24 * 3600,
                    utilization=0.44,
                    total_tokens=4400,
                    requests=44,
                    by_project={"weekly-app": AggregateBreakdown(total_tokens=4400, requests=44, share_pct=100.0)},
                    cache_state="live",
                ),
                "local_all": SnapshotWindow(total_tokens=9000, requests=90, cache_state="live"),
            },
        )
        provider = build_provider_dashboard(snapshot)
        self.assertEqual([item.name for item in provider.comparison], ["five_hour", "seven_day"])
        self.assertEqual(provider.primary.name if provider.primary else None, "seven_day")
        self.assertEqual([item.name for item in provider.details], ["local_all"])

    def test_dashboard_keeps_gemini_local_history_out_of_operations_pair(self):
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
        provider = build_provider_dashboard(snapshot)
        self.assertEqual([item.name for item in provider.comparison], ["current_quota"])
        self.assertEqual([item.name for item in provider.details], ["seven_day"])
        self.assertTrue(provider.comparison[0].is_quota)
        self.assertNotIn("Operations report", page)
        self.assertNotIn("Quota report", page)
        self.assertIn("Gemini (Google)", page)
        self.assertIn("현재 quota window", page)
        self.assertIn("7일 전체 사용량", page)
        self.assertIn("<dt>Utilization</dt><dd>local history</dd>", page)
        self.assertIn("<dt>Tokens</dt><dd>4.6K</dd>", page)
        self.assertIn("qc-quota-split", page)
        self.assertGreaterEqual(page.count("<h4>Apps</h4>"), 1)
        self.assertIn("gemini-live", page)
        self.assertIn("gemini-week", page)

    def test_gemini_quota_groups_are_normalized_and_rendered(self):
        now = int(time.time())
        snapshot = normalize_gemini_usage(
            {
                "fetched_at": now,
                "quota": {"buckets": []},
                "current_quota": {
                    "total": 1200,
                    "requests": 12,
                    "utilization": 0.03,
                    "resets_at": now + 22 * 3600,
                    "window_seconds": 24 * 3600,
                    "by_project": {
                        "gemini-live": {
                            "total": 1200,
                            "requests": 12,
                            "models": {"gemini-3-flash-preview": 1200},
                        }
                    },
                },
                "quota_by_model": {
                    "gemini-3-flash-preview": {
                        "utilization": 0.03,
                        "resets_at": now + 22 * 3600,
                        "token_type": "REQUESTS",
                    },
                    "gemini-3.1-pro-preview": {
                        "utilization": 0.0,
                        "resets_at": now + 24 * 3600,
                        "token_type": "REQUESTS",
                    },
                },
            }
        )
        payload = snapshot_to_dict(snapshot)
        self.assertEqual(validate_snapshot_dict(payload), ())
        groups = payload["windows"]["current_quota"]["quota_groups"]
        self.assertEqual(groups["flash"]["label"], "Flash 그룹")
        self.assertEqual(groups["pro"]["label"], "Pro 그룹")
        page = render_page([snapshot])
        self.assertIn("Flash 그룹", page)
        self.assertIn("Pro 그룹", page)
        self.assertIn("그룹별 Request 한도", page)

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
                    (str(Path(temp_dir) / ".agent_crew" / "demo-codex" / "claude"), "openai/gpt-5.4", 5),
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
            self.assertEqual(window["total_tokens"], 55)
            self.assertEqual(window["requests"], 3)
            self.assertNotIn("ignored", window["by_project"])
            self.assertEqual(window["by_project"]["demo-codex"]["models"]["gpt-5.4"], 55)
            self.assertNotIn("claude", window["by_project"])

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
            (Path(temp_dir) / "demo-gemini" / ".project_root").write_text(
                "/workspace/.agent_crew/worktrees/demo-gemini/gemini"
            )
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
            jsonl_message = {
                "id": "jsonl-message-1",
                "type": "gemini",
                "timestamp": "2026-04-30T08:24:48.369Z",
                "model": "models/gemini-3-flash-preview",
                "tokens": {"total": 13},
            }
            (session_dir / "session-3.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"sessionId": "demo"}),
                        json.dumps(jsonl_message),
                        json.dumps({**jsonl_message, "toolCalls": [{"id": "tool-1"}]}),
                    ]
                )
                + "\n"
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
            self.assertEqual(window["total_tokens"], 50)
            self.assertEqual(window["requests"], 3)
            self.assertEqual(window["by_project"]["demo-gemini"]["models"]["gemini-2.5-pro"], 30)
            self.assertEqual(window["by_project"]["demo-gemini"]["models"]["gemini-2.5-flash"], 7)
            self.assertEqual(window["by_project"]["demo-gemini"]["models"]["gemini-3-flash-preview"], 13)

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
            "/home/private-user",
            "1234567890",
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
