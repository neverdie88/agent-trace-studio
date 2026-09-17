"""Tolerant parser for Codex JSONL session journals."""

from __future__ import annotations

import glob
import json
import math
import os
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agent_trace_studio.models import (
    AnalysisResult,
    ParseIssue,
    SessionSummary,
    SessionTrace,
    TraceEvent,
    TurnSummary,
)

_START_EVENTS = frozenset({'task_started', 'turn_started'})
_COMPLETE_EVENTS = frozenset({'task_complete', 'turn_complete'})
_ABORT_EVENTS = frozenset({'turn_aborted', 'task_aborted'})
_JOURNAL_SUFFIXES = frozenset({'.json', '.jsonl'})
_JSON_ROW_KEYS = ('events', 'entries', 'records', 'items')
_JSON_TEXT_KEYS = ('content_text', 'journal', 'jsonl')
_DEFAULT_TRACE_CHAR_LIMIT = 20_000
_TRACE_LIFECYCLE_EVENTS = frozenset(
    {
        *_START_EVENTS,
        *_COMPLETE_EVENTS,
        *_ABORT_EVENTS,
        'context_compacted',
        'thread_rolled_back',
        'sub_agent_activity',
    }
)


class SessionResolutionError(RuntimeError):
    """Raised when an exact Codex session ID cannot be resolved locally."""


@dataclass(frozen=True)
class _JournalRows:
    rows: tuple[object, ...]
    malformed_rows: int = 0


@dataclass(frozen=True)
class _NormalizedJournalRow:
    item: object
    line_number: int


@dataclass
class _MutableTraceEvent:
    turn_id: str
    sequence: int
    line_number: int
    timestamp: str
    category: str
    kind: str
    title: str
    role: str = ''
    phase: str = ''
    text: str = ''
    tool_name: str = ''
    call_id: str = ''
    status: str = ''
    input_text: str = ''
    output_text: str = ''
    output_line_number: int | None = None
    duration_secs: float | None = None
    truncated_fields: set[str] = field(default_factory=set)


@dataclass
class _TraceAccumulator:
    session_file: Path
    char_limit: int
    session_id: str = ''
    current_turn_id: str = 'session'
    source_rows: int = 0
    omitted_rows: int = 0
    events: list[_MutableTraceEvent] = field(default_factory=list)
    call_events: dict[str, int] = field(default_factory=dict)
    message_fingerprints: set[tuple[str, str, str]] = field(default_factory=set)

    def append(self, event: _MutableTraceEvent) -> None:
        self.events.append(event)

    def finalize(self, included_turn_ids: set[str]) -> SessionTrace:
        events = [
            event
            for event in self.events
            if event.turn_id == 'session' or not included_turn_ids or event.turn_id in included_turn_ids
        ]
        frozen = tuple(
            TraceEvent(
                session_id=self.session_id or self.session_file.stem,
                session_file=str(self.session_file),
                turn_id=event.turn_id,
                sequence=index,
                line_number=event.line_number,
                output_line_number=event.output_line_number,
                timestamp=event.timestamp,
                category=event.category,
                kind=event.kind,
                role=event.role,
                phase=event.phase,
                title=event.title,
                text=event.text,
                tool_name=event.tool_name,
                call_id=event.call_id,
                status=event.status,
                input_text=event.input_text,
                output_text=event.output_text,
                duration_secs=event.duration_secs,
                truncated_fields=tuple(sorted(event.truncated_fields)),
            )
            for index, event in enumerate(events, start=1)
        )
        return SessionTrace(
            session_id=self.session_id or self.session_file.stem,
            session_file=str(self.session_file),
            events=frozen,
            source_rows=self.source_rows,
            events_total=len(events),
            collapsed_rows=max(self.source_rows - len(events), self.omitted_rows),
            truncated_events=sum(bool(event.truncated_fields) for event in events),
        )


@dataclass
class _UsageAccumulator:
    prompt: int = 0
    cached: int = 0
    completion: int = 0
    reasoning: int = 0
    total: int = 0

    def add_payload(self, payload: dict[str, object]) -> None:
        self.prompt += _safe_int(payload.get('input_tokens'))
        self.cached += _safe_int(payload.get('cached_input_tokens'))
        self.completion += _safe_int(payload.get('output_tokens'))
        self.reasoning += _safe_int(payload.get('reasoning_output_tokens'))
        self.total += _safe_int(payload.get('total_tokens'))


@dataclass
class _TurnAccumulator:
    session_id: str
    session_file: str
    turn_id: str
    started_at: datetime
    collaboration_mode: str = 'unknown'
    cwd: str = 'unknown'
    repository: str = 'unknown'
    repository_url: str = ''
    branch: str = ''
    model_provider: str = 'unknown'
    model: str = 'unknown'
    effort: str = 'unknown'
    status: str = 'in_progress'
    completed_at: datetime | None = None
    prompt_tokens: int = 0
    cached_input_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    function_call_total: int = 0
    custom_tool_call_total: int = 0
    web_search_call_total: int = 0
    agent_reasoning_total: int = 0
    response_reasoning_total: int = 0
    context_compacted_total: int = 0
    final_message_chars: int = 0
    model_context_window: int | None = None
    tool_counter: Counter[str] = field(default_factory=Counter)
    pending_web_search_calls: Counter[str] = field(default_factory=Counter)

    def add_usage(self, usage: _UsageAccumulator) -> None:
        self.prompt_tokens += usage.prompt
        self.cached_input_tokens += usage.cached
        self.completion_tokens += usage.completion
        self.reasoning_tokens += usage.reasoning
        self.total_tokens += usage.total

    def finalize(self) -> TurnSummary:
        duration = None
        if self.completed_at is not None:
            duration = max((self.completed_at - self.started_at).total_seconds(), 0.0)
        return TurnSummary(
            session_id=self.session_id,
            session_file=self.session_file,
            turn_id=self.turn_id,
            started_at=_isoformat(self.started_at),
            completed_at=_isoformat(self.completed_at) if self.completed_at else None,
            status=self.status,
            collaboration_mode=self.collaboration_mode,
            cwd=self.cwd,
            repository=self.repository,
            repository_url=self.repository_url,
            branch=self.branch,
            model_provider=self.model_provider,
            model=self.model,
            effort=self.effort,
            prompt_tokens=self.prompt_tokens,
            cached_input_tokens=self.cached_input_tokens,
            completion_tokens=self.completion_tokens,
            reasoning_tokens=self.reasoning_tokens,
            total_tokens=self.total_tokens,
            function_call_total=self.function_call_total,
            custom_tool_call_total=self.custom_tool_call_total,
            web_search_call_total=self.web_search_call_total,
            tool_call_total=sum(self.tool_counter.values()),
            agent_reasoning_total=self.agent_reasoning_total,
            response_reasoning_total=self.response_reasoning_total,
            context_compacted_total=self.context_compacted_total,
            final_message_chars=self.final_message_chars,
            duration_secs=duration,
            model_context_window=self.model_context_window,
            tool_breakdown=tuple(sorted(self.tool_counter.items())),
        )


