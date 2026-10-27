"""Guard the public package against deployment-specific dependencies."""

from __future__ import annotations

import ast
import re
import tempfile
import unittest
from pathlib import Path

from quota_core.context_economics.metrics_b1 import compute_metrics


PACKAGE = Path(__file__).resolve().parents[1] / "quota_core"
PRIVATE_MODULES = {"quota_ops", "alfred", "agent_crew"}
FLEET_NAMES = re.compile(
    r"(?<![a-z0-9])(?:alfred|quota-ops|apify-forge|halla|alpha_engine|"
    r"kis-trader|btc-quant-engine|claude_autonomous_trader)(?![a-z0-9])",
    re.IGNORECASE,
)


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
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    value = node.value
                    if re.search(r"\btmux\b|session[_ -]?restart", value, re.I):
                        violations.append(f"{path}:{node.lineno}: session actuation {value!r}")
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


if __name__ == "__main__":
    unittest.main()
