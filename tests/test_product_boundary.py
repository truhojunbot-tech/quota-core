"""Guard the public package against deployment-specific dependencies."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from quota_core.context_economics import metrics_b1
from quota_core.context_economics.metrics_b1 import compute_metrics


PACKAGE = Path(__file__).resolve().parents[1] / "quota_core"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PRIVATE_MODULES = {"quota_ops", "alfred", "agent_crew"}
FLEET_NAMES = re.compile(
    r"(?<![a-z0-9])(?:alfred|quota-ops|apify-forge|halla|alpha[_-]engine|"
    r"kis-trader|btc-quant-engine|claude_autonomous_trader)(?![a-z0-9])",
    re.IGNORECASE,
)


def _dynamic_import_target(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    dynamic = (isinstance(func, ast.Name) and func.id in {"__import__", "import_module"}) or (
        isinstance(func, ast.Attribute) and func.attr == "import_module")
    if dynamic and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


class ProductBoundaryTests(unittest.TestCase):
    maxDiff = None

    def test_core_has_no_private_imports_paths_names_or_session_actuation(self):
        violations = []
        for path in PACKAGE.rglob("*"):
            if not path.is_file() or path.suffix == ".py" or "__pycache__" in path.parts:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if "/home/" in content or FLEET_NAMES.search(content):
                violations.append(f"{path}: private data file")
        for path in PACKAGE.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "/home/" in source or FLEET_NAMES.search(source):
                violations.append(f"{path}: private path or fleet name")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    modules = []
                for module in modules:
                    if module.split(".")[0] in PRIVATE_MODULES:
                        violations.append(f"{path}:{node.lineno}: private import {module}")
                module = _dynamic_import_target(node)
                if module and module.split(".")[0] in PRIVATE_MODULES:
                    violations.append(f"{path}:{node.lineno}: dynamic private import {module}")
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    value = node.value
                    if re.search(r"\btmux\b|session[_ -]?restart", value, re.I):
                        violations.append(f"{path}:{node.lineno}: session actuation {value!r}")
        self.assertEqual(violations, [])

    def test_dynamic_import_guard_detects_private_import_forms(self):
        for source in ('importlib.import_module("agent_crew.memory")',
                       '__import__("alfred.runtime")',
                       'import_module("quota_ops.secret")'):
            tree = ast.parse(source)
            targets = [_dynamic_import_target(node) for node in ast.walk(tree)]
            self.assertTrue(any(target and target.split(".")[0] in PRIVATE_MODULES
                                for target in targets), source)

    def test_fixtures_have_no_fleet_names_or_home_paths(self):
        violations = []
        for path in FIXTURES.rglob("*"):
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if "/home/" in content or FLEET_NAMES.search(content):
                violations.append(str(path.relative_to(FIXTURES)))
        self.assertEqual(violations, [])

    def test_missing_private_metric_sources_are_unknown_not_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = compute_metrics(100, 200, root / "missing-crews", root / "missing-hooks",
                                   bots=["sample"], transcript_root=root / "missing-transcripts",
                                   lifecycle_log=root / "missing-lifecycle")
        self.assertTrue(rows)
        self.assertTrue(all(row["value"] is None or row["n"] for row in rows))
        self.assertTrue(all(row.get("reason") for row in rows if row["value"] is None))

    def test_nonshadow_served_receipt_without_memory_db_has_specific_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bot = root / "sample"
            bot.mkdir()
            (bot / "context_events.jsonl").write_text(json.dumps({
                "event_type": "context_pack_built", "shadow": False,
                "ts": 150, "result_ids": ["served-item"], "selected_count": 1,
            }) + "\n", encoding="utf-8")
            rows = compute_metrics(100, 200, root, root / "missing-hooks",
                                   bots=["sample"])
        m7 = next(row for row in rows if row["metric"] == "M7")
        self.assertIsNone(m7["value"])
        self.assertEqual(m7["reason"], "memory_db_not_supplied:--memory-db")

    def test_sha_matched_eval_without_memory_db_has_specific_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            eval_path = root / "eval.json"
            content = b'{"owner_cases":[],"task_cases":[]}'
            eval_path.write_bytes(content)
            with patch.object(metrics_b1, "EVAL_SHA256", hashlib.sha256(content).hexdigest()):
                rows = compute_metrics(100, 200, root / "missing-crews",
                                       root / "missing-hooks", bots=["sample"],
                                       eval_path=eval_path)
        for metric in ("M3a", "M3b"):
            row = next(row for row in rows if row["metric"] == metric)
            self.assertIsNone(row["value"])
            self.assertEqual(row["reason"], "memory_db_not_supplied:--memory-db")

    def test_local_revert_check_is_unknown_without_root_and_true_with_one(self):
        approved_at = 1_790_000_000
        end = approved_at + 3600
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "sample"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "marker.txt").write_text("sample\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "marker.txt"], check=True)
            commit_time = datetime.fromtimestamp(approved_at + 60, timezone.utc).isoformat()
            commit_env = dict(os.environ, GIT_AUTHOR_DATE=commit_time,
                              GIT_COMMITTER_DATE=commit_time)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                            "-c", "user.email=test@example.invalid", "commit", "-q",
                            "-m", "Revert change from PR #7"], check=True, env=commit_env)
            real_run = subprocess.run

            def read_only_gh(args, **kwargs):
                if args[0] == "gh":
                    return subprocess.CompletedProcess(args, 0, "[]", "")
                return real_run(args, **kwargs)

            with patch.object(metrics_b1.subprocess, "run", side_effect=read_only_gh):
                with patch.dict(os.environ, {"QUOTA_CORE_LOCAL_REPOS": ""}):
                    self.assertIsNone(metrics_b1._github_followup(
                        "sample", 99, 7, approved_at, end))
                with patch.dict(os.environ, {"QUOTA_CORE_LOCAL_REPOS": str(root)}):
                    self.assertIs(metrics_b1._github_followup(
                        "sample", 99, 7, approved_at, end), True)


if __name__ == "__main__":
    unittest.main()
