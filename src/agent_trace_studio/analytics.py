"""Aggregate parsed agent traces into dashboard and export data."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from statistics import mean

from agent_trace_studio.models import AnalysisResult, SessionSummary, SessionTrace, TurnSummary

PRODUCT_TITLE = 'Agent Trace Studio'


def build_dashboard_payload(
    result: AnalysisResult,
    *,
    title: str,
    assurance: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build the JSON payload consumed by the static dashboard."""

    session_rows = [_session_row(session) for session in result.sessions]
    turn_rows = [_turn_row(turn) for turn in result.turns]
    payload: dict[str, object] = {
        'meta': {
            'title': PRODUCT_TITLE,
            'trace_set_title': title if title != PRODUCT_TITLE else '',
            'generated_at': result.generated_at,
            'source_count': len(result.source_paths),
            'skipped_file_count': len(result.skipped_files),
            'trace_enabled': bool(result.traces),
        },
        'overview': overview_metrics(result),
        'activity': activity_rows(result),
        'breakdowns': {
            'tools': tool_rows(result.turns),
            'repositories': _counter_rows(Counter(session.repository for session in result.sessions)),
            'models': _counter_rows(Counter(turn.model for turn in result.turns)),
            'statuses': _counter_rows(Counter(turn.status for turn in result.turns)),
        },
        'sessions': session_rows,
        'turns': turn_rows,
        'traces': [_trace_row(trace) for trace in result.traces],
        'issues': [asdict(issue) for issue in result.issues],
        'exports': [
            {'label': 'Sessions CSV', 'path': 'sessions.csv'},
            {'label': 'Turns CSV', 'path': 'turns.csv'},
            {'label': 'Metrics CSV', 'path': 'metrics.csv'},
            {'label': 'Analysis JSON', 'path': 'analysis.json'},
            {'label': 'Manifest JSON', 'path': 'manifest.json'},
        ],
    }
    if assurance is not None:
        payload['assurance'] = assurance
    return payload


def overview_metrics(result: AnalysisResult) -> dict[str, float | int | str]:
    """Return top-level usage, latency, status, and data-quality measures."""

    turns = result.turns
    sessions = result.sessions
    durations = [turn.duration_secs for turn in turns if turn.duration_secs is not None]
    completed = sum(turn.status == 'completed' for turn in turns)
    aborted = sum(turn.status == 'aborted' for turn in turns)
    incomplete = sum(turn.status == 'incomplete' for turn in turns)
    tools = Counter[str]()
    for turn in turns:
        tools.update(dict(turn.tool_breakdown))
    conversation_keys = {
        key
        for session in sessions
        if (key := session.thread_ts or session.conversation_id or session.execution_session_id)
    }
    activity_dates = sorted({turn.started_at[:10] for turn in turns if turn.started_at})
    malformed_lines = sum(
        issue.count for issue in result.issues if issue.reason in {'malformed_json_line', 'non_object_json_line'}
    )
    return {
        'sessions_total': len(sessions),
        'conversations_total': len(conversation_keys),
        'turns_total': len(turns),
        'completed_turns': completed,
        'aborted_turns': aborted,
        'incomplete_turns': incomplete,
        'completed_rate': completed / len(turns) if turns else 0.0,
        'prompt_tokens': sum(turn.prompt_tokens for turn in turns),
        'cached_input_tokens': sum(turn.cached_input_tokens for turn in turns),
        'completion_tokens': sum(turn.completion_tokens for turn in turns),
        'reasoning_tokens': sum(turn.reasoning_tokens for turn in turns),
        'total_tokens': sum(turn.total_tokens for turn in turns),
        'tool_calls_total': sum(tools.values()),
        'unique_tools': len(tools),
        'unique_repositories': len(
            {session.repository for session in sessions if session.repository and session.repository != 'unknown'}
        ),
        'context_compacted_total': sum(turn.context_compacted_total for turn in turns),
        'agent_reasoning_total': sum(turn.agent_reasoning_total for turn in turns),
        'response_reasoning_total': sum(turn.response_reasoning_total for turn in turns),
        'avg_turns_per_session': len(turns) / len(sessions) if sessions else 0.0,
        'avg_turn_duration_secs': mean(durations) if durations else 0.0,
        'p50_turn_duration_secs': _percentile(durations, 0.50),
        'p95_turn_duration_secs': _percentile(durations, 0.95),
        'active_days': len(activity_dates),
        'first_activity_date': activity_dates[0] if activity_dates else '',
        'last_activity_date': activity_dates[-1] if activity_dates else '',
        'skipped_files': len(result.skipped_files),
        'parse_issue_count': sum(issue.count for issue in result.issues),
        'malformed_lines': malformed_lines,
    }


