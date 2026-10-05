These redacted task rows model the two 2026-09-14 incidents in quota-core#74.
`review_publication=stale` is the durable #304 signal; `result_branch` and
`result_commit` are the durable #305/#348 result refs. The historical incident
rows predate these producer fixes, so the fixture uses their post-fix storage
shape without claiming the original rows carry these fields.
