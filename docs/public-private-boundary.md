# Public/Private Boundary

## Purpose

This document defines the boundary between the future public `quota_core` repo and a private operations repo.

The product goal is that a public user can install `quota_core`, add a small config file, and run:

```bash
quota-core init
quota-core demo
quota-core scan
quota-core dashboard
```

Private ops should be an overlay for one environment, not a required dependency for public usage.

## Public Repo Ownership

The public repo owns reusable product code.

It may contain:
- Config schema and config loader
- CLI entrypoints
- Provider adapter interfaces
- Local provider scanners
- Snapshot schema and validators
- Window, reset, stale, and cache helpers
- Project/model aggregation helpers
- Runtime tagging helpers that accept config as input
- Dashboard renderer
- Shared GUI components
- Synthetic fixtures
- Public tests
- Public README, examples, and architecture docs

It must not contain:
- Real credentials
- API tokens
- Telegram bot tokens or chat IDs
- Internal bot names
- Internal filesystem paths
- Real usage DB files
- Real quota cache files
- Internal cron, systemd, or tmux setup
- Remote hostnames, labels, sudo settings, or SSH assumptions
- Private alert thresholds that reveal operational details

## Private Ops Ownership

The private repo owns deployment and operations overlay code.

It may contain:
- Private config overlays
- Internal bot registry
- Real credentials paths
- Real DB locations and migration snapshots
- Cron, watchdog, and alert wiring
- Telegram notification settings
- Live probe schedules
- Wrapper install scripts and symlink repair
- Remote host settings
- Operational runbooks

It must not contain the authoritative implementation of:
- Snapshot schema
- Provider-common aggregation logic
- Dashboard GUI components
- Generic runtime tagging behavior
- Public fixtures or public regression baselines

Private ops can wrap public core, but it cannot become the real core by accident.

## Dependency Direction

Allowed:

```text
private ops -> quota_core
```

Forbidden:

```text
quota_core -> private ops
```

Public code must not import private modules, read private config paths by default, or assume any local workspace layout.

## Configuration Contract

Public users configure behavior through a public-safe config file.

Config may include:
- Enabled providers
- Local log/database paths
- Dashboard host and port
- Cache directory
- Runtime bot path mappings
- Whether live probes are enabled

Config must treat live credentials as optional. The public tool must still support demo mode and local scan mode without live API credentials.

Default behavior must be conservative:
- Live probes disabled by default
- Missing provider paths produce warnings, not crashes
- No network calls during `quota-core demo`
- No private path assumptions

## GUI Boundary

Dashboard code belongs in public core only if it renders normalized snapshots.

Allowed GUI inputs:
- Normalized snapshot JSON
- Public config values
- Static public assets

Forbidden GUI inputs:
- Claude JSONL files directly
- Codex SQLite directly
- Gemini session files directly
- Real credentials
- Telegram settings
- Private bot registry
- Internal paths

Provider adapters and snapshot normalizers own raw data parsing. The dashboard must not repair provider data.

## Provider Boundary

Provider adapters may know provider-specific file formats and live probe mechanisms. They must output normalized snapshots.

Examples:
- Public: `~/.claude/projects` as a configurable default
- Public: `~/.codex/state_5.sqlite` as a configurable default
- Public: `~/.gemini/tmp` as a configurable default
- Public: `live_probe: false` by default
- Private: a cron schedule that enables and runs live probes for a specific account

## Runtime Boundary

Public runtime helpers may:
- Preserve explicit `LLM_USAGE_CLASS` and `BOT_NAME`
- Match cwd against configured bot paths
- Produce runtime metadata records
- Normalize invocation timestamps

Public runtime helpers must not:
- Hard-code bot names
- Hard-code private instance paths
- Write directly to provider-specific private DBs
- Assume a wrapper install location

Private ops owns the actual bot registry and wrapper installation.

## Split Readiness Checks

Before repo split, public core must pass these checks:
- No hard-coded private home paths
- No Telegram token or chat ID
- No real DB files committed
- No quota cache files committed
- No internal bot registry committed
- No imports from private ops modules
- `quota-core demo` works without config
- `quota-core scan` works with generated config and local paths
- Dashboard renders from synthetic normalized fixtures
- Tests pass without private ops present
- Clean install validation succeeds in a temporary environment

Private ops must pass these checks:
- Imports public core through stable public APIs
- Owns all private config and operations wiring
- Existing hourly reports and alerts still work
- Wrapper behavior remains compatible with current operations
