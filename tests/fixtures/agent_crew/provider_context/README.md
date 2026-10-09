# `provider_context_observed` / `provider_context_capped` fixtures

The original fixtures are producer-shaped. `organic-quota-ops-events.jsonl`
contains two unmodified deployed rows, and `organic-quota-ops-attribution.jsonl`
contains their matching unmodified terminal attribution rows, captured on
2026-10-09. The deployed producer build was `32c5b8f`, a descendant of
`84e150e` (verified with the Agent Crew commit graph).

The older `observations.jsonl` and `duplicated.jsonl` lines were shaped from
the merged producer. The organic pair proves parse → identity join → serialized
task economics for a real positive observation and a real cap event; it does
not establish comparative resume/fresh/reset economics.

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
| `organic-quota-ops-events.jsonl` | `review-impl-909ae895-r0` | real cap event: 341834 tokens, 15042732 bytes; pre-cap session differs from fresh task session |
| | `review-6bc835dd` | real observed event: 146555 tokens, 9148303 bytes |

## Field notes

- The capped event names the session `conversation_id` and the store `bytes`;
  the observed event names them `provider_session_id` and `context_bytes`. The
  parser normalises both, and neither is invented when absent.
- `context_tokens: null` is **unknown**; `0` is a measured empty window. Forcing
  one into the other is the single thing this consumer must never do.
- `cap_tokens: 0` means the token cap is disabled, not that the cap is zero.
