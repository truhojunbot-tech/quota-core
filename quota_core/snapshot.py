"""Normalized public snapshot types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CacheState = Literal["live", "cached", "stale", "unknown"]


@dataclass(frozen=True)
class AggregateBreakdown:
    """Token and request aggregate for a project or model."""

    total_tokens: int = 0
    requests: int = 0
    share_pct: float = 0.0
    models: dict[str, int] = field(default_factory=dict)
    model_requests: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeBreakdown:
    """Runtime-only aggregate for a window."""

    total_tokens: int = 0
    requests: int = 0
    by_project: dict[str, AggregateBreakdown] = field(default_factory=dict)
    by_model: dict[str, AggregateBreakdown] = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotQuotaGroup:
    """Provider quota group sharing one limit/reset window."""

    label: str
    utilization: float = 0.0
    resets_at: int | None = None
    token_type: str = ""
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotWindow:
    """Normalized provider window."""

    window_start: int | None = None
    window_end: int | None = None
    resets_at: int | None = None
    utilization: float = 0.0
    total_tokens: int = 0
    requests: int = 0
    by_project: dict[str, AggregateBreakdown] = field(default_factory=dict)
    by_model: dict[str, AggregateBreakdown] = field(default_factory=dict)
    quota_groups: dict[str, SnapshotQuotaGroup] = field(default_factory=dict)
    runtime: RuntimeBreakdown = field(default_factory=RuntimeBreakdown)
    cache_state: CacheState = "unknown"
    stale: bool = False


@dataclass(frozen=True)
class NormalizedSnapshot:
    """Normalized public provider snapshot."""

    source: str
    sampled_at: int
    windows: dict[str, SnapshotWindow] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    history: dict[str, Any] = field(default_factory=dict)


def empty_snapshot(source: str, sampled_at: int) -> NormalizedSnapshot:
    """Create an empty normalized snapshot."""

    return NormalizedSnapshot(source=source, sampled_at=sampled_at)


def aggregate_to_dict(aggregate: AggregateBreakdown) -> dict[str, Any]:
    """Convert an aggregate breakdown to public JSON shape."""

    return {
        "total_tokens": aggregate.total_tokens,
        "requests": aggregate.requests,
        "share_pct": aggregate.share_pct,
        "models": dict(aggregate.models),
        "model_requests": dict(aggregate.model_requests),
    }


def runtime_to_dict(runtime: RuntimeBreakdown) -> dict[str, Any]:
    """Convert runtime breakdown to public JSON shape."""

    return {
        "total_tokens": runtime.total_tokens,
        "requests": runtime.requests,
        "by_project": {key: aggregate_to_dict(value) for key, value in runtime.by_project.items()},
        "by_model": {key: aggregate_to_dict(value) for key, value in runtime.by_model.items()},
    }


def quota_group_to_dict(group: SnapshotQuotaGroup) -> dict[str, Any]:
    """Convert a quota group to public JSON shape."""

    return {
        "label": group.label,
        "utilization": group.utilization,
        "resets_at": group.resets_at,
        "token_type": group.token_type,
        "models": list(group.models),
    }


def window_to_dict(window: SnapshotWindow) -> dict[str, Any]:
    """Convert a snapshot window to public JSON shape."""

    return {
        "window_start": window.window_start,
        "window_end": window.window_end,
        "resets_at": window.resets_at,
        "utilization": window.utilization,
        "total_tokens": window.total_tokens,
        "requests": window.requests,
        "by_project": {key: aggregate_to_dict(value) for key, value in window.by_project.items()},
        "by_model": {key: aggregate_to_dict(value) for key, value in window.by_model.items()},
        "quota_groups": {key: quota_group_to_dict(value) for key, value in window.quota_groups.items()},
        "runtime": runtime_to_dict(window.runtime),
        "cache_state": window.cache_state,
        "stale": window.stale,
    }


def snapshot_to_dict(snapshot: NormalizedSnapshot) -> dict[str, Any]:
    """Convert a normalized snapshot to public JSON shape."""

    return {
        "source": snapshot.source,
        "sampled_at": snapshot.sampled_at,
        "windows": {key: window_to_dict(value) for key, value in snapshot.windows.items()},
        "errors": list(snapshot.errors),
        "warnings": list(snapshot.warnings),
        "history": dict(snapshot.history),
    }


def aggregate_from_dict(data: dict[str, Any]) -> AggregateBreakdown:
    """Build an aggregate breakdown from public JSON shape."""

    return AggregateBreakdown(
        total_tokens=int(data.get("total_tokens") or 0),
        requests=int(data.get("requests") or 0),
        share_pct=float(data.get("share_pct") or 0.0),
        models=_int_dict(data.get("models", {})),
        model_requests=_int_dict(data.get("model_requests", {})),
    )


def runtime_from_dict(data: dict[str, Any]) -> RuntimeBreakdown:
    """Build a runtime breakdown from public JSON shape."""

    return RuntimeBreakdown(
        total_tokens=int(data.get("total_tokens") or 0),
        requests=int(data.get("requests") or 0),
        by_project=_aggregate_map(data.get("by_project", {})),
        by_model=_aggregate_map(data.get("by_model", {})),
    )


def quota_group_from_dict(data: dict[str, Any]) -> SnapshotQuotaGroup:
    """Build a quota group from public JSON shape."""

    models = data.get("models", [])
    return SnapshotQuotaGroup(
        label=str(data.get("label") or ""),
        utilization=float(data.get("utilization") or 0.0),
        resets_at=data.get("resets_at"),
        token_type=str(data.get("token_type") or ""),
        models=tuple(str(model) for model in models if model) if isinstance(models, list) else (),
    )


def window_from_dict(data: dict[str, Any]) -> SnapshotWindow:
    """Build a snapshot window from public JSON shape."""

    cache_state = data.get("cache_state", "unknown")
    if cache_state not in {"live", "cached", "stale", "unknown"}:
        cache_state = "unknown"
    return SnapshotWindow(
        window_start=data.get("window_start"),
        window_end=data.get("window_end"),
        resets_at=data.get("resets_at"),
        utilization=float(data.get("utilization") or 0.0),
        total_tokens=int(data.get("total_tokens") or 0),
        requests=int(data.get("requests") or 0),
        by_project=_aggregate_map(data.get("by_project", {})),
        by_model=_aggregate_map(data.get("by_model", {})),
        quota_groups={
            str(name): quota_group_from_dict(group)
            for name, group in data.get("quota_groups", {}).items()
            if isinstance(group, dict)
        } if isinstance(data.get("quota_groups", {}), dict) else {},
        runtime=runtime_from_dict(data.get("runtime", {})) if isinstance(data.get("runtime", {}), dict) else RuntimeBreakdown(),
        cache_state=cache_state,  # type: ignore[arg-type]
        stale=bool(data.get("stale", False)),
    )


def snapshot_from_dict(data: dict[str, Any]) -> NormalizedSnapshot:
    """Build a normalized snapshot from public JSON shape."""

    windows_raw = data.get("windows", {})
    windows = {
        str(name): window_from_dict(window)
        for name, window in windows_raw.items()
        if isinstance(window, dict)
    } if isinstance(windows_raw, dict) else {}
    return NormalizedSnapshot(
        source=str(data.get("source") or "unknown"),
        sampled_at=int(data.get("sampled_at") or 0),
        windows=windows,
        errors=tuple(str(error) for error in data.get("errors", []) if error),
        warnings=tuple(str(warning) for warning in data.get("warnings", []) if warning),
        history=data.get("history", {}) if isinstance(data.get("history", {}), dict) else {},
    )


def validate_snapshot_dict(snapshot: dict[str, Any]) -> tuple[str, ...]:
    """Return schema validation errors for public snapshot JSON."""

    errors: list[str] = []
    if not isinstance(snapshot.get("source"), str) or not snapshot.get("source"):
        errors.append("source must be a non-empty string")
    if not isinstance(snapshot.get("sampled_at"), int):
        errors.append("sampled_at must be an integer unix timestamp")
    if "history" in snapshot and not isinstance(snapshot.get("history"), dict):
        errors.append("history must be an object")

    windows = snapshot.get("windows")
    if not isinstance(windows, dict):
        errors.append("windows must be an object")
        return tuple(errors)

    for window_name, window in windows.items():
        prefix = f"windows.{window_name}"
        if not isinstance(window_name, str) or not window_name:
            errors.append("window names must be non-empty strings")
        if not isinstance(window, dict):
            errors.append(f"{prefix} must be an object")
            continue
        _validate_optional_int(window, "window_start", prefix, errors)
        _validate_optional_int(window, "window_end", prefix, errors)
        _validate_optional_int(window, "resets_at", prefix, errors)
        if not isinstance(window.get("utilization", 0.0), (int, float)):
            errors.append(f"{prefix}.utilization must be numeric")
        if not isinstance(window.get("total_tokens", 0), int):
            errors.append(f"{prefix}.total_tokens must be an integer")
        if not isinstance(window.get("requests", 0), int):
            errors.append(f"{prefix}.requests must be an integer")
        quota_groups = window.get("quota_groups", {})
        if not isinstance(quota_groups, dict):
            errors.append(f"{prefix}.quota_groups must be an object")
        else:
            for group_name, group in quota_groups.items():
                group_prefix = f"{prefix}.quota_groups.{group_name}"
                if not isinstance(group, dict):
                    errors.append(f"{group_prefix} must be an object")
                    continue
                if not isinstance(group.get("label", ""), str):
                    errors.append(f"{group_prefix}.label must be a string")
                if not isinstance(group.get("utilization", 0.0), (int, float)):
                    errors.append(f"{group_prefix}.utilization must be numeric")
                _validate_optional_int(group, "resets_at", group_prefix, errors)
                if not isinstance(group.get("models", []), list):
                    errors.append(f"{group_prefix}.models must be a list")
        cache_state = window.get("cache_state", "unknown")
        if cache_state not in {"live", "cached", "stale", "unknown"}:
            errors.append(f"{prefix}.cache_state must be live, cached, stale, or unknown")
        if not isinstance(window.get("stale", False), bool):
            errors.append(f"{prefix}.stale must be a boolean")
        for key in ("by_project", "by_model"):
            if not isinstance(window.get(key, {}), dict):
                errors.append(f"{prefix}.{key} must be an object")
        if not isinstance(window.get("runtime", {}), dict):
            errors.append(f"{prefix}.runtime must be an object")
    return tuple(errors)


def _validate_optional_int(window: dict[str, Any], key: str, prefix: str, errors: list[str]) -> None:
    value = window.get(key)
    if value is not None and not isinstance(value, int):
        errors.append(f"{prefix}.{key} must be an integer or null")


def _aggregate_map(raw: Any) -> dict[str, AggregateBreakdown]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): aggregate_from_dict(value)
        for name, value in raw.items()
        if isinstance(value, dict)
    }


def _int_dict(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        try:
            result[str(key)] = int(value or 0)
        except (TypeError, ValueError):
            continue
    return result
