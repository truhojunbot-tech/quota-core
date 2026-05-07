"""Claude transcript session analytics parser."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable

from quota_core.adapters.projects import normalize_project_name
from quota_core.session.report import SessionReportQuery, build_session_report, normalize_session_report_query

MAX_FILE_BYTES = 50 * 1024 * 1024
ACTIVE_GAP_SECONDS = 300

Usage = dict[str, int]
ProjectNormalizer = Callable[[object], str]


@dataclass
class Aggregate:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    api_calls: int = 0
    models: dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens

    def add(self, usage: Usage, model: str) -> None:
        self.input_tokens += usage["input_tokens"]
        self.output_tokens += usage["output_tokens"]
        self.cache_read_input_tokens += usage["cache_read_input_tokens"]
        self.cache_creation_input_tokens += usage["cache_creation_input_tokens"]
        self.api_calls += 1
        if model:
            self.models[model] = self.models.get(model, 0) + usage_total(usage)


@dataclass
class PromptContext:
    text: str = ""
    slash_command: str | None = None
    skill: str | None = None
    subagent_type: str | None = None
    timestamp: int | None = None


def analyze_claude_sessions(
    transcript_roots: Iterable[str | Path],
    *,
    window: str | None = None,
    since: str | None = None,
    redaction: str | None = None,
    now: int | None = None,
    quota_windows: dict[str, Any] | None = None,
    max_file_bytes: int = MAX_FILE_BYTES,
    active_gap_seconds: int = ACTIVE_GAP_SECONDS,
    project_normalizer: ProjectNormalizer = normalize_project_name,
    quota_scanner_total_tokens: int | None = None,
) -> dict[str, Any]:
    """Return normalized Claude session analytics from local JSONL transcripts."""

    generated_at = int(time.time()) if now is None else int(now)
    query = normalize_session_report_query(
        window=window,
        since=since,
        redaction=redaction,
        now=generated_at,
        quota_windows=quota_windows,
    )
    scanner = ClaudeSessionScanner(
        transcript_roots=tuple(Path(root).expanduser() for root in transcript_roots),
        query=query,
        generated_at=generated_at,
        max_file_bytes=max_file_bytes,
        active_gap_seconds=active_gap_seconds,
        project_normalizer=project_normalizer,
        quota_scanner_total_tokens=quota_scanner_total_tokens,
    )
    return scanner.scan()


class ClaudeSessionScanner:
    def __init__(
        self,
        *,
        transcript_roots: tuple[Path, ...],
        query: SessionReportQuery,
        generated_at: int,
        max_file_bytes: int,
        active_gap_seconds: int,
        project_normalizer: ProjectNormalizer,
        quota_scanner_total_tokens: int | None,
    ) -> None:
        self.transcript_roots = transcript_roots
        self.query = query
        self.generated_at = generated_at
        self.max_file_bytes = max_file_bytes
        self.active_gap_seconds = active_gap_seconds
        self.project_normalizer = project_normalizer
        self.quota_scanner_total_tokens = quota_scanner_total_tokens
        self.totals = Aggregate()
        self.by_project: dict[str, Aggregate] = defaultdict(Aggregate)
        self.by_model: dict[str, Aggregate] = defaultdict(Aggregate)
        self.by_subagent: dict[str, Aggregate] = defaultdict(Aggregate)
        self.by_skill: dict[str, Aggregate] = defaultdict(Aggregate)
        self.by_slash_command: dict[str, Aggregate] = defaultdict(Aggregate)
        self.runtime_by_class: dict[str, Aggregate] = defaultdict(Aggregate)
        self.expensive_prompts: dict[str, dict[str, Any]] = {}
        self.cache_breaks: dict[str, dict[str, Any]] = {}
        self.seen_api_keys: set[str] = set()
        self.duplicate_records = 0
        self.skipped_oversized_files = 0
        self.skipped_unparseable_timestamps = 0
        self.malformed_json_records = 0
        self.timestamps: list[int] = []
        self.warnings: list[str] = []

    def scan(self) -> dict[str, Any]:
        for root in self.transcript_roots:
            self._scan_root(root)
        self._finalize_warnings()
        return build_session_report(
            query=self.query,
            generated_at=self.generated_at,
            totals=self._totals(),
            by_project=self._rows(self.by_project),
            by_model=self._rows(self.by_model),
            by_subagent=self._rows(self.by_subagent),
            by_skill=self._rows(self.by_skill),
            by_slash_command=self._rows(self.by_slash_command),
            expensive_prompts=sorted(self.expensive_prompts.values(), key=lambda row: -int(row["total_tokens"]))[:20],
            cache_breaks=sorted(self.cache_breaks.values(), key=lambda row: -int(row["tokens"]))[:20],
            runtime_attribution=self._runtime_attribution(),
            reconciliation=self._reconciliation(),
            warnings=self.warnings,
        )

    def _scan_root(self, root: Path) -> None:
        if not root.exists():
            self.warnings.append("transcript root does not exist")
            return
        transcripts = [root] if root.is_file() else sorted(root.rglob("*.jsonl"))
        for transcript in transcripts:
            self._scan_file(root if root.is_dir() else root.parent, transcript)

    def _scan_file(self, root: Path, transcript: Path) -> None:
        try:
            if transcript.stat().st_size > self.max_file_bytes:
                self.skipped_oversized_files += 1
                return
        except OSError:
            return
        project = self.project_normalizer(project_from_path(root, transcript))
        context = PromptContext()
        try:
            handle = transcript.open(errors="replace")
        except OSError:
            return
        with handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    self.malformed_json_records += 1
                    continue
                if not isinstance(record, dict):
                    continue
                timestamp = parse_timestamp(record.get("timestamp") or record.get("created_at") or record.get("createdAt") or record.get("time"))
                if not self._in_window(timestamp):
                    continue
                if timestamp is not None:
                    self.timestamps.append(timestamp)
                context = update_context(context, record, timestamp)
                usage = extract_usage(record)
                if not usage:
                    continue
                key = dedupe_key(record, usage)
                if key in self.seen_api_keys:
                    self.duplicate_records += 1
                    continue
                self.seen_api_keys.add(key)
                model = extract_model(record)
                self._add_usage(project, model, usage, runtime_classification(record), context, timestamp)

    def _in_window(self, timestamp: int | None) -> bool:
        selected = self.query.window
        if selected.kind == "all":
            return True
        if timestamp is None:
            self.skipped_unparseable_timestamps += 1
            return False
        if selected.window_start is not None and timestamp < selected.window_start:
            return False
        if selected.window_end is not None and timestamp > selected.window_end:
            return False
        return True

    def _add_usage(
        self,
        project: str,
        model: str,
        usage: Usage,
        runtime_class: str,
        context: PromptContext,
        timestamp: int | None,
    ) -> None:
        self.totals.add(usage, model)
        self.by_project[project].add(usage, model)
        model_key = model or "unknown"
        self.by_model[model_key].add(usage, model_key)
        if context.subagent_type:
            self.by_subagent[context.subagent_type].add(usage, model)
        if context.skill:
            self.by_skill[context.skill].add(usage, model)
        if context.slash_command:
            self.by_slash_command[context.slash_command].add(usage, model)
        self.runtime_by_class[runtime_class].add(usage, model)
        total = usage_total(usage)
        if context.text:
            preview, prompt_hash = redact_prompt(context.text, self.query.redaction)
            row = self.expensive_prompts.get(prompt_hash)
            if row is None:
                row = {
                    "project": project,
                    "prompt_preview": preview,
                    "prompt_hash": prompt_hash,
                    "timestamp": timestamp,
                    "total_tokens": 0,
                    "api_calls": 0,
                    "subagent_type": context.subagent_type,
                    "slash_command": context.slash_command,
                    "skill": context.skill,
                    "redaction": self.query.redaction,
                }
                self.expensive_prompts[prompt_hash] = row
            row["total_tokens"] = int(row["total_tokens"]) + total
            row["api_calls"] = int(row["api_calls"]) + 1
        if usage["cache_creation_input_tokens"] > 0 and usage["cache_creation_input_tokens"] >= usage["cache_read_input_tokens"]:
            preview, prompt_hash = redact_prompt(context.text, self.query.redaction)
            row = self.cache_breaks.get(prompt_hash)
            if row is None:
                row = {
                    "project": project,
                    "timestamp": timestamp,
                    "reason": "cache_creation_spike_after_prompt_change",
                    "tokens": 0,
                    "api_calls": 0,
                    "prompt_hash": prompt_hash,
                    "prompt_preview": preview,
                }
                self.cache_breaks[prompt_hash] = row
            row["tokens"] = int(row["tokens"]) + usage["cache_creation_input_tokens"]
            row["api_calls"] = int(row["api_calls"]) + 1

    def _totals(self) -> dict[str, Any]:
        cache_total = self.totals.cache_read_input_tokens + self.totals.cache_creation_input_tokens
        return {
            "input_tokens": self.totals.input_tokens,
            "output_tokens": self.totals.output_tokens,
            "cache_read_input_tokens": self.totals.cache_read_input_tokens,
            "cache_creation_input_tokens": self.totals.cache_creation_input_tokens,
            "total_tokens": self.totals.total_tokens,
            "api_calls": self.totals.api_calls,
            "deduped_api_calls": self.duplicate_records,
            "cache_hit_pct": round(self.totals.cache_read_input_tokens / cache_total * 100, 1) if cache_total else 0.0,
            "active_seconds": active_seconds(self.timestamps, self.active_gap_seconds),
            "wall_seconds": wall_seconds(self.timestamps),
        }

    def _rows(self, aggregates: dict[str, Aggregate]) -> list[dict[str, Any]]:
        total = self.totals.total_tokens
        return [aggregate_row(name, aggregate, total) for name, aggregate in sorted(aggregates.items(), key=lambda item: -item[1].total_tokens)]

    def _runtime_attribution(self) -> dict[str, Any]:
        human = self.runtime_by_class.get("human", Aggregate()).total_tokens
        runtime = self.runtime_by_class.get("runtime", Aggregate()).total_tokens
        unknown = self.runtime_by_class.get("unknown", Aggregate()).total_tokens
        return {
            "human_tokens": human,
            "runtime_tokens": runtime,
            "unknown_tokens": unknown,
            "by_class": self._rows(self.runtime_by_class),
        }

    def _reconciliation(self) -> dict[str, Any]:
        session_total = self.totals.total_tokens
        delta = None if self.quota_scanner_total_tokens is None else session_total - self.quota_scanner_total_tokens
        delta_pct = None
        if delta is not None and self.quota_scanner_total_tokens:
            delta_pct = round(delta / self.quota_scanner_total_tokens * 100, 1)
        notes = []
        if self.quota_scanner_total_tokens is None:
            notes.append("quota scanner total unavailable")
        if self.duplicate_records:
            notes.append(f"deduped {self.duplicate_records} duplicate transcript records")
        return {
            "quota_scanner_total_tokens": self.quota_scanner_total_tokens,
            "session_total_tokens": session_total,
            "delta_tokens": delta,
            "delta_pct": delta_pct,
            "notes": notes,
        }

    def _finalize_warnings(self) -> None:
        if self.skipped_oversized_files:
            self.warnings.append(f"skipped {self.skipped_oversized_files} oversized transcript files")
        if self.skipped_unparseable_timestamps:
            self.warnings.append(f"skipped {self.skipped_unparseable_timestamps} records without parseable timestamps")
        if self.malformed_json_records:
            self.warnings.append(f"skipped {self.malformed_json_records} malformed json records")


def extract_usage(record: dict[str, Any]) -> Usage | None:
    raw = nested_dict(record, "message", "usage") or record.get("usage")
    if not isinstance(raw, dict):
        return None
    usage = {
        "input_tokens": coerce_int(raw.get("input_tokens")),
        "output_tokens": coerce_int(raw.get("output_tokens")),
        "cache_read_input_tokens": coerce_int(raw.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": coerce_int(raw.get("cache_creation_input_tokens")),
    }
    return usage if usage_total(usage) > 0 else None


def extract_model(record: dict[str, Any]) -> str:
    return str(nested_value(record, "message", "model") or record.get("model") or "unknown").split("/")[-1]


def dedupe_key(record: dict[str, Any], usage: Usage) -> str:
    request_id = record.get("requestId") or record.get("request_id") or nested_value(record, "message", "requestId")
    if request_id:
        return f"request:{request_id}"
    message_id = nested_value(record, "message", "id") or record.get("message_id")
    if message_id:
        return f"message:{message_id}"
    usage_tuple = tuple(usage[key] for key in sorted(usage))
    uuid = record.get("uuid")
    if uuid:
        return f"uuid:{uuid}:{usage_tuple}"
    return f"synthetic:{record.get('timestamp')}:{extract_model(record)}:{usage_tuple}"


def update_context(context: PromptContext, record: dict[str, Any], timestamp: int | None) -> PromptContext:
    prompt = user_prompt_text(record)
    tool_context = tool_use_context(record)
    slash_command = slash_command_from_prompt(prompt) if prompt else context.slash_command
    return PromptContext(
        text=prompt or context.text,
        slash_command=slash_command,
        skill=tool_context.get("skill") or context.skill,
        subagent_type=tool_context.get("subagent_type") or context.subagent_type,
        timestamp=timestamp or context.timestamp,
    )


def user_prompt_text(record: dict[str, Any]) -> str:
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    role = message.get("role") or record.get("role") or record.get("type")
    if role != "user":
        return ""
    return text_from_content(message.get("content") if "content" in message else record.get("content"))


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(part.strip() for part in parts if part.strip()).strip()
    return ""


def tool_use_context(record: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    blocks = message.get("content") if "content" in message else record.get("content")
    if not isinstance(blocks, list):
        return result
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = str(block.get("name") or "")
        raw_input = block.get("input") if isinstance(block.get("input"), dict) else {}
        if name.lower() == "skill":
            skill = raw_input.get("skill") or raw_input.get("name") or raw_input.get("command")
            if skill:
                result["skill"] = str(skill)
        if name.lower() in {"agent", "task"}:
            subagent = raw_input.get("subagent_type") or raw_input.get("agent_type") or raw_input.get("type") or raw_input.get("description")
            if subagent:
                result["subagent_type"] = str(subagent)
    return result


def slash_command_from_prompt(prompt: str) -> str | None:
    match = re.match(r"^\s*/([A-Za-z0-9_.-]+)", prompt)
    return f"/{match.group(1)}" if match else None


def runtime_classification(record: dict[str, Any]) -> str:
    value = record.get("usage_class") or record.get("runtime_class") or record.get("LLM_USAGE_CLASS")
    if value in {"human", "runtime"}:
        return str(value)
    env = record.get("env")
    if isinstance(env, dict) and env.get("LLM_USAGE_CLASS") == "runtime":
        return "runtime"
    return "unknown"


def aggregate_row(name: str, aggregate: Aggregate, total_tokens: int) -> dict[str, Any]:
    return {
        "name": name,
        "display_name": name,
        "total_tokens": aggregate.total_tokens,
        "api_calls": aggregate.api_calls,
        "share_pct": round(aggregate.total_tokens / total_tokens * 100, 1) if total_tokens else 0.0,
        "input_tokens": aggregate.input_tokens,
        "output_tokens": aggregate.output_tokens,
        "cache_read_input_tokens": aggregate.cache_read_input_tokens,
        "cache_creation_input_tokens": aggregate.cache_creation_input_tokens,
        "models": dict(sorted(aggregate.models.items(), key=lambda item: -item[1])),
    }


def redact_prompt(prompt: str, redaction: str) -> tuple[str, str]:
    digest = "sha256:" + hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()
    normalized = normalize_prompt_preview(prompt)
    if redaction == "none":
        return prompt, digest
    if redaction == "summary":
        return " ".join(normalized.split()[:12]), digest
    compact = " ".join(normalized.split())
    return (compact[:157] + "...") if len(compact) > 160 else compact, digest


def normalize_prompt_preview(prompt: str) -> str:
    compact = " ".join(prompt.split())
    if compact.startswith("=== AGENT_CREW TASK ==="):
        task_id = agent_crew_value(prompt, compact, "task_id")
        task_type = agent_crew_value(prompt, compact, "task_type")
        branch = agent_crew_value(prompt, compact, "branch")
        label = "Agent Crew"
        if task_type:
            label += f" {task_type}"
        if branch:
            label += f" {branch}"
        if task_id:
            label += f" ({task_id})"
        return label
    channel_marker = '<channel source="plugin:' + "tele" + "gram:" + "tele" + "gram" + '"'
    if compact.startswith(channel_marker):
        message = regex_group(r">\s*(.*?)\s*</channel>", compact)
        label = "Tele" + "gram"
        return f"{label}: {message}" if message else f"{label} message"
    return compact


def agent_crew_value(prompt: str, compact: str, field: str) -> str | None:
    line_match = re.search(rf"(?m)^[ \t]*{re.escape(field)}:[ \t]*(.*?)[ \t]*$", prompt)
    value = line_match.group(1).strip() if line_match else None
    if not value:
        value = regex_group(rf"\b{re.escape(field)}:\s*([^\s]+)", compact)
    if value in {"priority:", "context:", "description:", "result_url:"}:
        return None
    return value


def regex_group(pattern: str, value: str) -> str | None:
    match = re.search(pattern, value)
    return match.group(1) if match else None


def active_seconds(timestamps: list[int], gap_seconds: int) -> int:
    ordered = sorted(set(timestamps))
    if len(ordered) < 2:
        return 0
    total = 0
    previous = ordered[0]
    for current in ordered[1:]:
        gap = current - previous
        if 0 < gap <= gap_seconds:
            total += gap
        previous = current
    return total


def wall_seconds(timestamps: list[int]) -> int:
    return max(timestamps) - min(timestamps) if len(timestamps) >= 2 else 0


def parse_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number / 1000) if number > 10_000_000_000 else int(number)
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def usage_total(usage: Usage) -> int:
    return sum(int(value) for value in usage.values())


def coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def nested_value(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def nested_dict(data: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    value = nested_value(data, *keys)
    return value if isinstance(value, dict) else None


def project_from_path(root: Path, transcript: Path) -> str:
    try:
        relative = transcript.relative_to(root)
    except ValueError:
        return transcript.parent.name or "unknown"
    if len(relative.parts) >= 2 and relative.parts[0] == "subagents":
        return transcript.parent.parent.name or "subagents"
    return relative.parts[0] if relative.parts else transcript.parent.name or "unknown"