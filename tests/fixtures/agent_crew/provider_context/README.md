# `provider_context_observed` / `provider_context_capped` fixtures

All rows are synthetic and producer-shaped. `synthetic-events.jsonl` pairs a
cap event with an observed event; `synthetic-attribution.jsonl` supplies their
matching terminal tasks. IDs, timestamps, paths and measurements are invented.

The `observations.jsonl` and `duplicated.jsonl` lines are also synthetic. The
paired rows prove parse → identity join → serialized task economics for a
positive observation and a cap event; they do
not establish comparative resume/fresh/reset economics.

## What each line covers

| file | line | shape |
|---|---|---|
| `observations.jsonl` | `task-claude-positive` | a positive synthetic measurement |
| | `task-claude-zero` | **measured zero** — must not read as unknown |
| | `task-claude-unreadable` | measurement attempted, `context_tokens: null` |
| | `task-agy` | a provider with no token measurement at all (`null`) |
| | `task-codex` | likewise |
| | `task-capped` | a cap-triggered dispatch, which arrives as the *other* event |
| `duplicated.jsonl` | `task-both` | one dispatch with **both** events — contaminated or historical data |
| `synthetic-events.jsonl` | `review-cap-sample` | cap event: 3418 pre-cap tokens, 15040 pre-cap bytes; pre-cap session differs from fresh task session |
| | `review-observed-sample` | observed event: 1465 tokens, 9148 bytes |

## Field notes

- The capped event names the session `conversation_id` and the store `bytes`;
  the observed event names them `provider_session_id` and `context_bytes`. The
  parser normalises both, and neither is invented when absent.
- `context_tokens: null` is **unknown**; `0` is a measured empty window. Forcing
  one into the other is the single thing this consumer must never do.
- `cap_tokens: 0` means the token cap is disabled, not that the cap is zero.
- A capped measurement belongs to the discarded predecessor, so the joined
  fresh task stores it in `pre_cap_context_tokens`/`pre_cap_context_bytes`;
  its dispatch-window `context_tokens`/`context_bytes` stay unknown.
