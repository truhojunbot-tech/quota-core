# quota_core

`quota_core` turns local LLM usage data into normalized snapshots and dashboard views.

The public product goal is simple: install the package, create a small config file, scan local provider data, and open a local dashboard.

```bash
pip install quota-core
quota-core init
quota-core scan
quota-core dashboard
```

This repository is currently being refactored toward that public shape. Hojoon's private operations code lives beside it for now, but public `quota_core` must not depend on private paths, credentials, Telegram settings, real databases, or internal bot names.

## Quickstart

From this source tree:

```bash
python -m quota_core.cli init --config /tmp/quota-core.toml
python -m quota_core.cli demo --output /tmp/quota-core-demo.html
python -m quota_core.cli scan --config /tmp/quota-core.toml --output /tmp/quota-core-snapshot.json
python -m quota_core.cli dashboard --snapshot /tmp/quota-core-snapshot.json --output /tmp/quota-core-dashboard.html
```

After package installation:

```bash
quota-core init
quota-core demo
quota-core scan
quota-core dashboard
```

## Commands

### `quota-core init`

Writes a public-safe starter config.

```bash
quota-core init --config ~/.config/quota-core/config.toml
```

Live probes are disabled by default. Missing local provider paths should produce warnings, not crashes.

### `quota-core demo`

Renders a dashboard from bundled synthetic fixtures.

```bash
quota-core demo --output quota-core-demo.html
```

The demo does not read real provider logs and does not require credentials.

### `quota-core scan`

Reads public config and writes a normalized snapshot bundle.

```bash
quota-core scan --config ~/.config/quota-core/config.toml --output quota-core-snapshot.json
```

The current scan path supports config-driven local scanners for Claude, Codex, and Gemini. Missing provider paths produce schema-valid snapshots with actionable warnings.

### `quota-core dashboard`

Renders static dashboard HTML from a normalized snapshot file.

```bash
quota-core dashboard --snapshot quota-core-snapshot.json --output quota-core-dashboard.html
```

## Public Snapshot Contract

Dashboard and reporting code consume normalized snapshots only. They must not inspect raw Claude JSONL files, Codex SQLite databases, Gemini session files, credentials, Telegram settings, private bot registries, or internal paths.

The schema is documented in [docs/snapshot-schema.md](docs/snapshot-schema.md).

## Public vs Private Boundary

Public `quota_core` owns:

- config schema and loader
- CLI entrypoints
- provider adapter interfaces
- normalized snapshot schema
- window/reset/stale/cache helpers
- runtime tagging helpers that accept config as input
- dashboard GUI components
- synthetic fixtures and tests

Private ops owns:

- real credentials paths
- internal bot registry
- cron/watchdog/alert wiring
- Telegram settings
- real usage DBs and migration snapshots
- wrapper install scripts
- remote host settings

Dependency direction is one-way:

```text
private ops -> quota_core
```

`quota_core` must never import private ops.

See [docs/public-private-boundary.md](docs/public-private-boundary.md) for the full split contract.

## Privacy And Safety

`quota_core demo` performs no network calls and reads no real provider data.

`quota_core scan` should work from explicit local config. Live provider probes must be opt-in. Public fixtures should be synthetic, not copied from real logs.
