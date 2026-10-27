These wholly synthetic task rows model stale and misrouted review cascades.
`review_publication=stale` and `result_branch`/`result_commit` are the durable
producer signals. The fixture also includes a later review round whose parent
is an earlier review: a changed reviewed SHA on the same branch is not a
misroute.
