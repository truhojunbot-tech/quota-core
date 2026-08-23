# AGY incident regression fixture (quota-core issue #60)

`attribution.jsonl` here models the real `agent_crew#205`/`#206` incident
referenced in issue #60: 31 tester failures over ~21 hours caused by AGY's
transient `subscriber fell behind updates` streaming/backpressure error,
which agent_crew's dispatcher had not (at the time) classified as
retriable.

Unlike `tests/fixtures/agent_crew/real_contract/`, this fixture is **not**
sanitized real production output. To be precise about what is and isn't
confirmed (corrected in round-1 review of issue #60 -- the previous wording
here wrongly implied the tag itself was unconfirmed/hypothetical):
`agy_subscriber_lag` **is** a real, shipped tag in agent_crew's own
dispatcher -- defined in `server.py:258-262`'s docstring and implemented at
`server.py:299-300` (`if "subscriber fell behind updates" in tail: return
"agy_subscriber_lag"`). What is actually true is only that no task has
*failed* with that reason in any locally observed
`~/.agent_crew/*/attribution.jsonl` or `tasks.db` `error_info` value as of
this change (see `quota_core/context_economics/schema.py`'s module comments
above `_PROVIDER_TRANSPORT_MARKERS` for what *was* observed:
`dispatcher_timeout`, `exit_1`, `agy_quota_exhausted`, `no_result_submitted`,
`transient_agy_timeout_max_retries`, `transient_agy_subscriber_lag_max_retries`).
This fixture is instead constructed to match:

1. the real, observed `"failed:<reason>"` colon-delimited encoding
   `agent_crew_adapter.py`/`schema.normalize_outcome` already parse, and
2. the exact literal tag agent_crew's own dispatcher uses for this
   transient-error case (`agy_subscriber_lag` -- confirmed via read-only
   inspection of the local `agent_crew` checkout's
   `tests/unit/test_transient_error_detection.py` and
   `_detect_transient_error_in_log`). Note this is matched in
   `schema.py`'s `_PROVIDER_TRANSPORT_MARKERS` as a literal substring
   (`"agy_subscriber"`), not a true `agy_`-prefix rule -- see that module's
   comments for why a broader prefix match was deliberately not implemented.

It exists to prove `classify_failure_category` puts
`"failed:agy_subscriber_lag"` into `"provider_or_transport"` (not
`"context_or_policy"`), and that `stratified_failure_rates`/
`context_age_vs_failure_rate` exclude it from `policy_relevant_failure_rate`
while still counting it in `raw_failure_rate` -- i.e. this incident does not
get interpreted as context-policy evidence, matching issue #60's acceptance
criteria.

Shape: 12 single-row (already-terminal) tester tasks sharing one
`context_id`/`provider_session_id`, `session_task_index` 1-12 (resume
policy). 6 succeed, 4 fail with `"failed:agy_subscriber_lag"`, 1 fails with
`"failed:exit_1"` (a real observed but evidence-free bare exit code,
included for contrast -- classifies as `"unknown"`, not
`"work_product_or_test"`, per round-1 review of issue #60: a bare exit code
alone is not positive evidence of a test/lint/assertion failure), and 1
fails with the bare `"failed"` backward-compat shape (no reason at all,
also `"unknown"`).
