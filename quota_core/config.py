"""Public-safe configuration loading for quota_core."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_TOML = """# quota_core public config

[dashboard]
host = "127.0.0.1"
port = 8088

[providers.claude]
enabled = true
live_probe = false

[providers.claude.paths]
projects_dir = "~/.claude/projects"

[providers.codex]
enabled = true
live_probe = false

[providers.codex.paths]
state_db = "~/.codex/state_5.sqlite"
sessions_dir = "~/.codex/sessions"

[providers.gemini]
enabled = true
live_probe = false

[providers.gemini.paths]
tmp_dir = "~/.gemini/tmp"

[runtime]

# Add runtime bots when you want subprocess calls tagged by cwd.
# [[runtime.bots]]
# name = "my-bot"
# paths = ["~/projects/my-bot"]
"""


@dataclass(frozen=True)
class ProviderConfig:
    """Configuration for one provider adapter."""

    enabled: bool = True
    paths: dict[str, str] = field(default_factory=dict)
    live_probe: bool = False


@dataclass(frozen=True)
class RuntimeBotConfig:
    """Public-safe runtime bot mapping."""

    name: str
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeConfig:
    """Runtime tagging configuration."""

    bots: tuple[RuntimeBotConfig, ...] = ()


@dataclass(frozen=True)
class DashboardConfig:
    """Dashboard server configuration."""

    host: str = "127.0.0.1"
    port: int = 8088


@dataclass(frozen=True)
class QuotaCoreConfig:
    """Top-level public quota_core configuration."""

    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)


def default_config_path() -> Path:
    """Return the default public config path."""

    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "quota-core" / "config.toml"
    return Path.home() / ".config" / "quota-core" / "config.toml"


def write_default_config(path: str | Path | None = None, *, force: bool = False) -> Path:
    """Write a starter config and return its path."""

    target = Path(path).expanduser() if path is not None else default_config_path()
    if target.exists() and not force:
        raise FileExistsError(f"Config already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DEFAULT_CONFIG_TOML)
    return target


def load_config(path: str | Path | None = None) -> QuotaCoreConfig:
    """Load config from TOML on disk."""

    target = Path(path).expanduser() if path is not None else default_config_path()
    if not target.exists():
        return QuotaCoreConfig()
    data = tomllib.loads(target.read_text())
    return config_from_mapping(data)


def config_from_mapping(data: dict[str, Any]) -> QuotaCoreConfig:
    """Build config from a TOML-compatible mapping."""

    providers = _parse_providers(data.get("providers", {}))
    runtime = _parse_runtime(data.get("runtime", {}))
    dashboard = _parse_dashboard(data.get("dashboard", {}))
    return QuotaCoreConfig(providers=providers, runtime=runtime, dashboard=dashboard)


def validate_config(config: QuotaCoreConfig) -> tuple[str, ...]:
    """Return non-fatal config warnings."""

    warnings: list[str] = []
    for provider_name, provider_config in sorted(config.providers.items()):
        if not provider_config.enabled:
            continue
        for key, raw_path in sorted(provider_config.paths.items()):
            candidate = Path(raw_path).expanduser()
            if not candidate.exists():
                warnings.append(f"provider {provider_name}: path {key} does not exist: {raw_path}")
    return tuple(warnings)


def _parse_providers(raw: Any) -> dict[str, ProviderConfig]:
    if not isinstance(raw, dict):
        return {}

    providers: dict[str, ProviderConfig] = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            continue
        paths_raw = value.get("paths", {})
        paths = {str(k): str(v) for k, v in paths_raw.items()} if isinstance(paths_raw, dict) else {}
        providers[str(name)] = ProviderConfig(
            enabled=bool(value.get("enabled", True)),
            paths=paths,
            live_probe=bool(value.get("live_probe", False)),
        )
    return providers


def _parse_runtime(raw: Any) -> RuntimeConfig:
    if not isinstance(raw, dict):
        return RuntimeConfig()

    bots_raw = raw.get("bots", [])
    bots: list[RuntimeBotConfig] = []
    if isinstance(bots_raw, list):
        for item in bots_raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            paths_raw = item.get("paths", [])
            paths = tuple(str(path) for path in paths_raw) if isinstance(paths_raw, list) else ()
            bots.append(RuntimeBotConfig(name=str(name), paths=paths))
    return RuntimeConfig(bots=tuple(bots))


def _parse_dashboard(raw: Any) -> DashboardConfig:
    if not isinstance(raw, dict):
        return DashboardConfig()

    host = str(raw.get("host", "127.0.0.1"))
    try:
        port = int(raw.get("port", 8088))
    except (TypeError, ValueError):
        port = 8088
    return DashboardConfig(host=host, port=port)
