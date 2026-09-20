# Economics policy inputs (quota-core issue #80)

## `price_book.json`

A **synthetic** price book in the shape `--pricing` accepts:

    {provider: {model_version | "default": {component: per_token_rate}}}

⛔These are invented round numbers, not any vendor's real rates. quota-core
ships no price table on purpose: rates are commercial policy, differ per
account and change without notice, so a table baked into this repo would be
stale immediately and would silently mis-cost every consumer that trusted it.
The operator supplies real rates; this fixture only exercises the mechanism.

It deliberately covers three cases the resolver must get right:

1. `provider-a` / `model-x-2` — an exact model-version entry, which must win
   over the provider default.
2. `provider-a` / `default` — the provider-wide fallback for any other model.
3. `provider-b` — a provider that prices only two components. The other three
   stay **unknown (null)**, never zero.

No provider here corresponds to a real vendor and no identifier is private.
