"""Public data models for parsed agent traces and generated reports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TurnSummary:
    session_id: str
    session_file: str
    turn_id: str
    started_at: str
    completed_at: str | None
    status: str
    collaboration_mode: str
    cwd: str
    repository: str
    repository_url: str
    branch: str
    model_provider: str
    model: str
    effort: str
    prompt_tokens: int
    cached_input_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    total_tokens: int
    function_call_total: int
    custom_tool_call_total: int
    web_search_call_total: int
    tool_call_total: int
    agent_reasoning_total: int
    response_reasoning_total: int
    context_compacted_total: int
    final_message_chars: int
    duration_secs: float | None
    model_context_window: int | None
    tool_breakdown: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    session_file: str
    session_started_at: str | None
    first_turn_started_at: str | None
    last_event_at: str | None
    duration_secs: float | None
    originator: str
    source: str
    cli_version: str
    cwd: str
    conversation_id: str
    execution_session_id: str
    team_id: str
    channel_id: str
    thread_ts: str
    repository: str
    repository_url: str
    branch: str
    commit_hash: str
    model_provider: str
    model: str
    effort: str
    turns_total: int
    completed_turns: int
    aborted_turns: int
    incomplete_turns: int
    prompt_tokens: int
    cached_input_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    total_tokens: int
    tool_call_total: int
    unique_tool_names: int
    context_compacted_total: int
    agent_reasoning_total: int
    response_reasoning_total: int
    orphan_abort_total: int
    skipped_lines: int


@dataclass(frozen=True)
class ParseIssue:
    session_file: str
    reason: str
    count: int = 1


@dataclass(frozen=True)
class TraceEvent:
    session_id: str
    session_file: str
    turn_id: str
    sequence: int
    line_number: int
    output_line_number: int | None
    timestamp: str
    category: str
    kind: str
    role: str
    phase: str
    title: str
    text: str
    tool_name: str
    call_id: str
    status: str
    input_text: str
    output_text: str
    duration_secs: float | None
    truncated_fields: tuple[str, ...]


@dataclass(frozen=True)
class SessionTrace:
    session_id: str
    session_file: str
    events: tuple[TraceEvent, ...]
    source_rows: int
    events_total: int
    collapsed_rows: int
    truncated_events: int


@dataclass(frozen=True)
class AnalysisResult:
    generated_at: str
    source_paths: tuple[str, ...]
    skipped_files: tuple[str, ...]
    issues: tuple[ParseIssue, ...]
    sessions: tuple[SessionSummary, ...]
    turns: tuple[TurnSummary, ...]
    traces: tuple[SessionTrace, ...] = ()


@dataclass(frozen=True)
class ReportBundle:
    output_dir: Path
    index_path: Path
    manifest_path: Path
    analysis_path: Path
    sessions_csv_path: Path
    turns_csv_path: Path
    metrics_csv_path: Path
