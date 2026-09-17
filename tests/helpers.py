"""Synthetic Codex journal fixtures used by unit tests."""

from __future__ import annotations

import json
from pathlib import Path


def write_journal(
    path: Path,
    *,
    session_id: str = 'session-1',
    include_malformed: bool = False,
    complete_second_turn: bool = False,
) -> None:
    rows: list[object] = [
        {
            'timestamp': '2026-08-10T01:00:00Z',
            'type': 'session_meta',
            'payload': {
                'id': session_id,
                'timestamp': '2026-08-10T01:00:00Z',
                'cwd': '/workspace/example',
                'conversation_id': 'conversation-1',
                'originator': 'codex_desktop',
                'source': 'desktop',
                'cli_version': '1.0.0',
                'model_provider': 'openai',
                'git': {
                    'repository_url': 'ssh://git@example.test/team/example.git',
                    'branch': 'feature/dashboard',
                    'commit_hash': 'abc123',
                },
            },
        },
        {
            'timestamp': '2026-08-10T01:00:01Z',
            'type': 'turn_context',
            'payload': {'cwd': '/workspace/example', 'model': 'gpt-5', 'effort': 'medium'},
        },
        {
            'timestamp': '2026-08-10T01:00:02Z',
            'type': 'event_msg',
            'payload': {
                'type': 'task_started',
                'turn_id': 'turn-1',
                'model_context_window': 258400,
                'collaboration_mode_kind': 'default',
            },
        },
        {
            'timestamp': '2026-08-10T01:00:03Z',
            'type': 'response_item',
            'payload': {
                'type': 'function_call',
                'name': 'exec_command',
                'call_id': 'call-1',
                'arguments': '{"secret":"TOP_SECRET_TOOL_INPUT"}',
            },
        },
        {
            'timestamp': '2026-08-10T01:00:03.100Z',
            'type': 'response_item',
            'payload': {
                'type': 'function_call_output',
                'call_id': 'call-1',
                'output': 'TOP_SECRET_TOOL_OUTPUT',
            },
        },
        {
            'timestamp': '2026-08-10T01:00:04Z',
            'type': 'event_msg',
            'payload': {
                'type': 'token_count',
                'info': {
                    'total_token_usage': {
                        'input_tokens': 100,
                        'cached_input_tokens': 50,
                        'output_tokens': 20,
                        'reasoning_output_tokens': 5,
                        'total_tokens': 125,
                    },
                    'last_token_usage': {
                        'input_tokens': 100,
                        'cached_input_tokens': 50,
                        'output_tokens': 20,
                        'reasoning_output_tokens': 5,
                        'total_tokens': 125,
                    },
                },
            },
        },
        {
            'timestamp': '2026-08-10T01:00:05Z',
            'type': 'event_msg',
            'payload': {
                'type': 'token_count',
                'info': {
                    'total_token_usage': {
                        'input_tokens': 100,
                        'cached_input_tokens': 50,
                        'output_tokens': 20,
                        'reasoning_output_tokens': 5,
                        'total_tokens': 125,
                    },
                    'last_token_usage': {
                        'input_tokens': 100,
                        'cached_input_tokens': 50,
                        'output_tokens': 20,
                        'reasoning_output_tokens': 5,
                        'total_tokens': 125,
                    },
                },
            },
        },
        {
            'timestamp': '2026-08-10T01:00:06Z',
            'type': 'response_item',
            'payload': {'type': 'custom_tool_call', 'name': 'apply_patch'},
        },
        {
            'timestamp': '2026-08-10T01:00:07Z',
            'type': 'response_item',
            'payload': {'type': 'web_search_call', 'action': {'type': 'search'}},
        },
        {
            'timestamp': '2026-08-10T01:00:08Z',
            'type': 'event_msg',
            'payload': {'type': 'agent_reasoning', 'text': 'Synthetic reasoning'},
        },
        {
            'timestamp': '2026-08-10T01:00:09Z',
            'type': 'response_item',
            'payload': {
                'type': 'reasoning',
                'summary': [],
                'encrypted_content': 'TOP_SECRET_ENCRYPTED_REASONING',
            },
        },
        {
            'timestamp': '2026-08-10T01:00:10Z',
            'type': 'event_msg',
            'payload': {'type': 'context_compacted'},
        },
        {
            'timestamp': '2026-08-10T01:00:11Z',
            'type': 'event_msg',
            'payload': {'type': 'task_complete', 'turn_id': 'turn-1', 'last_agent_message': 'Done'},
        },
        {
            'timestamp': '2026-08-10T01:00:12Z',
            'type': 'event_msg',
            'payload': {'type': 'task_started', 'turn_id': 'turn-2'},
        },
        {
            'timestamp': '2026-08-10T01:00:13Z',
            'type': 'response_item',
            'payload': {'type': 'function_call', 'name': 'exec_command'},
        },
        {
            'timestamp': '2026-08-10T01:00:14Z',
            'type': 'event_msg',
            'payload': {'type': 'task_complete' if complete_second_turn else 'turn_aborted'},
        },
    ]
    serialized = [json.dumps(row) for row in rows]
    if include_malformed:
        serialized.insert(3, '{bad-json')
        serialized.insert(4, json.dumps(['not', 'an', 'object']))
    path.write_text('\n'.join(serialized) + '\n', encoding='utf-8')