@dataclass
class _SessionAccumulator:
    session_file: Path
    session_id: str
    session_started_at: datetime | None = None
    first_turn_started_at: datetime | None = None
    last_event_at: datetime | None = None
    originator: str = 'unknown'
    source: str = 'unknown'
    cli_version: str = 'unknown'
    cwd: str = 'unknown'
    conversation_id: str = ''
    execution_session_id: str = ''
    team_id: str = ''
    channel_id: str = ''
    thread_ts: str = ''
    repository: str = 'unknown'
    repository_url: str = ''
    branch: str = ''
    commit_hash: str = ''
    model_provider: str = 'unknown'
    model: str = 'unknown'
    effort: str = 'unknown'
    prompt_tokens: int = 0
    cached_input_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    context_compacted_total: int = 0
    agent_reasoning_total: int = 0
    response_reasoning_total: int = 0
    orphan_abort_total: int = 0
    skipped_lines: int = 0
    recognized_rows: int = 0
    primary_session_meta_seen: bool = False
    tool_counter: Counter[str] = field(default_factory=Counter)
    pending_web_search_calls: Counter[str] = field(default_factory=Counter)
    counted_tool_call_ids: set[str] = field(default_factory=set)
    turns: list[TurnSummary] = field(default_factory=list)

    def observe_time(self, value: datetime | None) -> None:
        if value is not None and (self.last_event_at is None or value > self.last_event_at):
            self.last_event_at = value

    def add_usage(self, usage: _UsageAccumulator) -> None:
        self.prompt_tokens += usage.prompt
        self.cached_input_tokens += usage.cached
        self.completion_tokens += usage.completion
        self.reasoning_tokens += usage.reasoning
        self.total_tokens += usage.total

    def add_tool(self, tool_name: str) -> None:
        self.tool_counter[tool_name] += 1

    def finalize(self) -> SessionSummary:
        completed = sum(turn.status == 'completed' for turn in self.turns)
        aborted = sum(turn.status == 'aborted' for turn in self.turns)
        incomplete = sum(turn.status == 'incomplete' for turn in self.turns)
        anchor = self.first_turn_started_at or self.session_started_at
        duration = None
        if anchor is not None and self.last_event_at is not None:
            duration = max((self.last_event_at - anchor).total_seconds(), 0.0)
        return SessionSummary(
            session_id=self.session_id,
            session_file=str(self.session_file),
            session_started_at=_isoformat(self.session_started_at) if self.session_started_at else None,
            first_turn_started_at=_isoformat(anchor) if anchor else None,
            last_event_at=_isoformat(self.last_event_at) if self.last_event_at else None,
            duration_secs=duration,
            originator=self.originator,
            source=self.source,
            cli_version=self.cli_version,
            cwd=self.cwd,
            conversation_id=self.conversation_id,
            execution_session_id=self.execution_session_id,
            team_id=self.team_id,
            channel_id=self.channel_id,
            thread_ts=self.thread_ts,
            repository=self.repository,
            repository_url=self.repository_url,
            branch=self.branch,
            commit_hash=self.commit_hash,
            model_provider=self.model_provider,
            model=self.model,
            effort=self.effort,
            turns_total=len(self.turns),
            completed_turns=completed,
            aborted_turns=aborted,
            incomplete_turns=incomplete,
            prompt_tokens=self.prompt_tokens,
            cached_input_tokens=self.cached_input_tokens,
            completion_tokens=self.completion_tokens,
            reasoning_tokens=self.reasoning_tokens,
            total_tokens=self.total_tokens,
            tool_call_total=sum(self.tool_counter.values()),
            unique_tool_names=len(self.tool_counter),
            context_compacted_total=self.context_compacted_total,
            agent_reasoning_total=self.agent_reasoning_total,
            response_reasoning_total=self.response_reasoning_total,
            orphan_abort_total=self.orphan_abort_total,
            skipped_lines=self.skipped_lines,
        )


def discover_journal_paths(
    inputs: Sequence[str | Path] = (),
    *,
    codex_home: Path | None = None,
    limit: int | None = None,
) -> tuple[Path, ...]:
    """Resolve files, directories, and recursive globs to journal paths."""

    candidates: list[Path] = []
    if inputs:
        for raw_input in inputs:
            candidates.extend(_expand_input(raw_input))
    else:
        candidates.extend(_default_journal_paths(codex_home))
    deduplicated = sorted(
        {path.expanduser().resolve() for path in candidates if path.is_file()},
        key=_mtime_or_zero,
        reverse=True,
    )
    if limit is not None and limit > 0:
        deduplicated = deduplicated[:limit]
    return tuple(deduplicated)


def resolve_session_ids(
    session_ids: Sequence[str],
    *,
    codex_home: Path | None = None,
) -> tuple[Path, ...]:
    """Resolve exact ``session_meta.payload.id`` values under ``CODEX_HOME``."""

    requested = tuple(dict.fromkeys(session_id.strip() for session_id in session_ids if session_id.strip()))
    if not requested:
        return ()
    invalid = [session_id for session_id in requested if _invalid_session_id(session_id)]
    if invalid:
        raise SessionResolutionError(f'invalid session ID: {invalid[0]}')

    available = _default_journal_paths(codex_home)
    roots = _codex_journal_roots(codex_home)
    resolved: list[Path] = []
    for session_id in requested:
        filename_candidates = [path for path in available if session_id in path.name]
        remaining = [path for path in available if path not in filename_candidates]
        matches = [path for path in filename_candidates if _journal_session_id(path) == session_id]
        if not matches:
            matches = [path for path in remaining if _journal_session_id(path) == session_id]
        if not matches:
            searched = ', '.join(str(root) for root in roots)
            raise SessionResolutionError(f'Codex session ID {session_id} was not found under: {searched}')
        matches.sort(key=_mtime_or_zero, reverse=True)
        resolved.append(matches[0].resolve())
    return tuple(dict.fromkeys(resolved))


def analyze_journals(
    source_paths: Sequence[Path],
    *,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    generated_at: datetime | None = None,
    include_trace: bool = False,
    trace_char_limit: int = _DEFAULT_TRACE_CHAR_LIMIT,
) -> AnalysisResult:
    """Parse journals into deterministic session and turn summaries."""

    normalized_from = _as_utc(from_time)
    normalized_to = _as_utc(to_time)
    sessions: list[SessionSummary] = []
    turns: list[TurnSummary] = []
    traces: list[SessionTrace] = []
    skipped_files: list[str] = []
    issues: list[ParseIssue] = []
    for path in source_paths:
        summary, parsed_turns, trace, file_issues = _parse_journal(
            path,
            from_time=normalized_from,
            to_time=normalized_to,
            include_trace=include_trace,
            trace_char_limit=max(trace_char_limit, 1),
        )
        issues.extend(file_issues)
        if summary is None:
            skipped_files.append(str(path))
            continue
        sessions.append(summary)
        turns.extend(parsed_turns)
        if trace is not None:
            traces.append(trace)
    sessions.sort(key=lambda item: item.first_turn_started_at or item.session_started_at or '', reverse=True)
    turns.sort(key=lambda item: item.started_at, reverse=True)
    timestamp = _as_utc(generated_at) or datetime.now(UTC)
    return AnalysisResult(
        generated_at=_isoformat(timestamp),
        source_paths=tuple(str(path) for path in source_paths),
        skipped_files=tuple(skipped_files),
        issues=tuple(issues),
        sessions=tuple(sessions),
        turns=tuple(turns),
        traces=tuple(traces),
    )


def parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 value and normalize it to UTC."""

    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = f'{text[:-1]}+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(parsed)


def _expand_input(raw_input: str | Path) -> Iterable[Path]:
    raw_text = str(Path(str(raw_input)).expanduser())
    if glob.has_magic(raw_text):
        return (Path(value) for value in glob.glob(raw_text, recursive=True))  # noqa: PTH207
    path = Path(raw_text)
    if path.is_dir():
        return _directory_journal_paths(path)
    if path.is_file():
        return (path,)
    return ()


def _codex_journal_roots(codex_home: Path | None) -> tuple[Path, Path]:
    home = (codex_home or Path(os.environ.get('CODEX_HOME', '~/.codex'))).expanduser()
    return home / 'sessions', home / 'archived_sessions'


def _default_journal_paths(codex_home: Path | None) -> list[Path]:
    candidates: list[Path] = []
    for root in _codex_journal_roots(codex_home):
        if root.is_dir():
            candidates.extend(_directory_journal_paths(root))
    return sorted(
        {path.resolve() for path in candidates if path.is_file()},
        key=_mtime_or_zero,
        reverse=True,
    )


def _directory_journal_paths(root: Path) -> Iterable[Path]:
    return (path for path in root.rglob('*') if path.is_file() and path.suffix.lower() in _JOURNAL_SUFFIXES)


def _invalid_session_id(session_id: str) -> bool:
    return any(character in session_id for character in ('/', '\\', '\x00', '*', '?', '[', ']'))


def _journal_session_id(path: Path) -> str | None:
    try:
        journal = _read_journal_rows(path)
    except (OSError, UnicodeError):
        return None
    for item in journal.rows:
        if not isinstance(item, dict) or item.get('type') != 'session_meta':
            continue
        payload = item.get('payload')
        if isinstance(payload, dict):
            return _safe_text(payload.get('id'))
    return None


def _read_journal_rows(path: Path) -> _JournalRows:
    text = path.read_text(encoding='utf-8')
    if not text.strip():
        return _JournalRows(rows=())
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return _decode_json_lines(text)
    return _flatten_json_document(document)


def _decode_json_lines(text: str) -> _JournalRows:
    rows: list[object] = []
    malformed_rows = 0
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        try:
            document = json.loads(raw_line)
        except json.JSONDecodeError:
            malformed_rows += 1
            continue
        if isinstance(document, dict):
            flattened = _flatten_json_document(document)
            rows.extend(flattened.rows)
            malformed_rows += flattened.malformed_rows
        else:
            rows.append(document)
    return _JournalRows(rows=tuple(rows), malformed_rows=malformed_rows)


def _flatten_json_document(document: object) -> _JournalRows:
    if isinstance(document, list):
        rows: list[object] = []
        malformed_rows = 0
        for item in document:
            flattened = _flatten_json_document(item)
            rows.extend(flattened.rows)
            malformed_rows += flattened.malformed_rows
        return _JournalRows(rows=tuple(rows), malformed_rows=malformed_rows)
    if not isinstance(document, dict):
        return _JournalRows(rows=(document,))
    if 'type' in document and 'payload' in document:
        return _JournalRows(rows=(document,))
    for key in _JSON_ROW_KEYS:
        value = document.get(key)
        if isinstance(value, list):
            return _flatten_json_document(value)
    for key in _JSON_TEXT_KEYS:
        value = document.get(key)
        if isinstance(value, str):
            return _decode_json_lines(value)
    return _JournalRows(rows=(document,))


def _normalize_journal_rows(rows: tuple[object, ...]) -> tuple[_NormalizedJournalRow, ...]:
    if _looks_like_claude_journal(rows):
        return _normalize_claude_rows(rows)
    return tuple(_NormalizedJournalRow(item=item, line_number=index) for index, item in enumerate(rows, start=1))


def _looks_like_claude_journal(rows: tuple[object, ...]) -> bool:
    has_codex_envelope = any(
        isinstance(item, dict)
        and item.get('type') in {'session_meta', 'turn_context', 'event_msg', 'response_item', 'agent_trace_event'}
        and isinstance(item.get('payload'), dict)
        for item in rows
    )
    if has_codex_envelope:
        return False
    return any(
        isinstance(item, dict)
        and item.get('type') in {'user', 'assistant', 'system', 'result', 'queue-operation', 'attachment'}
        and isinstance(item.get('sessionId'), str)
        for item in rows
    )


def _normalize_claude_rows(rows: tuple[object, ...]) -> tuple[_NormalizedJournalRow, ...]:
    source_rows = [(index, item) for index, item in enumerate(rows, start=1) if isinstance(item, dict)]
    first = next((item for _, item in source_rows if isinstance(item.get('sessionId'), str)), None)
    if first is None:
        return tuple(_NormalizedJournalRow(item=item, line_number=index) for index, item in enumerate(rows, start=1))

    session_id = _safe_text(first.get('sessionId')) or 'claude-session'
    first_line = next(index for index, item in source_rows if item is first)
    normalized: list[_NormalizedJournalRow] = [
        _NormalizedJournalRow(
            item=_normalized_row(
                'session_meta',
                {
                    'id': session_id,
                    'timestamp': first.get('timestamp'),
                    'cwd': first.get('cwd'),
                    'originator': 'claude_code',
                    'source': first.get('entrypoint') or 'claude_code',
                    'cli_version': first.get('version'),
                    'model_provider': 'anthropic',
                    'git': {'branch': first.get('gitBranch')},
                },
                first.get('timestamp'),
            ),
            line_number=first_line,
        )
    ]
    turn_id = ''
    turn_open = False
    turn_index = 0
    cumulative_usage = _UsageAccumulator()

    def append(line_number: int, timestamp: object, entry_type: str, payload: dict[str, object]) -> None:
        normalized.append(
            _NormalizedJournalRow(
                item=_normalized_row(entry_type, payload, timestamp),
                line_number=line_number,
            )
        )

    for line_number, item in source_rows:
        row_type = _safe_text(item.get('type')) or ''
        timestamp = item.get('timestamp')
        message = item.get('message')
        message_payload = message if isinstance(message, dict) else {}
        blocks = _claude_content_blocks(message_payload.get('content'))
        if row_type == 'user':
            tool_results = [block for block in blocks if isinstance(block, dict) and block.get('type') == 'tool_result']
            visible_blocks = [
                block for block in blocks if not isinstance(block, dict) or block.get('type') != 'tool_result'
            ]
            user_text = _content_text(visible_blocks)
            if user_text:
                if turn_open:
                    append(line_number, timestamp, 'event_msg', {'type': 'task_complete', 'turn_id': turn_id})
                turn_index += 1
                turn_id = _safe_text(item.get('promptId')) or _safe_text(item.get('uuid')) or f'turn-{turn_index}'
                turn_open = True
                append(
                    line_number,
                    timestamp,
                    'turn_context',
                    {'turn_id': turn_id, 'cwd': item.get('cwd'), 'model': 'unknown'},
                )
                append(line_number, timestamp, 'event_msg', {'type': 'task_started', 'turn_id': turn_id})
                append(line_number, timestamp, 'event_msg', {'type': 'user_message', 'message': user_text})
            for block in tool_results:
                append(
                    line_number,
                    timestamp,
                    'response_item',
                    {
                        'type': 'function_call_output',
                        'call_id': block.get('tool_use_id'),
                        'output': block.get('content'),
                        'status': 'failed' if block.get('is_error') is True else 'completed',
                    },
                )
            continue
        if row_type == 'assistant':
            if not turn_open:
                turn_index += 1
                turn_id = _safe_text(item.get('parentUuid')) or f'turn-{turn_index}'
                turn_open = True
                append(line_number, timestamp, 'event_msg', {'type': 'task_started', 'turn_id': turn_id})
            model = _safe_text(message_payload.get('model')) or 'unknown'
            append(
                line_number,
                timestamp,
                'turn_context',
                {'turn_id': turn_id, 'cwd': item.get('cwd'), 'model': model},
            )
            for block in blocks:
                if isinstance(block, str):
                    if block.strip():
                        append(
                            line_number,
                            timestamp,
                            'event_msg',
                            {'type': 'agent_message', 'message': block, 'phase': 'commentary'},
                        )
                    continue
                block_type = _safe_text(block.get('type')) or ''
                if block_type == 'text':
                    append(
                        line_number,
                        timestamp,
                        'event_msg',
                        {
                            'type': 'agent_message',
                            'message': block.get('text'),
                            'phase': 'final_answer' if message_payload.get('stop_reason') else 'commentary',
                        },
                    )
                elif block_type in {'tool_use', 'server_tool_use'}:
                    append(
                        line_number,
                        timestamp,
                        'response_item',
                        {
                            'type': 'function_call',
                            'name': block.get('name'),
                            'call_id': block.get('id'),
                            'arguments': block.get('input'),
                        },
                    )
            usage = message_payload.get('usage')
            if isinstance(usage, dict):
                last_usage = _claude_usage(usage)
                cumulative_usage.prompt += last_usage.prompt
                cumulative_usage.cached += last_usage.cached
                cumulative_usage.completion += last_usage.completion
                cumulative_usage.reasoning += last_usage.reasoning
                cumulative_usage.total += last_usage.total
                append(
                    line_number,
                    timestamp,
                    'event_msg',
                    {
                        'type': 'token_count',
                        'info': {
                            'total_token_usage': _usage_payload(cumulative_usage),
                            'last_token_usage': _usage_payload(last_usage),
                        },
                    },
                )
            stop_reason = _safe_text(message_payload.get('stop_reason')) or ''
            if stop_reason and stop_reason not in {'tool_use', 'pause_turn', 'max_tokens'}:
                append(line_number, timestamp, 'event_msg', {'type': 'task_complete', 'turn_id': turn_id})
                turn_open = False
            continue
        if row_type == 'result' and turn_open:
            event_type = 'task_aborted' if item.get('is_error') is True else 'task_complete'
            append(line_number, timestamp, 'event_msg', {'type': event_type, 'turn_id': turn_id})
            turn_open = False

    return tuple(normalized)


def _normalized_row(entry_type: str, payload: dict[str, object], timestamp: object) -> dict[str, object]:
    return {'timestamp': timestamp, 'type': entry_type, 'payload': payload}


def _claude_content_blocks(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if value in (None, ''):
        return []
    return [value]


def _claude_usage(value: dict[str, object]) -> _UsageAccumulator:
    prompt = _safe_int(value.get('input_tokens')) + _safe_int(value.get('cache_creation_input_tokens'))
    cached = _safe_int(value.get('cache_read_input_tokens'))
    completion = _safe_int(value.get('output_tokens'))
    return _UsageAccumulator(
        prompt=prompt,
        cached=cached,
        completion=completion,
        total=prompt + cached + completion,
    )


def _usage_payload(value: _UsageAccumulator) -> dict[str, int]:
    return {
        'input_tokens': value.prompt,
        'cached_input_tokens': value.cached,
        'output_tokens': value.completion,
        'reasoning_output_tokens': value.reasoning,
        'total_tokens': value.total,
    }


def _parse_journal(
    path: Path,
    *,
    from_time: datetime | None,
    to_time: datetime | None,
    include_trace: bool,
    trace_char_limit: int,
) -> tuple[SessionSummary | None, list[TurnSummary], SessionTrace | None, list[ParseIssue]]:
    accumulator = _SessionAccumulator(session_file=path, session_id=path.stem)
    trace_accumulator = _TraceAccumulator(session_file=path, char_limit=trace_char_limit) if include_trace else None
    current_turn: _TurnAccumulator | None = None
    last_turn_context: dict[str, object] = {}
    last_total_usage: tuple[int, int, int, int, int] | None = None
    has_relevant_turn = False
    issues: list[ParseIssue] = []
    malformed_rows = 0
    non_object_rows = 0
    try:
        journal = _read_journal_rows(path)
    except (OSError, UnicodeError):
        return None, [], None, [ParseIssue(session_file=str(path), reason='unreadable_file')]
    malformed_rows = journal.malformed_rows
    accumulator.skipped_lines += malformed_rows
    if trace_accumulator is not None:
        trace_accumulator.source_rows = len(journal.rows) + malformed_rows
        trace_accumulator.omitted_rows += malformed_rows
    normalized_rows = _normalize_journal_rows(journal.rows)
    for normalized_row in normalized_rows:
        line_number = normalized_row.line_number
        item = normalized_row.item
        if not isinstance(item, dict):
            accumulator.skipped_lines += 1
            non_object_rows += 1
            if trace_accumulator is not None:
                trace_accumulator.omitted_rows += 1
            continue
        timestamp = parse_timestamp(item.get('timestamp'))
        accumulator.observe_time(timestamp)
        payload = item.get('payload')
        entry_type = _safe_text(item.get('type'))
        if trace_accumulator is not None:
            _record_trace_row(
                trace_accumulator,
                item=item,
                line_number=line_number,
                timestamp=timestamp,
            )
        if entry_type == 'session_meta' and isinstance(payload, dict):
            accumulator.recognized_rows += 1
            _apply_session_meta(accumulator, payload)
            if trace_accumulator is not None:
                trace_accumulator.session_id = accumulator.session_id
            continue
        if entry_type == 'turn_context' and isinstance(payload, dict):
            accumulator.recognized_rows += 1
            last_turn_context = payload
            if current_turn is not None:
                _apply_turn_context(current_turn, accumulator, payload)
            continue
        if entry_type == 'event_msg' and isinstance(payload, dict):
            accumulator.recognized_rows += 1
            payload_type = _safe_text(payload.get('type'))
            if payload_type in _START_EVENTS:
                if current_turn is not None:
                    current_turn.status = 'incomplete'
                    current_turn.completed_at = timestamp or current_turn.started_at
                    has_relevant_turn |= _append_turn(
                        accumulator,
                        current_turn.finalize(),
                        from_time=from_time,
                        to_time=to_time,
                    )
                turn_id = _safe_text(payload.get('turn_id')) or f'unknown-{len(accumulator.turns) + 1}'
                current_turn = _TurnAccumulator(
                    session_id=accumulator.session_id,
                    session_file=str(path),
                    turn_id=turn_id,
                    started_at=timestamp or accumulator.last_event_at or datetime.now(UTC),
                    collaboration_mode=(
                        _safe_text(payload.get('collaboration_mode_kind'))
                        or _safe_text(payload.get('collaboration_mode'))
                        or 'unknown'
                    ),
                    model_context_window=_safe_int_or_none(payload.get('model_context_window')),
                )
                _apply_turn_context(current_turn, accumulator, last_turn_context)
                if accumulator.first_turn_started_at is None:
                    accumulator.first_turn_started_at = current_turn.started_at
                continue
            if payload_type in _COMPLETE_EVENTS:
                if current_turn is not None:
                    current_turn.status = 'completed'
                    current_turn.completed_at = timestamp or accumulator.last_event_at or datetime.now(UTC)
                    current_turn.final_message_chars = len(
                        (_safe_text(payload.get('last_agent_message')) or '').strip()
                    )
                    has_relevant_turn |= _append_turn(
                        accumulator,
                        current_turn.finalize(),
                        from_time=from_time,
                        to_time=to_time,
                    )
                    current_turn = None
                continue
            if payload_type in _ABORT_EVENTS:
                if current_turn is None:
                    accumulator.orphan_abort_total += 1
                else:
                    current_turn.status = 'aborted'
                    current_turn.completed_at = timestamp or accumulator.last_event_at or datetime.now(UTC)
                    has_relevant_turn |= _append_turn(
                        accumulator,
                        current_turn.finalize(),
                        from_time=from_time,
                        to_time=to_time,
                    )
                    current_turn = None
                continue
            if payload_type == 'token_count':
                last_total_usage = _record_token_usage(
                    payload,
                    accumulator=accumulator,
                    current_turn=current_turn,
                    last_total_usage=last_total_usage,
                )
                continue
            if payload_type == 'agent_reasoning':
                accumulator.agent_reasoning_total += 1
                if current_turn is not None:
                    current_turn.agent_reasoning_total += 1
                continue
            if payload_type == 'context_compacted':
                accumulator.context_compacted_total += 1
                if current_turn is not None:
                    current_turn.context_compacted_total += 1
                continue
            if payload_type == 'web_search_end':
                _record_web_search_end(payload, accumulator=accumulator, current_turn=current_turn)
                continue
            if payload_type in {'mcp_tool_call_end', 'patch_apply_end'}:
                _record_tool_completion(
                    payload,
                    payload_type=payload_type,
                    accumulator=accumulator,
                    current_turn=current_turn,
                )
                continue
            continue
        if entry_type == 'response_item' and isinstance(payload, dict):
            accumulator.recognized_rows += 1
            _record_response_item(payload, accumulator=accumulator, current_turn=current_turn)
            continue
        if entry_type == 'agent_trace_event' and isinstance(payload, dict):
            accumulator.recognized_rows += 1
            if current_turn is None:
                turn_id = _safe_text(payload.get('turn_id')) or f'run-{len(accumulator.turns) + 1}'
                current_turn = _TurnAccumulator(
                    session_id=accumulator.session_id,
                    session_file=str(path),
                    turn_id=turn_id,
                    started_at=timestamp or accumulator.last_event_at or datetime.now(UTC),
                )
                _apply_turn_context(current_turn, accumulator, last_turn_context)
                if accumulator.first_turn_started_at is None:
                    accumulator.first_turn_started_at = current_turn.started_at
            _record_canonical_metrics(payload, accumulator=accumulator, current_turn=current_turn)

    if current_turn is not None:
        current_turn.status = 'incomplete'
        current_turn.completed_at = accumulator.last_event_at or current_turn.started_at
        has_relevant_turn |= _append_turn(
            accumulator,
            current_turn.finalize(),
            from_time=from_time,
            to_time=to_time,
        )
    if malformed_rows:
        issues.append(ParseIssue(session_file=str(path), reason='malformed_json_line', count=malformed_rows))
    if non_object_rows:
        issues.append(ParseIssue(session_file=str(path), reason='non_object_json_line', count=non_object_rows))
    if accumulator.recognized_rows == 0:
        issues.append(ParseIssue(session_file=str(path), reason='no_recognized_codex_rows'))
        return None, [], None, issues
    if (from_time is not None or to_time is not None) and not has_relevant_turn:
        return None, [], None, issues
    trace = None
    if trace_accumulator is not None:
        trace_accumulator.session_id = accumulator.session_id
        trace = trace_accumulator.finalize({turn.turn_id for turn in accumulator.turns})
    return accumulator.finalize(), list(accumulator.turns), trace, issues


def _record_trace_row(
    trace: _TraceAccumulator,
    *,
    item: dict[str, object],
    line_number: int,
    timestamp: datetime | None,
) -> None:
    entry_type = _safe_text(item.get('type')) or 'unknown'
    payload = item.get('payload')
    if not isinstance(payload, dict):
        trace.omitted_rows += 1
        return
    timestamp_text = _isoformat(timestamp) if timestamp else (_safe_text(item.get('timestamp')) or '')
    if entry_type == 'session_meta':
        if not trace.session_id:
            trace.session_id = _safe_text(payload.get('id')) or trace.session_id
        if any(event.kind == 'session_meta' for event in trace.events):
            trace.omitted_rows += 1
            return
        text, truncated = _trace_value(
            _select_trace_fields(
                payload,
                'id',
                'timestamp',
                'cwd',
                'originator',
                'source',
                'cli_version',
                'model_provider',
                'forked_from_id',
                'git',
            ),
            limit=trace.char_limit,
        )
        event = _new_trace_event(
            trace,
            line_number=line_number,
            timestamp=timestamp_text,
            category='context',
            kind='session_meta',
            title='Session context',
            text=text,
            turn_id='session',
        )
        if truncated:
            event.truncated_fields.add('text')
        trace.append(event)
        return
    if entry_type == 'turn_context':
        trace.current_turn_id = _safe_text(payload.get('turn_id')) or trace.current_turn_id
        text, truncated = _trace_value(
            _select_trace_fields(
                payload,
                'turn_id',
                'cwd',
                'model',
                'effort',
                'collaboration_mode',
                'approval_policy',
                'sandbox_policy',
            ),
            limit=trace.char_limit,
        )
        event = _new_trace_event(
            trace,
            line_number=line_number,
            timestamp=timestamp_text,
            category='context',
            kind='turn_context',
            title='Turn context',
            text=text,
        )
        if truncated:
            event.truncated_fields.add('text')
        trace.append(event)
        return
    if entry_type == 'event_msg':
        _record_trace_event_message(
            trace,
            payload=payload,
            line_number=line_number,
            timestamp=timestamp_text,
        )
        return
    if entry_type == 'response_item':
        _record_trace_response_item(
            trace,
            payload=payload,
            line_number=line_number,
            timestamp=timestamp_text,
        )
        return
    if entry_type == 'agent_trace_event':
        _record_canonical_trace_event(
            trace,
            payload=payload,
            line_number=line_number,
            timestamp=timestamp_text,
        )
        return
    trace.omitted_rows += 1


def _record_canonical_trace_event(
    trace: _TraceAccumulator,
    *,
    payload: dict[str, object],
    line_number: int,
    timestamp: str,
) -> None:
    category = _safe_text(payload.get('category')) or 'context'
    if category not in {'message', 'tool', 'reasoning', 'lifecycle', 'context'}:
        category = 'context'
    turn_id = _safe_text(payload.get('turn_id')) or trace.current_turn_id
    if turn_id != 'session':
        trace.current_turn_id = turn_id
    kind = _safe_text(payload.get('kind')) or 'agent_event'
    text, text_truncated = _trace_value(payload.get('text'), trace.char_limit)
    input_value = payload.get('input_text') if 'input_text' in payload else payload.get('input')
    output_value = payload.get('output_text') if 'output_text' in payload else payload.get('output')
    input_text, input_truncated = _trace_value(input_value, trace.char_limit)
    output_text, output_truncated = _trace_value(output_value, trace.char_limit)
    duration = _finite_float(payload.get('duration_secs'))
    event = _new_trace_event(
        trace,
        line_number=line_number,
        timestamp=timestamp,
        category=category,
        kind=kind,
        title=_safe_text(payload.get('title')) or _canonical_title(category, kind, payload),
        turn_id=turn_id,
        role=_safe_text(payload.get('role')) or '',
        phase=_safe_text(payload.get('phase')) or '',
        text=text,
        tool_name=_safe_text(payload.get('tool_name')) or '',
        call_id=_safe_text(payload.get('call_id')) or '',
        status=_safe_text(payload.get('status')) or '',
        input_text=input_text,
        output_text=output_text,
        duration_secs=max(duration, 0.0) if duration is not None else None,
    )
    if text_truncated:
        event.truncated_fields.add('text')
    if input_truncated:
        event.truncated_fields.add('input_text')
    if output_truncated:
        event.truncated_fields.add('output_text')
    trace.append(event)


def _canonical_title(category: str, kind: str, payload: dict[str, object]) -> str:
    if category == 'tool':
        return f'Tool: {_safe_text(payload.get("tool_name")) or kind}'
    if category == 'message':
        role = _safe_text(payload.get('role')) or 'Agent'
        return f'{role.capitalize()} message'
    if category == 'reasoning':
        return 'Reasoning summary'
    if category == 'lifecycle':
        return 'Run lifecycle'
    return kind.replace('_', ' ').strip().capitalize() or 'Agent event'


def _record_canonical_metrics(
    payload: dict[str, object],
    *,
    accumulator: _SessionAccumulator,
    current_turn: _TurnAccumulator,
) -> None:
    category = _safe_text(payload.get('category')) or ''
    if category == 'tool':
        tool_name = _safe_text(payload.get('tool_name')) or _safe_text(payload.get('kind')) or 'tool'
        call_id = _safe_text(payload.get('call_id')) or ''
        if not call_id or call_id not in accumulator.counted_tool_call_ids:
            accumulator.add_tool(tool_name)
            current_turn.tool_counter[tool_name] += 1
            if call_id:
                accumulator.counted_tool_call_ids.add(call_id)
    elif category == 'reasoning':
        accumulator.agent_reasoning_total += 1
        current_turn.agent_reasoning_total += 1


def _record_trace_event_message(
    trace: _TraceAccumulator,
    *,
    payload: dict[str, object],
    line_number: int,
    timestamp: str,
) -> None:
    payload_type = _safe_text(payload.get('type')) or 'event'
    if payload_type in _START_EVENTS:
        trace.current_turn_id = _safe_text(payload.get('turn_id')) or trace.current_turn_id
        trace.append(
            _new_trace_event(
                trace,
                line_number=line_number,
                timestamp=timestamp,
                category='lifecycle',
                kind=payload_type,
                title='Turn started',
                status='in_progress',
            )
        )
        return
    if payload_type == 'user_message':
        text = _safe_text(payload.get('message')) or _safe_text(payload.get('text')) or ''
        _append_trace_message(
            trace,
            line_number=line_number,
            timestamp=timestamp,
            kind=payload_type,
            role='user',
            phase='',
            title='User message',
            text=text,
        )
        return
    if payload_type == 'agent_message':
        phase = _safe_text(payload.get('phase')) or ''
        title = 'Assistant final answer' if phase == 'final_answer' else 'Assistant update'
        _append_trace_message(
            trace,
            line_number=line_number,
            timestamp=timestamp,
            kind=payload_type,
            role='assistant',
            phase=phase,
            title=title,
            text=_safe_text(payload.get('message')) or '',
        )
        return
    if payload_type == 'agent_reasoning':
        _append_trace_message(
            trace,
            line_number=line_number,
            timestamp=timestamp,
            kind=payload_type,
            role='assistant',
            phase='reasoning',
            title='Reasoning summary',
            text=_safe_text(payload.get('text')) or '',
            category='reasoning',
        )
        return
    if payload_type in _COMPLETE_EVENTS or payload_type in _ABORT_EVENTS:
        completed = payload_type in _COMPLETE_EVENTS
        turn_id = _safe_text(payload.get('turn_id')) or trace.current_turn_id
        status = 'completed' if completed else 'aborted'
        reason = _safe_text(payload.get('reason')) or ''
        last_message = _safe_text(payload.get('last_agent_message')) or ''
        if last_message and (turn_id, 'assistant', last_message.strip()) not in trace.message_fingerprints:
            reason = last_message
        event = _new_trace_event(
            trace,
            line_number=line_number,
            timestamp=timestamp,
            category='lifecycle',
            kind=payload_type,
            title='Turn completed' if completed else 'Turn aborted',
            text=reason,
            status=status,
            turn_id=turn_id,
            duration_secs=_duration_seconds(payload),
        )
        event.text, truncated = _truncate_trace_text(event.text, trace.char_limit)
        if truncated:
            event.truncated_fields.add('text')
        trace.append(event)
        trace.current_turn_id = 'session'
        return
    if payload_type == 'context_compacted':
        trace.append(
            _new_trace_event(
                trace,
                line_number=line_number,
                timestamp=timestamp,
                category='lifecycle',
                kind=payload_type,
                title='Context compacted',
            )
        )
        return
    if payload_type == 'thread_rolled_back':
        turns = _safe_int(payload.get('num_turns'))
        trace.append(
            _new_trace_event(
                trace,
                line_number=line_number,
                timestamp=timestamp,
                category='lifecycle',
                kind=payload_type,
                title='Thread rolled back',
                text=f'{turns} turn(s) rolled back',
                turn_id='session',
            )
        )
        return
    if payload_type == 'sub_agent_activity':
        agent_path = _safe_text(payload.get('agent_path')) or 'subagent'
        activity = _safe_text(payload.get('kind')) or 'activity'
        trace.append(
            _new_trace_event(
                trace,
                line_number=line_number,
                timestamp=timestamp,
                category='lifecycle',
                kind=payload_type,
                title=f'Subagent {activity}',
                text=agent_path,
                role=agent_path,
            )
        )
        return
    if payload_type in {'mcp_tool_call_end', 'patch_apply_end'}:
        _record_trace_tool_completion(
            trace,
            payload=payload,
            payload_type=payload_type,
            line_number=line_number,
            timestamp=timestamp,
        )
        return
    if payload_type == 'web_search_end':
        input_text, input_truncated = _trace_value(payload.get('query') or payload.get('action'), trace.char_limit)
        output_text, output_truncated = _trace_value(payload.get('results'), trace.char_limit)
        event = _new_trace_event(
            trace,
            line_number=line_number,
            timestamp=timestamp,
            category='tool',
            kind=payload_type,
            title='Tool: web search',
            tool_name='web_search',
            call_id=_safe_text(payload.get('call_id')) or '',
            input_text=input_text,
            output_text=output_text,
            status='completed',
        )
        if input_truncated:
            event.truncated_fields.add('input_text')
        if output_truncated:
            event.truncated_fields.add('output_text')
        trace.append(event)
        return
    if payload_type not in _TRACE_LIFECYCLE_EVENTS:
        trace.omitted_rows += 1


def _record_trace_response_item(
    trace: _TraceAccumulator,
    *,
    payload: dict[str, object],
    line_number: int,
    timestamp: str,
) -> None:
    payload_type = _safe_text(payload.get('type')) or 'item'
    if payload_type == 'message':
        role = _safe_text(payload.get('role')) or 'unknown'
        phase = _safe_text(payload.get('phase')) or ''
        category = 'context' if role in {'developer', 'system'} else 'message'
        if role in {'developer', 'system'}:
            title = f'{role.title()} context'
        elif role == 'assistant' and phase == 'final_answer':
            title = 'Assistant final answer'
        else:
            title = f'{role.title()} message'
        _append_trace_message(
            trace,
            line_number=line_number,
            timestamp=timestamp,
            kind=f'response_item.{payload_type}',
            role=role,
            phase=phase,
            title=title,
            text=_content_text(payload.get('content')),
            category=category,
        )
        return
    if payload_type == 'agent_message':
        author = _safe_text(payload.get('author')) or 'subagent'
        recipient = _safe_text(payload.get('recipient')) or ''
        title = f'Subagent message: {author}'
        if recipient:
            title = f'{title} to {recipient}'
        _append_trace_message(
            trace,
            line_number=line_number,
            timestamp=timestamp,
            kind=f'response_item.{payload_type}',
            role=author,
            phase='subagent',
            title=title,
            text=_content_text(payload.get('content')),
        )
        return
    if payload_type == 'reasoning':
        text = _content_text(payload.get('summary')) or _content_text(payload.get('content'))
        if not text:
            trace.omitted_rows += 1
            return
        _append_trace_message(
            trace,
            line_number=line_number,
            timestamp=timestamp,
            kind=f'response_item.{payload_type}',
            role='assistant',
            phase='reasoning',
            title='Reasoning summary',
            text=text,
            category='reasoning',
        )
        return
    if payload_type in {'function_call', 'custom_tool_call', 'web_search_call', 'tool_search_call'}:
        _record_trace_tool_call(
            trace,
            payload=payload,
            payload_type=payload_type,
            line_number=line_number,
            timestamp=timestamp,
        )
        return
    if payload_type in {'function_call_output', 'custom_tool_call_output', 'tool_search_output'}:
        _record_trace_tool_output(
            trace,
            payload=payload,
            payload_type=payload_type,
            line_number=line_number,
            timestamp=timestamp,
        )
        return
    trace.omitted_rows += 1


def _record_trace_tool_call(
    trace: _TraceAccumulator,
    *,
    payload: dict[str, object],
    payload_type: str,
    line_number: int,
    timestamp: str,
) -> None:
    action = payload.get('action')
    action_name = _safe_text(action.get('type')) if isinstance(action, dict) else ''
    tool_name = (
        _safe_text(payload.get('name'))
        or _safe_text(payload.get('execution'))
        or action_name
        or payload_type.removesuffix('_call')
    )
    input_value = payload.get('arguments')
    if input_value is None:
        input_value = payload.get('input')
    if input_value is None:
        input_value = action
    input_text, truncated = _trace_value(input_value, trace.char_limit)
    call_id = _safe_text(payload.get('call_id')) or _safe_text(payload.get('id')) or ''
    event_index = trace.call_events.get(call_id) if call_id else None
    if event_index is not None:
        event = trace.events[event_index]
        if not event.tool_name:
            event.tool_name = tool_name
            event.title = f'Tool: {tool_name}'
        if not event.input_text:
            event.input_text = input_text
            if truncated:
                event.truncated_fields.add('input_text')
        return
    event = _new_trace_event(
        trace,
        line_number=line_number,
        timestamp=timestamp,
        category='tool',
        kind=f'response_item.{payload_type}',
        title=f'Tool: {tool_name}',
        tool_name=tool_name,
        call_id=call_id,
        status=_safe_text(payload.get('status')) or 'called',
        input_text=input_text,
    )
    if truncated:
        event.truncated_fields.add('input_text')
    trace.append(event)
    if call_id:
        trace.call_events[call_id] = len(trace.events) - 1


def _record_trace_tool_output(
    trace: _TraceAccumulator,
    *,
    payload: dict[str, object],
    payload_type: str,
    line_number: int,
    timestamp: str,
) -> None:
    call_id = _safe_text(payload.get('call_id')) or ''
    output_value = payload.get('output')
    if output_value is None:
        output_value = payload.get('tools')
    output_text, truncated = _trace_value(output_value, trace.char_limit)
    event_index = trace.call_events.get(call_id)
    if event_index is not None:
        event = trace.events[event_index]
        event.output_text = output_text
        event.output_line_number = line_number
        event.status = _safe_text(payload.get('status')) or 'completed'
        if truncated:
            event.truncated_fields.add('output_text')
        return
    event = _new_trace_event(
        trace,
        line_number=line_number,
        timestamp=timestamp,
        category='tool',
        kind=f'response_item.{payload_type}',
        title='Unpaired tool output',
        call_id=call_id,
        status=_safe_text(payload.get('status')) or 'completed',
        output_text=output_text,
    )
    if truncated:
        event.truncated_fields.add('output_text')
    trace.append(event)


def _record_trace_tool_completion(
    trace: _TraceAccumulator,
    *,
    payload: dict[str, object],
    payload_type: str,
    line_number: int,
    timestamp: str,
) -> None:
    call_id = _safe_text(payload.get('call_id')) or _safe_text(payload.get('id')) or ''
    invocation = payload.get('invocation')
    if payload_type == 'mcp_tool_call_end':
        invocation_payload = invocation if isinstance(invocation, dict) else {}
        tool_name = (
            _safe_text(invocation_payload.get('tool'))
            or _safe_text(invocation_payload.get('name'))
            or _safe_text(payload.get('tool'))
            or 'mcp'
        )
        input_value = invocation
        output_value = payload.get('result')
    else:
        tool_name = 'apply_patch'
        input_value = _select_trace_fields(payload, 'changes')
        output_value = _select_trace_fields(payload, 'status', 'success', 'changes', 'stdout', 'stderr')
    input_text, input_truncated = _trace_value(input_value, trace.char_limit)
    output_text, output_truncated = _trace_value(output_value, trace.char_limit)
    status = _tool_completion_status(payload)
    duration_secs = _duration_seconds(payload)
    event_index = trace.call_events.get(call_id) if call_id else None
    if event_index is not None:
        event = trace.events[event_index]
        if not event.input_text:
            event.input_text = input_text
            if input_truncated:
                event.truncated_fields.add('input_text')
        event.output_text = output_text
        event.output_line_number = line_number
        event.status = status
        event.duration_secs = duration_secs
        if payload_type == 'patch_apply_end':
            event.turn_id = _safe_text(payload.get('turn_id')) or event.turn_id
        if output_truncated:
            event.truncated_fields.add('output_text')
        return
    event = _new_trace_event(
        trace,
        line_number=line_number,
        timestamp=timestamp,
        category='tool',
        kind=payload_type,
        title=f'Tool: {tool_name}',
        turn_id=_safe_text(payload.get('turn_id')) or None,
        tool_name=tool_name,
        call_id=call_id,
        status=status,
        input_text=input_text,
        output_text=output_text,
        duration_secs=duration_secs,
    )
    if input_truncated:
        event.truncated_fields.add('input_text')
    if output_truncated:
        event.truncated_fields.add('output_text')
    trace.append(event)
    if call_id:
        trace.call_events[call_id] = len(trace.events) - 1


def _tool_completion_status(payload: dict[str, object]) -> str:
    status = _safe_text(payload.get('status'))
    if status:
        return status
    success = payload.get('success')
    if isinstance(success, bool):
        return 'completed' if success else 'failed'
    return 'completed'


def _append_trace_message(
    trace: _TraceAccumulator,
    *,
    line_number: int,
    timestamp: str,
    kind: str,
    role: str,
    phase: str,
    title: str,
    text: str,
    category: str = 'message',
) -> None:
    normalized = text.strip()
    if not normalized:
        trace.omitted_rows += 1
        return
    fingerprint = (trace.current_turn_id, role, normalized)
    if fingerprint in trace.message_fingerprints:
        trace.omitted_rows += 1
        return
    trace.message_fingerprints.add(fingerprint)
    visible_text, truncated = _truncate_trace_text(normalized, trace.char_limit)
    event = _new_trace_event(
        trace,
        line_number=line_number,
        timestamp=timestamp,
        category=category,
        kind=kind,
        title=title,
        role=role,
        phase=phase,
        text=visible_text,
    )
    if truncated:
        event.truncated_fields.add('text')
    trace.append(event)


def _new_trace_event(
    trace: _TraceAccumulator,
    *,
    line_number: int,
    timestamp: str,
    category: str,
    kind: str,
    title: str,
    turn_id: str | None = None,
    role: str = '',
    phase: str = '',
    text: str = '',
    tool_name: str = '',
    call_id: str = '',
    status: str = '',
    input_text: str = '',
    output_text: str = '',
    duration_secs: float | None = None,
) -> _MutableTraceEvent:
    return _MutableTraceEvent(
        turn_id=turn_id or trace.current_turn_id,
        sequence=len(trace.events) + 1,
        line_number=line_number,
        timestamp=timestamp,
        category=category,
        kind=kind,
        title=title,
        role=role,
        phase=phase,
        text=text,
        tool_name=tool_name,
        call_id=call_id,
        status=status,
        input_text=input_text,
        output_text=output_text,
        duration_secs=duration_secs,
    )


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_content_text(item) for item in value]
        return '\n\n'.join(part for part in parts if part)
    if not isinstance(value, dict):
        return ''
    for key in ('text', 'content', 'input_text', 'output_text'):
        text = _content_text(value.get(key))
        if text:
            return text
    value_type = _safe_text(value.get('type')) or ''
    if value_type in {'input_image', 'image', 'audio'}:
        return f'[{value_type} omitted]'
    return ''


def _select_trace_fields(payload: dict[str, object], *keys: str) -> dict[str, object]:
    return {key: payload[key] for key in keys if key in payload and payload[key] not in ('', None)}


def _trace_value(value: object, limit: int) -> tuple[str, bool]:
    if value is None:
        return '', False
    if isinstance(value, str):
        text = value
        stripped = text.strip()
        if stripped.startswith(('{', '[')):
            with suppress(json.JSONDecodeError):
                text = json.dumps(json.loads(stripped), indent=2, sort_keys=True, ensure_ascii=True)
    else:
        text = json.dumps(_sanitize_trace_value(value), indent=2, sort_keys=True, ensure_ascii=True, default=str)
    return _truncate_trace_text(text, limit)


def _sanitize_trace_value(value: object) -> object:
    if isinstance(value, list):
        return [_sanitize_trace_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, object] = {}
    value_type = _safe_text(value.get('type')) or ''
    for key, item in value.items():
        if key == 'encrypted_content':
            continue
        if key in {'data', 'blob'} and value_type in {'image', 'audio', 'resource'} and isinstance(item, str):
            result[key] = f'[binary content omitted: {len(item)} characters]'
        else:
            result[key] = _sanitize_trace_value(item)
    return result


def _truncate_trace_text(value: str, limit: int) -> tuple[str, bool]:
    text = value.strip()
    if len(text) <= limit:
        return text, False
    omitted = len(text) - limit
    return f'{text[:limit]}\n\n[truncated: {omitted} characters omitted]', True


def _duration_seconds(payload: dict[str, object]) -> float | None:
    milliseconds = _finite_float(payload.get('duration_ms'))
    if milliseconds is not None:
        return max(milliseconds / 1000, 0.0)
    duration = payload.get('duration')
    if not isinstance(duration, dict):
        return None
    secs = _finite_float(duration.get('secs'))
    nanos = _finite_float(duration.get('nanos'))
    if secs is None or nanos is None:
        return None
    total = secs + nanos / 1_000_000_000
    return max(total, 0.0) if math.isfinite(total) else None


def _finite_float(value: object) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _append_turn(
    accumulator: _SessionAccumulator,
    turn: TurnSummary,
    *,
    from_time: datetime | None,
    to_time: datetime | None,
) -> bool:
    if not _turn_in_range(turn, from_time=from_time, to_time=to_time):
        return False
    accumulator.turns.append(turn)
    return True


def _record_token_usage(
    payload: dict[str, object],
    *,
    accumulator: _SessionAccumulator,
    current_turn: _TurnAccumulator | None,
    last_total_usage: tuple[int, int, int, int, int] | None,
) -> tuple[int, int, int, int, int] | None:
    usage_info = payload.get('info')
    if not isinstance(usage_info, dict):
        return last_total_usage
    total_payload = usage_info.get('total_token_usage')
    last_payload = usage_info.get('last_token_usage')
    if not isinstance(total_payload, dict) or not isinstance(last_payload, dict):
        return last_total_usage
    total_key = (
        _safe_int(total_payload.get('input_tokens')),
        _safe_int(total_payload.get('cached_input_tokens')),
        _safe_int(total_payload.get('output_tokens')),
        _safe_int(total_payload.get('reasoning_output_tokens')),
        _safe_int(total_payload.get('total_tokens')),
    )
    if total_key == last_total_usage:
        return last_total_usage
    usage = _UsageAccumulator()
    usage.add_payload(last_payload)
    accumulator.add_usage(usage)
    if current_turn is not None:
        current_turn.add_usage(usage)
    return total_key


def _record_response_item(
    payload: dict[str, object],
    *,
    accumulator: _SessionAccumulator,
    current_turn: _TurnAccumulator | None,
) -> None:
    payload_type = _safe_text(payload.get('type'))
    tool_name = ''
    tool_kind = ''
    if payload_type == 'function_call':
        tool_name = _safe_text(payload.get('name')) or 'function_call'
        tool_kind = 'function'
    elif payload_type == 'custom_tool_call':
        tool_name = _safe_text(payload.get('name')) or 'custom_tool_call'
        tool_kind = 'custom'
    elif payload_type == 'web_search_call':
        action = payload.get('action')
        tool_name = _safe_text(action.get('type')) if isinstance(action, dict) else ''
        tool_name = tool_name or 'web_search_call'
        tool_kind = 'web'
    elif payload_type == 'tool_search_call':
        action = payload.get('action')
        action_name = _safe_text(action.get('type')) if isinstance(action, dict) else ''
        tool_name = (
            _safe_text(payload.get('name')) or _safe_text(payload.get('execution')) or action_name or 'tool_search'
        )
        tool_kind = 'tool_search'
    elif payload_type == 'reasoning':
        accumulator.response_reasoning_total += 1
        if current_turn is not None:
            current_turn.response_reasoning_total += 1
        return
    if not tool_name:
        return
    call_id = _safe_text(payload.get('call_id')) or _safe_text(payload.get('id')) or ''
    if call_id and call_id in accumulator.counted_tool_call_ids:
        return
    if call_id:
        accumulator.counted_tool_call_ids.add(call_id)
    accumulator.add_tool(tool_name)
    if payload_type == 'web_search_call':
        pending_calls = (
            current_turn.pending_web_search_calls if current_turn is not None else accumulator.pending_web_search_calls
        )
        pending_calls[_web_search_call_key(payload)] += 1
    if current_turn is None:
        return
    current_turn.tool_counter[tool_name] += 1
    if tool_kind == 'function':
        current_turn.function_call_total += 1
    elif tool_kind == 'custom':
        current_turn.custom_tool_call_total += 1
    elif tool_kind == 'web':
        current_turn.web_search_call_total += 1


def _record_tool_completion(
    payload: dict[str, object],
    *,
    payload_type: str,
    accumulator: _SessionAccumulator,
    current_turn: _TurnAccumulator | None,
) -> None:
    call_id = _safe_text(payload.get('call_id')) or _safe_text(payload.get('id')) or ''
    if call_id and call_id in accumulator.counted_tool_call_ids:
        return
    if call_id:
        accumulator.counted_tool_call_ids.add(call_id)
    if payload_type == 'mcp_tool_call_end':
        invocation = payload.get('invocation')
        invocation_payload = invocation if isinstance(invocation, dict) else {}
        tool_name = (
            _safe_text(invocation_payload.get('tool'))
            or _safe_text(invocation_payload.get('name'))
            or _safe_text(payload.get('tool'))
            or 'mcp'
        )
    else:
        tool_name = 'apply_patch'
    accumulator.add_tool(tool_name)
    if current_turn is not None:
        current_turn.tool_counter[tool_name] += 1


def _web_search_call_key(payload: dict[str, object]) -> str:
    return _safe_text(payload.get('call_id')) or _safe_text(payload.get('id')) or ''


def _record_web_search_end(
    payload: dict[str, object],
    *,
    accumulator: _SessionAccumulator,
    current_turn: _TurnAccumulator | None,
) -> None:
    pending_calls = (
        current_turn.pending_web_search_calls if current_turn is not None else accumulator.pending_web_search_calls
    )
    call_key = _web_search_call_key(payload)
    if pending_calls[call_key]:
        pending_calls[call_key] -= 1
        if not pending_calls[call_key]:
            del pending_calls[call_key]
        return
    accumulator.add_tool('web_search')
    if current_turn is not None:
        current_turn.tool_counter['web_search'] += 1
        current_turn.web_search_call_total += 1


def _apply_session_meta(accumulator: _SessionAccumulator, payload: dict[str, object]) -> None:
    if accumulator.primary_session_meta_seen:
        return
    accumulator.primary_session_meta_seen = True
    accumulator.session_id = _safe_text(payload.get('id')) or accumulator.session_id
    accumulator.session_started_at = parse_timestamp(payload.get('timestamp')) or accumulator.session_started_at
    accumulator.cwd = _safe_text(payload.get('cwd')) or accumulator.cwd
    accumulator.conversation_id = _safe_text(payload.get('conversation_id')) or accumulator.conversation_id
    accumulator.execution_session_id = (
        _safe_text(payload.get('execution_session_id')) or accumulator.execution_session_id
    )
    accumulator.team_id = _safe_text(payload.get('team_id')) or accumulator.team_id
    accumulator.channel_id = _safe_text(payload.get('channel_id')) or accumulator.channel_id
    accumulator.thread_ts = _safe_text(payload.get('thread_ts')) or accumulator.thread_ts
    accumulator.originator = _safe_text(payload.get('originator')) or accumulator.originator
    accumulator.source = _safe_text(payload.get('source')) or accumulator.source
    accumulator.cli_version = _safe_text(payload.get('cli_version')) or accumulator.cli_version
    accumulator.model_provider = _safe_text(payload.get('model_provider')) or accumulator.model_provider
    git_payload = payload.get('git')
    if isinstance(git_payload, dict):
        accumulator.repository_url = _safe_text(git_payload.get('repository_url')) or accumulator.repository_url
        accumulator.repository = _repository_name(accumulator.repository_url) or accumulator.repository
        accumulator.branch = _safe_text(git_payload.get('branch')) or accumulator.branch
        accumulator.commit_hash = _safe_text(git_payload.get('commit_hash')) or accumulator.commit_hash


def _apply_turn_context(
    turn: _TurnAccumulator,
    accumulator: _SessionAccumulator,
    payload: dict[str, object],
) -> None:
    cwd = _safe_text(payload.get('cwd'))
    if cwd:
        turn.cwd = cwd
        accumulator.cwd = cwd
    model = _safe_text(payload.get('model'))
    if model:
        turn.model = model
        accumulator.model = model
    effort = _safe_text(payload.get('effort'))
    if effort:
        turn.effort = effort
        accumulator.effort = effort
    turn.repository = accumulator.repository
    turn.repository_url = accumulator.repository_url
    turn.branch = accumulator.branch
    turn.model_provider = accumulator.model_provider


def _turn_in_range(
    turn: TurnSummary,
    *,
    from_time: datetime | None,
    to_time: datetime | None,
) -> bool:
    started_at = parse_timestamp(turn.started_at)
    if started_at is None:
        return True
    if from_time is not None and started_at < from_time:
        return False
    return not (to_time is not None and started_at > to_time)


def _repository_name(repository_url: str) -> str | None:
    if not repository_url:
        return None
    candidate = repository_url.rstrip('/').rsplit('/', maxsplit=1)[-1]
    if ':' in candidate:
        candidate = candidate.rsplit(':', maxsplit=1)[-1]
    if candidate.endswith('.git'):
        candidate = candidate[:-4]
    return candidate or None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _safe_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _safe_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return 0
    return 0


def _safe_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    return _safe_int(value)


def _mtime_or_zero(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
