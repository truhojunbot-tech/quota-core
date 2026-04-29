# Changelog

## 0.1.3 - 2026-04-29

Dashboard information architecture update.

- Added a command-center summary for highest pressure, next reset, and live/cached data state.
- Added a quota matrix that compares provider windows by utilization, tokens, reset, pace, top project, and cache state.
- Added a reset schedule and per-window context for range, sampled time, top project, and top model.
- Added regression coverage for the operational quota context sections.

## 0.1.2 - 2026-04-29

Operations dashboard correction.

- Separated local-history snapshots from live quota windows in the dashboard UI.
- Added attention and operations briefing sections for quota pressure, warnings, and project concentration.
- Made provider summary cards choose the highest-pressure quota window instead of always preferring the shortest window.
- Added regression coverage so local-only scans do not render as misleading `0.0%` quota utilization.

## 0.1.1 - 2026-04-29

Dashboard usability update.

- Reworked the dashboard into an operations-focused layout with provider summary cards.
- Added compact token/request metrics, share bars, and top-project truncation.
- Shortened local Claude project labels for easier scanning while preserving full names in tooltips.
- Added responsive dashboard layout rules for smaller screens.

## 0.1.0 - 2026-04-29

Initial public release preparation.

- Added public `quota-core` package metadata and CLI entrypoint.
- Added config-driven local scanners for Claude, Codex, and Gemini.
- Added normalized snapshot schema and dashboard HTML rendering.
- Added runtime tagging helpers for private overlays that pass explicit config.
- Added public/private boundary documentation and regression tests.
- Hardened local scanner warnings and bounded local file reads.