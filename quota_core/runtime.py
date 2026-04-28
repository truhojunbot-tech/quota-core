"""Public-safe runtime tagging helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class RuntimeMatch:
    """Result of matching a cwd against runtime bot config."""

    usage_class: str | None = None
    bot_name: str | None = None


def match_runtime_bot(cwd: str, bot_paths: Mapping[str, tuple[str, ...]]) -> RuntimeMatch:
    """Match a working directory against configured bot paths."""

    try:
        cwd_path = Path(cwd).expanduser().resolve()
    except Exception:
        return RuntimeMatch()

    for bot_name, paths in bot_paths.items():
        for raw_path in paths:
            try:
                bot_path = Path(raw_path).expanduser().resolve()
            except Exception:
                continue
            if cwd_path == bot_path or bot_path in cwd_path.parents:
                return RuntimeMatch(usage_class="runtime", bot_name=bot_name)
    return RuntimeMatch()


def runtime_env(cwd: str, bot_paths: Mapping[str, tuple[str, ...]], base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment with runtime tags applied when configured."""

    env = dict(base_env or os.environ)
    if env.get("LLM_USAGE_CLASS") or env.get("BOT_NAME"):
        return env

    match = match_runtime_bot(cwd, bot_paths)
    if match.usage_class:
        env.setdefault("LLM_USAGE_CLASS", match.usage_class)
    if match.bot_name:
        env.setdefault("BOT_NAME", match.bot_name)
    return env
