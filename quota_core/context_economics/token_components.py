"""Per-provider extraction of :class:`TokenComponents` from raw usage dicts.

Each provider exposes a different (and differently complete) breakdown of
what it billed. These helpers translate each provider's native usage shape
into the shared :class:`~quota_core.context_economics.schema.TokenComponents`
without collapsing distinct economic categories into one number, and without
inventing a component a provider did not actually report.
"""

from __future__ import annotations

from typing import Any

from .schema import TokenComponents


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def claude_token_components(usage: dict[str, Any]) -> TokenComponents:
    """Claude Messages API ``usage`` block -> :class:`TokenComponents`.

    Claude is the one provider quota-core already observes with a full
    breakdown (``input_tokens``, ``output_tokens``,
    ``cache_read_input_tokens``, ``cache_creation_input_tokens``); this keeps
    those four separate instead of summing them into a single total.
    """

    fresh_input = _opt_int(usage.get("input_tokens"))
    output = _opt_int(usage.get("output_tokens"))
    cache_read = _opt_int(usage.get("cache_read_input_tokens"))
    cache_creation = _opt_int(usage.get("cache_creation_input_tokens"))
    known = [v for v in (fresh_input, output, cache_read, cache_creation) if v is not None]
    provider_total = _opt_int(usage.get("total_tokens"))
    if provider_total is None and known:
        provider_total = sum(known)
    return TokenComponents(
        fresh_input=fresh_input,
        output=output,
        cache_read=cache_read,
        cache_creation=cache_creation,
        tool_tokens=None,
        provider_total=provider_total,
    )


def codex_token_components(usage: dict[str, Any]) -> TokenComponents:
    """Codex/OpenAI usage block -> :class:`TokenComponents`.

    Codex CLI telemetry commonly exposes input/output tokens and sometimes a
    cached-input count, but not a separate cache-write figure. Unreported
    components stay ``None`` -- they are not assumed to be zero.
    """

    fresh_input = _opt_int(usage.get("input_tokens"))
    cached_input = _opt_int(usage.get("cached_input_tokens") or usage.get("input_tokens_cached"))
    output = _opt_int(usage.get("output_tokens"))
    reasoning = _opt_int(usage.get("reasoning_tokens") or usage.get("output_tokens_reasoning"))
    provider_total = _opt_int(usage.get("total_tokens"))
    if provider_total is None:
        known = [v for v in (fresh_input, output) if v is not None]
        if known:
            provider_total = sum(known) + (cached_input or 0)
    return TokenComponents(
        fresh_input=fresh_input,
        output=output,
        cache_read=cached_input,
        cache_creation=None,
        tool_tokens=reasoning,
        provider_total=provider_total,
    )


def gemini_token_components(usage: dict[str, Any]) -> TokenComponents:
    """Gemini usage block -> :class:`TokenComponents`.

    Gemini's local telemetry typically exposes only a combined total; keep
    the component fields explicitly unknown rather than guessing a split.
    """

    fresh_input = _opt_int(usage.get("prompt_token_count") or usage.get("input_tokens"))
    output = _opt_int(usage.get("candidates_token_count") or usage.get("output_tokens"))
    cache_read = _opt_int(usage.get("cached_content_token_count"))
    provider_total = _opt_int(usage.get("total_token_count") or usage.get("total_tokens"))
    if provider_total is None:
        known = [v for v in (fresh_input, output) if v is not None]
        if known:
            provider_total = sum(known)
    return TokenComponents(
        fresh_input=fresh_input,
        output=output,
        cache_read=cache_read,
        cache_creation=None,
        tool_tokens=None,
        provider_total=provider_total,
    )


_PROVIDER_EXTRACTORS = {
    "claude": claude_token_components,
    "codex": codex_token_components,
    "gemini": gemini_token_components,
}


def token_components_for_provider(provider: str, usage: dict[str, Any]) -> TokenComponents:
    """Dispatch to the matching provider extractor; unknown providers keep only the total."""

    extractor = _PROVIDER_EXTRACTORS.get((provider or "").lower())
    if extractor is not None:
        return extractor(usage)
    total = _opt_int(usage.get("total_tokens"))
    return TokenComponents(provider_total=total)


def merge_token_components(components: list[TokenComponents]) -> TokenComponents:
    """Sum multiple :class:`TokenComponents`, keeping a field ``None`` only if *all* inputs lack it."""

    def _sum(attr: str) -> int | None:
        values = [getattr(c, attr) for c in components]
        known = [v for v in values if v is not None]
        if not known:
            return None
        return sum(known)

    return TokenComponents(
        fresh_input=_sum("fresh_input"),
        output=_sum("output"),
        cache_read=_sum("cache_read"),
        cache_creation=_sum("cache_creation"),
        tool_tokens=_sum("tool_tokens"),
        provider_total=_sum("provider_total"),
    )


__all__ = [
    "claude_token_components",
    "codex_token_components",
    "gemini_token_components",
    "token_components_for_provider",
    "merge_token_components",
]
