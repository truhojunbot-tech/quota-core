"""Provider adapter interfaces for quota_core."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .config import ProviderConfig
from .snapshot import NormalizedSnapshot


@dataclass(frozen=True)
class ProviderContext:
    """Runtime context passed to provider adapters."""

    name: str
    config: ProviderConfig


class ProviderAdapter(Protocol):
    """Protocol implemented by provider adapters."""

    source: str

    def scan(self, context: ProviderContext) -> NormalizedSnapshot:
        """Return a normalized provider snapshot."""


def enabled_provider_names(configs: dict[str, ProviderConfig]) -> tuple[str, ...]:
    """Return enabled provider names in stable order."""

    return tuple(name for name, provider_config in sorted(configs.items()) if provider_config.enabled)
