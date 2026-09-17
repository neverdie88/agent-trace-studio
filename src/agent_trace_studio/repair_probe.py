"""Privacy-safe parser replay probe used by the repair verifier."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from agent_trace_studio.parser import analyze_journals


def build_parser_probe(source_path: Path, session_id: str) -> dict[str, object]:
    """Return structural parser output without trace text or tool payloads."""

    result = analyze_journals([source_path], include_trace=True)
    trace = next((item for item in result.traces if item.session_id == session_id), None)
    session = next((item for item in result.sessions if item.session_id == session_id), None)
    if trace is None or session is None:
        raise ValueError(f'parser did not produce session {session_id}')
    category_counts = Counter(event.category for event in trace.events)
    kind_counts = Counter(event.kind for event in trace.events)
    role_counts = Counter(event.role for event in trace.events if event.role)
    tool_counts = Counter(event.tool_name for event in trace.events if event.tool_name)
    status_counts = Counter(event.status for event in trace.events if event.status)
    return {
        'session_id': session_id,
        'source_rows': trace.source_rows,
        'events_total': trace.events_total,
        'collapsed_rows': trace.collapsed_rows,
        'truncated_events': trace.truncated_events,
        'turns_total': session.turns_total,
        'completed_turns': session.completed_turns,
        'aborted_turns': session.aborted_turns,
        'incomplete_turns': session.incomplete_turns,
        'skipped_lines': session.skipped_lines,
        'category_counts': dict(sorted(category_counts.items())),
        'kind_counts': dict(sorted(kind_counts.items())),
        'role_counts': dict(sorted(role_counts.items())),
        'tool_counts': dict(sorted(tool_counts.items())),
        'status_counts': dict(sorted(status_counts.items())),
        'issues': [
            {'reason': issue.reason, 'count': issue.count}
            for issue in result.issues
            if issue.session_file == str(source_path.resolve())
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('source_path', type=Path)
    parser.add_argument('session_id')
    args = parser.parse_args()
    print(json.dumps(build_parser_probe(args.source_path.resolve(), args.session_id), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
