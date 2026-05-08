"""Project-name normalization shared by provider adapters."""

from __future__ import annotations

from quota_core.snapshot import AggregateBreakdown

PROVIDER_SUFFIXES = {"claude", "codex", "gemini"}


def normalize_project_name(raw: object) -> str:
    """Return a stable project name for local paths and agent_crew worktrees."""

    name = str(raw or "unknown")
    path = name.replace("\\", "/")
    parts = [part for part in path.split("/") if part]

    if "worktrees" in parts:
        index = parts.index("worktrees") + 1
        provider = parts[index + 1].lower() if index + 1 < len(parts) else ""
        if index < len(parts) and provider in PROVIDER_SUFFIXES:
            return _canonical_project(parts[index])

    for marker in (".agent_crew", "agent_crew", "agent-crew"):
        if marker in parts:
            index = parts.index(marker) + 1
            if index + 2 < len(parts) and parts[index].lower() == "worktrees" and parts[index + 2].lower() in PROVIDER_SUFFIXES:
                return _canonical_project(parts[index + 1])
            if index + 1 < len(parts) and parts[index + 1].lower() in PROVIDER_SUFFIXES:
                return _canonical_project(parts[index])

    if parts and parts[-1].lower() in PROVIDER_SUFFIXES and len(parts) >= 2:
        return _canonical_project(parts[-2])

    candidate = parts[-1] if parts else name
    normalized = _normalize_encoded_agent_crew(candidate)
    if normalized != candidate:
        return _canonical_project(normalized)
    return _canonical_project(candidate) if parts else _canonical_project(name)


def merge_project_breakdown(
    projects: dict[str, AggregateBreakdown],
    project_name: object,
    *,
    tokens: int,
    requests: int,
    models: dict[str, int] | None = None,
    model_requests: dict[str, int] | None = None,
) -> None:
    """Add one project row into a normalized aggregate map."""

    normalized_name = normalize_project_name(project_name)
    current = projects.get(normalized_name, AggregateBreakdown())
    merged_models = dict(current.models)
    for model, model_tokens in (models or {}).items():
        merged_models[model] = merged_models.get(model, 0) + int(model_tokens)
    merged_model_requests = dict(current.model_requests)
    for model, count in (model_requests or {}).items():
        merged_model_requests[model] = merged_model_requests.get(model, 0) + int(count)
    projects[normalized_name] = AggregateBreakdown(
        total_tokens=current.total_tokens + tokens,
        requests=current.requests + requests,
        models=merged_models,
        model_requests=merged_model_requests,
    )


def finalize_project_aggregates(
    projects: dict[str, AggregateBreakdown],
    total_tokens: int,
) -> dict[str, AggregateBreakdown]:
    """Sort projects and recompute shares after normalization merges."""

    return {
        project: AggregateBreakdown(
            total_tokens=aggregate.total_tokens,
            requests=aggregate.requests,
            share_pct=round(aggregate.total_tokens / total_tokens * 100, 1) if total_tokens else 0.0,
            models=aggregate.models,
            model_requests=aggregate.model_requests,
        )
        for project, aggregate in sorted(projects.items(), key=lambda item: -item[1].total_tokens)
    }


def project_aggregates_from_raw(raw: object, total_tokens: int) -> dict[str, AggregateBreakdown]:
    """Normalize a provider raw project breakdown into sorted public aggregates."""

    if not isinstance(raw, dict):
        return {}

    aggregates: dict[str, AggregateBreakdown] = {}
    for project, value in raw.items():
        if not isinstance(value, dict):
            continue
        merge_project_breakdown(
            aggregates,
            project,
            tokens=_coerce_int(value.get("total") or value.get("tokens") or value.get("total_tokens")),
            requests=_coerce_int(value.get("requests")),
            models=_int_map(value.get("models", {})),
            model_requests=_int_map(value.get("model_requests", {})),
        )
    return finalize_project_aggregates(aggregates, total_tokens)


def project_aggregates_with_runtime_extras(
    raw_projects: object,
    raw_runtime_projects: object,
    total_tokens: int,
) -> dict[str, AggregateBreakdown]:
    """Normalize main projects and include runtime-only projects in the same denominator."""

    merged = dict(raw_projects) if isinstance(raw_projects, dict) else {}
    main_project_names = {normalize_project_name(project) for project in merged}
    if isinstance(raw_runtime_projects, dict):
        for project, value in raw_runtime_projects.items():
            normalized_project = normalize_project_name(project)
            if normalized_project not in main_project_names:
                merged[project] = value
                main_project_names.add(normalized_project)
    return project_aggregates_from_raw(merged, total_tokens)


def model_aggregates_from_projects(
    projects: dict[str, AggregateBreakdown],
    total_tokens: int,
) -> dict[str, AggregateBreakdown]:
    """Roll normalized project aggregates up into a model breakdown."""

    model_tokens: dict[str, int] = {}
    model_requests: dict[str, int] = {}
    for project in projects.values():
        for model, tokens in project.models.items():
            model_tokens[model] = model_tokens.get(model, 0) + int(tokens)
        for model, requests in project.model_requests.items():
            model_requests[model] = model_requests.get(model, 0) + int(requests)
    return {
        model: AggregateBreakdown(
            total_tokens=tokens,
            requests=model_requests.get(model, 0),
            share_pct=round(tokens / total_tokens * 100, 1) if total_tokens else 0.0,
        )
        for model, tokens in sorted(model_tokens.items(), key=lambda item: -item[1])
    }


def _normalize_encoded_agent_crew(name: str) -> str:
    tokens = [token for token in name.strip("-").split("-") if token]
    lower = [token.lower() for token in tokens]
    for marker in (("agent", "crew", "worktrees"), ("agent", "crew"), ("worktrees",)):
        marker_len = len(marker)
        for index in range(0, len(lower) - marker_len + 1):
            if tuple(lower[index : index + marker_len]) != marker:
                continue
            project_tokens = tokens[index + marker_len :]
            if project_tokens and project_tokens[-1].lower() in PROVIDER_SUFFIXES:
                project_tokens = project_tokens[:-1]
            if project_tokens:
                return "-".join(project_tokens)
    return name


def _int_map(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        result[str(key)] = _coerce_int(value)
    return result


def _coerce_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _canonical_project(name: str) -> str:
    return name.replace("_", "-") or "unknown"