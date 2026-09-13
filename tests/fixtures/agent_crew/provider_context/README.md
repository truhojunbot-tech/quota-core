# `provider_context_observed` / `provider_context_capped` fixtures

⛔**fixture-validated / production-sample pending.**

These lines are shaped from the producer's emitter as merged, not captured from
a deployed fleet. The producer ships the token cap disabled by default, so the
*observation* path only begins writing rows once a runtime carrying it is
actually deployed and dispatches. Until a real organic sample exists, nothing in
this consumer may be used to claim end-to-end context economics — see
`docs/context_economics.md`.

## What each line covers

| file | line | shape |
|---|---|---|
| `observations.jsonl` | `task-claude-positive` | a real window, measured |
| | `task-claude-zero` | **measured zero** — must not read as unknown |
| | `task-claude-unreadable` | measurement attempted, `context_tokens: null` |
| | `task-agy` | a provider with no token measurement at all (`null`) |
| | `task-codex` | likewise |
| | `task-capped` | a cap-triggered dispatch, which arrives as the *other* event |
| `duplicated.jsonl` | `task-both` | one dispatch with **both** events — contaminated or historical data |

## Field notes

- The capped event names the session `conversation_id` and the store `bytes`;
  the observed event names them `provider_session_id` and `context_bytes`. The
  parser normalises both, and neither is invented when absent.
- `context_tokens: null` is **unknown**; `0` is a measured empty window. Forcing
  one into the other is the single thing this consumer must never do.
- `cap_tokens: 0` means the token cap is disabled, not that the cap is zero.