def activity_rows(result: AnalysisResult) -> list[dict[str, object]]:
    """Build one activity row per UTC date."""

    sessions_by_day: Counter[str] = Counter()
    turns_by_day: Counter[str] = Counter()
    tokens_by_day: Counter[str] = Counter()
    tools_by_day: Counter[str] = Counter()
    for session in result.sessions:
        day = (session.first_turn_started_at or session.session_started_at or '')[:10]
        if day:
            sessions_by_day[day] += 1
    for turn in result.turns:
        day = turn.started_at[:10]
        if not day:
            continue
        turns_by_day[day] += 1
        tokens_by_day[day] += turn.total_tokens
        tools_by_day[day] += turn.tool_call_total
    days = sorted(set(sessions_by_day) | set(turns_by_day))
    return [
        {
            'date': day,
            'sessions': sessions_by_day[day],
            'turns': turns_by_day[day],
            'tokens': tokens_by_day[day],
            'tools': tools_by_day[day],
        }
        for day in days
    ]


def metric_rows(result: AnalysisResult) -> list[dict[str, object]]:
    """Build normalized long-form metrics for CSV export."""

    rows: list[dict[str, object]] = []
    session_daily: Counter[tuple[str, str]] = Counter()
    conversation_daily: defaultdict[str, set[str]] = defaultdict(set)
    turn_daily: Counter[tuple[str, str]] = Counter()
    token_daily: Counter[tuple[str, str]] = Counter()
    tools: Counter[str] = Counter()
    for session in result.sessions:
        day = (session.first_turn_started_at or session.session_started_at or '')[:10]
        session_daily[(day, session.repository)] += 1
        conversation_key = session.thread_ts or session.conversation_id or session.execution_session_id
        if day and conversation_key:
            conversation_daily[day].add(conversation_key)
    for turn in result.turns:
        day = turn.started_at[:10]
        turn_daily[(day, turn.status)] += 1
        token_daily[(day, 'prompt_tokens')] += turn.prompt_tokens
        token_daily[(day, 'completion_tokens')] += turn.completion_tokens
        token_daily[(day, 'reasoning_tokens')] += turn.reasoning_tokens
        token_daily[(day, 'cached_input_tokens')] += turn.cached_input_tokens
        tools.update(dict(turn.tool_breakdown))
    for (day, repository), value in sorted(session_daily.items()):
        rows.append(_metric_row('session_seen_total', 'day', value, date_value=day, repository=repository))
    for day, values in sorted(conversation_daily.items()):
        rows.append(_metric_row('conversation_seen_total', 'day', len(values), date_value=day))
    for (day, status), value in sorted(turn_daily.items()):
        rows.append(_metric_row('turn_total', 'day', value, date_value=day, status=status))
    for (day, name), value in sorted(token_daily.items()):
        rows.append(_metric_row(name, 'day', value, date_value=day))
    for name, value in tools.most_common():
        rows.append(_metric_row('codex_tool_call_total', 'tool', value, tool_name=name))
    return rows


def tool_rows(turns: tuple[TurnSummary, ...]) -> list[dict[str, object]]:
    counts: Counter[str] = Counter()
    for turn in turns:
        counts.update(dict(turn.tool_breakdown))
    return [{'name': name, 'value': value} for name, value in counts.most_common()]


def _session_row(session: SessionSummary) -> dict[str, object]:
    row = asdict(session)
    row['started_at'] = session.first_turn_started_at or session.session_started_at or ''
    row['completion_rate'] = session.completed_turns / session.turns_total if session.turns_total else 0.0
    return row


def _turn_row(turn: TurnSummary) -> dict[str, object]:
    row = asdict(turn)
    row['tool_breakdown'] = dict(turn.tool_breakdown)
    return row


def _trace_row(trace: SessionTrace) -> dict[str, object]:
    row = asdict(trace)
    row['events'] = [asdict(event) for event in trace.events]
    return row


def _counter_rows(counter: Counter[str]) -> list[dict[str, object]]:
    return [
        {'name': name or 'unknown', 'value': value}
        for name, value in counter.most_common()
        if name and name != 'unknown'
    ]


def _metric_row(
    metric_name: str,
    grain: str,
    value: int | float,
    *,
    date_value: str = '',
    repository: str = '',
    status: str = '',
    tool_name: str = '',
) -> dict[str, object]:
    return {
        'metric_name': metric_name,
        'grain': grain,
        'date_value': date_value,
        'repository': repository,
        'status': status,
        'tool_name': tool_name,
        'value': float(value),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
