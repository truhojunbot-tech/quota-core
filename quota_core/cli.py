"""quota-core command line entrypoint skeleton."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from .config import QuotaCoreConfig, default_config_path, load_config, validate_config, write_default_config
from .dashboard.renderer import render_page
from .dashboard.verification import verify_dashboard_html
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
    verify_parser = subcommands.add_parser("verify-dashboard", help="verify a dashboard HTML artifact against its snapshot")
    verify_parser.add_argument("--snapshot", default="quota-core-snapshot.json", help="snapshot JSON path")
    verify_parser.add_argument("--html", default="quota-core-dashboard.html", help="dashboard HTML path")
    parity_parser = subcommands.add_parser("verify-session-parity", help="compare quota-core Claude session analytics with the official session-report analyzer")
    parity_parser.add_argument("--official-analyzer", required=True, help="path to official analyze-sessions.mjs")
    parity_parser.add_argument("--transcript-root", default="~/.claude/projects", help="Claude projects transcript root")
    parity_parser.add_argument("--since", default="7d", help="rolling range such as 24h, 7d, 30d, or all")
    parity_parser.add_argument("--max-file-bytes", type=int, default=1_000_000_000_000, help="quota-core max JSONL file size for parity runs")
    parity_parser.add_argument("--tolerance-pct", type=float, default=0.1, help="allowed total-token delta percentage before failing")
    parity_parser.add_argument("--output", help="optional JSON summary output path")
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
    if args.command == "verify-dashboard":
        errors = verify_dashboard(args.snapshot, args.html)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print("Dashboard verification passed")
        return 0
    if args.command == "verify-session-parity":
        summary = verify_session_parity(
            official_analyzer=args.official_analyzer,
            transcript_root=args.transcript_root,
            since=args.since,
            max_file_bytes=args.max_file_bytes,
            output=args.output,
        )
        print(json.dumps(summary, indent=2))
        if abs(float(summary["total_delta_pct"])) > args.tolerance_pct:
            print(f"ERROR: total token delta exceeds tolerance: {summary['total_delta_pct']}% > {args.tolerance_pct}%")
            return 1
        print("Session parity verification passed")
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


def verify_dashboard(snapshot_path: str | Path, html_path: str | Path) -> list[str]:
    """Verify generated dashboard HTML against the snapshot that produced it."""

    snapshots = load_snapshot_file(snapshot_path)
    html = Path(html_path).expanduser().read_text(errors="replace")
    return verify_dashboard_html(snapshots, html)


def verify_session_parity(
    *,
    official_analyzer: str | Path,
    transcript_root: str | Path,
    since: str,
    max_file_bytes: int,
    output: str | Path | None = None,
) -> dict[str, object]:
    """Compare quota-core session analytics against the official analyzer."""

    analyzer = Path(official_analyzer).expanduser()
    root = Path(transcript_root).expanduser()
    command = ["node", str(analyzer), "--json", "--dir", str(root)]
    if since != "all":
        command.extend(["--since", since])
    result = subprocess.run(command, text=True, capture_output=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2000:] or f"official analyzer failed with exit code {result.returncode}")
    official = json.loads(result.stdout)

    from .session import analyze_claude_sessions

    core = analyze_claude_sessions(
        [root],
        since="all" if since == "all" else since,
        redaction="preview",
        max_file_bytes=max_file_bytes,
    )
    summary = session_parity_summary(official, core)
    summary["official_analyzer"] = str(analyzer)
    summary["transcript_root"] = str(root)
    summary["since"] = since
    if output:
        target = Path(output).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def session_parity_summary(official: dict[str, object], core: dict[str, object]) -> dict[str, object]:
    """Return comparable high-level metrics for official vs quota-core reports."""

    official_overall = official.get("overall") if isinstance(official.get("overall"), dict) else {}
    official_input = official_overall.get("input_tokens") if isinstance(official_overall.get("input_tokens"), dict) else {}
    core_totals = core.get("totals") if isinstance(core.get("totals"), dict) else {}
    official_total = int(official_input.get("total") or 0) + int(official_overall.get("output_tokens") or 0)
    core_total = int(core_totals.get("total_tokens") or 0)
    delta = core_total - official_total
    return {
        "official_total_tokens": official_total,
        "quota_core_total_tokens": core_total,
        "total_delta_tokens": delta,
        "total_delta_pct": round(delta / official_total * 100, 4) if official_total else 0.0,
        "official_api_calls": int(official_overall.get("api_calls") or 0),
        "quota_core_api_calls": int(core_totals.get("api_calls") or 0),
        "official_human_messages": int(official_overall.get("human_messages") or 0),
        "quota_core_human_messages": int(core_totals.get("human_messages") or 0),
        "official_sessions": int(official_overall.get("sessions") or 0),
        "quota_core_sessions": int(core_totals.get("sessions") or 0),
        "official_cache_hit_pct": float(official_input.get("pct_cached") or 0),
        "quota_core_cache_hit_pct": float(core_totals.get("cache_hit_pct") or 0),
    }


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
