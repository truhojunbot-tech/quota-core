# Changelog

## 0.1.8 - 2026-04-29

Dashboard structure refactor.

- Added a dashboard view model that maps snapshots into the original operations report structure before rendering.
- Moved provider window pairing, primary-window selection, reset ordering, and data-state summaries out of HTML components.
- Added regression coverage for Claude/Codex-style 5-hour + 7-day and Gemini-style current quota + 7-day grouping.

## 0.1.7 - 2026-04-29

Gemini quota window alignment.

- Pairs `current_quota` with `seven_day` in provider cards and detail sections when a provider has no 5-hour window.
- Keeps per-window app lists side by side for Gemini-style quota snapshots.

## 0.1.6 - 2026-04-29

Dashboard provider-detail split view.

- Added a side-by-side 5-hour and 7-day layout in provider detail sections.
- Lists each quota window's top apps inside its own detail column.

## 0.1.5 - 2026-04-29

Dashboard provider-card progress bars.

- Added separate 5-hour and 7-day progress bars in provider summary cards.
- Removed the single combined provider-card bar when quota windows are available.

## 0.1.4 - 2026-04-29

Dashboard short-window visibility fix.

- Restored 5-hour quota visibility in provider summary cards instead of showing only the highest-pressure window.
- Provider cards now show both 5-hour and 7-day rows when both quota windows are available.

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