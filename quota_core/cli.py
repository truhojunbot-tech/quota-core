"""quota-core command line entrypoint skeleton."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .config import QuotaCoreConfig, default_config_path, load_config, validate_config, write_default_config
from .dashboard.renderer import render_page
from .providers import enabled_provider_names
from .snapshot import NormalizedSnapshot, empty_snapshot, snapshot_from_dict, snapshot_to_dict, validate_snapshot_dict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quota-core")
    subcommands = parser.add_subparsers(dest="command")
    init_parser = subcommands.add_parser("init", help="write a starter config")
    init_parser.add_argument("--config", help="config path to write")
    init_parser.add_argument("--force", action="store_true", help="overwrite an existing config")
    demo_parser = subcommands.add_parser("demo", help="run with synthetic fixtures")
    demo_parser.add_argument("--output", default="quota-core-demo.html", help="HTML output path")
    scan_parser = subcommands.add_parser("scan", help="scan configured local providers")
    scan_parser.add_argument("--config", help="config path to read")
    scan_parser.add_argument("--output", default="quota-core-snapshot.json", help="snapshot JSON output path")
    dashboard_parser = subcommands.add_parser("dashboard", help="render a local dashboard from snapshots")
    dashboard_parser.add_argument("--snapshot", default="quota-core-snapshot.json", help="snapshot JSON path")
    dashboard_parser.add_argument("--output", default="quota-core-dashboard.html", help="HTML output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "init":
        path = write_default_config(args.config or default_config_path(), force=args.force)
        print(f"Wrote config: {path}")
        return 0
    if args.command == "demo":
        path = write_demo(args.output)
        print(f"Wrote demo dashboard: {path}")
        return 0
    if args.command == "scan":
        path = write_scan(args.config, args.output)
        print(f"Wrote snapshot: {path}")
        return 0
    if args.command == "dashboard":
        path = write_dashboard(args.snapshot, args.output)
        print(f"Wrote dashboard: {path}")
        return 0
    raise SystemExit(f"quota-core {args.command} is not implemented yet")


def write_demo(output: str | Path) -> Path:
    """Render the bundled synthetic fixture as a demo dashboard."""

    fixture = Path(__file__).resolve().parent / "fixtures" / "normalized" / "claude-live.json"
    data = json.loads(fixture.read_text())
    errors = validate_snapshot_dict(data)
    if errors:
        raise ValueError("Invalid bundled demo fixture: " + "; ".join(errors))
    html = render_page([snapshot_from_dict(data)], title="quota_core demo")
    target = Path(output).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html)
    return target


def write_scan(config_path: str | Path | None, output: str | Path) -> Path:
    """Write a normalized snapshot bundle from public config."""

    config = load_config(config_path)
    snapshots = scan_config(config)
    payload = {
        "snapshots": [snapshot_to_dict(snapshot) for snapshot in snapshots],
        "warnings": list(validate_config(config)),
    }
    target = Path(output).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2))
    return target


def scan_config(config: QuotaCoreConfig) -> list[NormalizedSnapshot]:
    """Return normalized snapshots for enabled providers."""

    sampled_at = int(time.time())
    snapshots: list[NormalizedSnapshot] = []
    for provider_name in enabled_provider_names(config.providers):
        provider_config = config.providers[provider_name]
        if provider_name == "claude":
            from .adapters.claude import scan_claude_local

            snapshots.append(scan_claude_local(provider_config, sampled_at))
        elif provider_name == "codex":
            from .adapters.codex import scan_codex_local

            snapshots.append(scan_codex_local(provider_config, sampled_at))
        elif provider_name == "gemini":
            from .adapters.gemini import scan_gemini_local

            snapshots.append(scan_gemini_local(provider_config, sampled_at))
        else:
            snapshots.append(
                NormalizedSnapshot(
                    source=provider_name,
                    sampled_at=sampled_at,
                    warnings=("local scanner is not implemented for this provider yet",),
                )
            )
    return snapshots


def write_dashboard(snapshot_path: str | Path, output: str | Path) -> Path:
    """Render dashboard HTML from a snapshot JSON file."""

    snapshots = load_snapshot_file(snapshot_path)
    html = render_page(snapshots, title="quota_core dashboard")
    target = Path(output).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html)
    return target


def load_snapshot_file(snapshot_path: str | Path) -> list[NormalizedSnapshot]:
    """Load one snapshot or a snapshot bundle from JSON."""

    data = json.loads(Path(snapshot_path).expanduser().read_text())
    if isinstance(data, dict) and isinstance(data.get("snapshots"), list):
        return [snapshot_from_dict(item) for item in data["snapshots"] if isinstance(item, dict)]
    if isinstance(data, dict):
        errors = validate_snapshot_dict(data)
        if errors:
            raise ValueError("Invalid snapshot: " + "; ".join(errors))
        return [snapshot_from_dict(data)]
    raise ValueError("Snapshot file must contain an object or a snapshot bundle")


if __name__ == "__main__":
    raise SystemExit(main())
