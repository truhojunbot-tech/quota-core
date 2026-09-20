"""Provider/version-specific pricing adapters for task economics (#80).

⛔quota-core ships NO vendor price table. Prices are commercial policy that
  changes without notice and differs per account, so a table baked in here
  would be stale the day after it was written and would silently mis-cost
  every consumer that trusted it. This module provides the *mechanism*: a
  caller supplies rates, keyed by provider and model version, and gets back a
  per-component breakdown.

Three invariants this module exists to hold:

1. **No fabricated universal total.** Components are priced separately and
   never summed here. ``reasoning`` overlaps ``output`` on providers that
   report both (quota-core#78), so adding them double-counts; and a provider
   that exposes only some components would otherwise produce a "total" that
   silently means something different from another provider's.
2. **Unknown is never zero.** A missing rate, a missing token count, or a
   provider/model with no entry yields ``None``, not ``0.0``.
3. **Retry and failover cost is reported separately** from productive cost,
   using the producer's own ``retry_of``/``fallback_of`` lineage rather than
   any inference about why a task ran.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .policy import ComponentPrice, ProviderPricing
from .schema import TaskEconomicsRecord

#: The five separately-priced components, in a fixed order so output is stable.
PRICED_COMPONENTS: tuple[str, ...] = (
    "uncached_input", "cache_write", "cache_read", "output", "reasoning",
)

#: Telemetry field backing each priced component.
_COMPONENT_TOKEN_FIELD: dict[str, str] = {
    "uncached_input": "uncached_input_tokens",
    "cache_write": "cache_write_tokens",
    "cache_read": "cache_read_tokens",
    "output": "output_tokens",
    "reasoning": "reasoning_tokens",
}


def _rate(value: object) -> float | None:
    """A usable per-token rate, or ``None``. Rejects bool, negatives, non-finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 and isfinite(value) else None


def _tokens(value: object) -> int | None:
    """A usable token count, or ``None``. Rejects bool and negatives."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(frozen=True)
class PricingBook:
    """Caller-supplied rates, resolved per provider and model version.

    Lookup is exact-version-first: an entry for ``(provider, model)`` wins over
    a provider-wide default entry (``model=None``). A provider with neither
    yields ``None`` -- costs stay unknown rather than falling back to some
    other provider's rates, which would be a fabricated number wearing a real
    provider's name.
    """

    entries: tuple[ProviderPricing, ...] = ()

    @classmethod
    def from_mapping(cls, mapping: object) -> PricingBook:
        """Build from ``{provider: {model_or_"default": {component: rate}}}``.

        Tolerant by design, because this is operator-supplied configuration
        rather than producer telemetry: a malformed provider, model or rate is
        skipped, leaving those costs unknown, instead of raising in the middle
        of a report or coercing junk into a number.
        """
        if not isinstance(mapping, dict):
            return cls()
        entries: list[ProviderPricing] = []
        for provider, models in sorted(mapping.items(), key=lambda kv: str(kv[0])):
            if not isinstance(provider, str) or not provider or not isinstance(models, dict):
                continue
            for model, rates in sorted(models.items(), key=lambda kv: str(kv[0])):
                if not isinstance(rates, dict):
                    continue
                resolved = {
                    name: _rate(rates.get(name)) for name in PRICED_COMPONENTS
                }
                if all(value is None for value in resolved.values()):
                    continue
                entries.append(ProviderPricing(
                    provider=provider,
                    model=None if model in (None, "", "default", "*") else str(model),
                    price=ComponentPrice(**resolved),
                ))
        return cls(tuple(entries))

    def lookup(self, provider: str | None, model: str | None) -> ProviderPricing | None:
        """Exact ``(provider, model)`` first, then the provider-wide default."""
        if not provider:
            return None
        exact = [e for e in self.entries if e.provider == provider and e.model == model]
        if exact:
            return exact[0]
        default = [e for e in self.entries if e.provider == provider and e.model is None]
        return default[0] if default else None

    def for_record(self, record: TaskEconomicsRecord) -> ProviderPricing | None:
        return self.lookup(record.provider or record.agent, record.model)


@dataclass(frozen=True)
class TaskCostBreakdown:
    """Per-component cost for one task, with retry/failover kept separate.

    ⛔``productive`` and ``retry_or_failover`` are two views of the SAME task,
      never two tasks: exactly one of them is populated for a given record,
      chosen by the producer's own ``retry_of``/``fallback_of`` lineage. A
      caller that wants "what did retries cost us" sums the retry side across
      records; summing both sides of one record would double-count it.

    ⛔There is deliberately no total field. See this module's docstring.
    """

    task_id: str
    provider: str | None
    model: str | None
    priced: bool
    is_retry_or_failover: bool
    lineage: tuple[str, ...]
    components: dict[str, float | None]

    def to_dict(self) -> dict[str, object]:
        side = "retry_or_failover" if self.is_retry_or_failover else "productive"
        return {
            "task_id": self.task_id,
            "provider": self.provider,
            "model": self.model,
            "priced": self.priced,
            "cost_attribution": side,
            "lineage": list(self.lineage),
            "productive": dict(self.components) if not self.is_retry_or_failover else _unknown_components(),
            "retry_or_failover": dict(self.components) if self.is_retry_or_failover else _unknown_components(),
            "non_additive_components": ["reasoning"],
            "aggregation": "prohibited_overlapping_components",
        }


def _unknown_components() -> dict[str, None]:
    return {name: None for name in PRICED_COMPONENTS}


def price_task(record: TaskEconomicsRecord, book: PricingBook | None) -> TaskCostBreakdown:
    """Cost one task's measured components at the caller's rates.

    A record with no matching provider/model entry, or with unknown token
    counts, yields ``None`` components and ``priced=False``. It is never
    silently costed at another provider's rates or at zero.
    """
    pricing = book.for_record(record) if book is not None else None
    lineage = tuple(
        label for label, value in (("retry_of", record.retry_of), ("fallback_of", record.fallback_of))
        if value
    )
    components: dict[str, float | None] = {}
    for name in PRICED_COMPONENTS:
        tokens = _tokens(getattr(record.task_telemetry, _COMPONENT_TOKEN_FIELD[name]))
        rate = _rate(getattr(pricing.price, name)) if pricing is not None else None
        if tokens is None or rate is None:
            components[name] = None
            continue
        try:
            cost = tokens * rate
        except OverflowError:
            components[name] = None
            continue
        components[name] = cost if isfinite(cost) else None
    return TaskCostBreakdown(
        task_id=record.task_id,
        provider=record.provider or record.agent,
        model=record.model,
        priced=pricing is not None,
        is_retry_or_failover=bool(lineage),
        lineage=lineage,
        components=components,
    )
