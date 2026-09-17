from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agent_trace_studio.parser import (
    SessionResolutionError,
    analyze_journals,
    discover_journal_paths,
    resolve_session_ids,
)
from helpers import write_journal


class ParserTest(unittest.TestCase):
    def test_reconstructs_turns_and_deduplicates_token_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)

            result = analyze_journals([path], generated_at=datetime(2026, 8, 16, tzinfo=UTC))

        self.assertEqual(len(result.sessions), 1)
        self.assertEqual(len(result.turns), 2)
        by_id = {turn.turn_id: turn for turn in result.turns}
        completed = by_id['turn-1']
        aborted = by_id['turn-2']
        self.assertEqual(completed.status, 'completed')
        self.assertEqual(completed.total_tokens, 125)
        self.assertEqual(completed.prompt_tokens, 100)
        self.assertEqual(completed.cached_input_tokens, 50)
        self.assertEqual(completed.completion_tokens, 20)
        self.assertEqual(completed.reasoning_tokens, 5)
        self.assertEqual(completed.tool_call_total, 3)
        self.assertEqual(dict(completed.tool_breakdown), {'apply_patch': 1, 'exec_command': 1, 'search': 1})
        self.assertEqual(completed.context_compacted_total, 1)
        self.assertEqual(completed.agent_reasoning_total, 1)
        self.assertEqual(completed.response_reasoning_total, 1)
        self.assertEqual(aborted.status, 'aborted')
        self.assertEqual(aborted.tool_call_total, 1)
        self.assertEqual(result.sessions[0].total_tokens, 125)
        self.assertEqual(result.sessions[0].tool_call_total, 4)

    def test_opt_in_trace_pairs_tool_outputs_and_omits_encrypted_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)

            result = analyze_journals([path], include_trace=True, trace_char_limit=20)

        self.assertEqual(len(result.traces), 1)
        trace = result.traces[0]
        tool_event = next(event for event in trace.events if event.call_id == 'call-1')
        self.assertEqual(tool_event.tool_name, 'exec_command')
        self.assertIn('TOP_S', tool_event.input_text)
        self.assertIn('TOP_SECRET_TOOL_', tool_event.output_text)
        self.assertEqual(tool_event.output_line_number, tool_event.line_number + 1)
        self.assertEqual(set(tool_event.truncated_fields), {'input_text', 'output_text'})
        serialized = json.dumps([event.__dict__ for event in trace.events])
        self.assertNotIn('TOP_SECRET_ENCRYPTED_REASONING', serialized)

    def test_counts_tool_search_and_only_standalone_web_search_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'searches.jsonl'
            rows = [
                {
                    'timestamp': '2026-08-11T00:00:00Z',
                    'type': 'session_meta',
                    'payload': {'id': 'search-session', 'timestamp': '2026-08-11T00:00:00Z'},
                },
                {
                    'timestamp': '2026-08-11T00:00:01Z',
                    'type': 'event_msg',
                    'payload': {'type': 'task_started', 'turn_id': 'search-turn'},
                },
                {
                    'timestamp': '2026-08-11T00:00:02Z',
                    'type': 'response_item',
                    'payload': {'type': 'tool_search_call', 'name': 'discover_tools', 'call_id': 'tools-1'},
                },
                {
                    'timestamp': '2026-08-11T00:00:03Z',
                    'type': 'response_item',
                    'payload': {'type': 'tool_search_output', 'call_id': 'tools-1', 'tools': ['synthetic_tool']},
                },
                {
                    'timestamp': '2026-08-11T00:00:04Z',
                    'type': 'event_msg',
                    'payload': {'type': 'web_search_end', 'call_id': 'standalone-1', 'query': 'synthetic query'},
                },
                {
                    'timestamp': '2026-08-11T00:00:05Z',
                    'type': 'response_item',
                    'payload': {
                        'type': 'web_search_call',
                        'call_id': 'web-1',
                        'action': {'type': 'search', 'query': 'paired synthetic query'},
                    },
                },
                {
                    'timestamp': '2026-08-11T00:00:06Z',
                    'type': 'event_msg',
                    'payload': {'type': 'web_search_end', 'call_id': 'web-1', 'results': []},
                },
                {
                    'timestamp': '2026-08-11T00:00:07Z',
                    'type': 'event_msg',
                    'payload': {'type': 'task_complete', 'turn_id': 'search-turn'},
                },
            ]
            path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')

            result = analyze_journals([path], include_trace=True)

        turn = result.turns[0]
        self.assertEqual(turn.tool_call_total, 3)
        self.assertEqual(turn.web_search_call_total, 2)
        self.assertEqual(dict(turn.tool_breakdown), {'discover_tools': 1, 'search': 1, 'web_search': 1})
        self.assertEqual(result.sessions[0].tool_call_total, 3)
        tool_events = [event for event in result.traces[0].events if event.category == 'tool']
        self.assertEqual(
            [event.kind for event in tool_events],
            [
                'response_item.tool_search_call',
                'web_search_end',
                'response_item.web_search_call',
                'web_search_end',
            ],
        )
        self.assertEqual(tool_events[0].output_line_number, 4)

    def test_normalizes_and_deduplicates_mcp_and_patch_completions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tool-completions.jsonl'
            rows = [
                {
                    'timestamp': '2026-08-12T00:00:00Z',
                    'type': 'session_meta',
                    'payload': {'id': 'completion-session', 'timestamp': '2026-08-12T00:00:00Z'},
                },
                {
                    'timestamp': '2026-08-12T00:00:01Z',
                    'type': 'event_msg',
                    'payload': {'type': 'task_started', 'turn_id': 'completion-turn'},
                },
                {
                    'timestamp': '2026-08-12T00:00:02Z',
                    'type': 'event_msg',
                    'payload': {
                        'type': 'mcp_tool_call_end',
                        'call_id': 'mcp-standalone',
                        'invocation': {
                            'server': 'synthetic-server',
                            'tool': 'lookup',
                            'arguments': {'query': 'synthetic'},
                        },
                        'duration_ms': 250,
                        'result': {'content': 'synthetic MCP result'},
                    },
                },
                {
                    'timestamp': '2026-08-12T00:00:03Z',
                    'type': 'response_item',
                    'payload': {
                        'type': 'custom_tool_call',
                        'name': 'apply_patch',
                        'call_id': 'patch-paired',
                        'input': 'synthetic patch',
                    },
                },
                {
                    'timestamp': '2026-08-12T00:00:04Z',
                    'type': 'event_msg',
                    'payload': {
                        'type': 'patch_apply_end',
                        'turn_id': 'completion-turn',
                        'call_id': 'patch-paired',
                        'success': True,
                        'status': 'completed',
                        'changes': {'modified': ['synthetic.txt']},
                        'stdout': 'paired patch output',
                        'stderr': '',
                    },
                },
                {
                    'timestamp': '2026-08-12T00:00:05Z',
                    'type': 'response_item',
                    'payload': {
                        'type': 'function_call',
                        'name': 'paired_lookup',
                        'call_id': 'mcp-paired',
                        'arguments': {'query': 'paired'},
                    },
                },
                {
                    'timestamp': '2026-08-12T00:00:06Z',
                    'type': 'event_msg',
                    'payload': {
                        'type': 'mcp_tool_call_end',
                        'call_id': 'mcp-paired',
                        'invocation': {'server': 'synthetic-server', 'tool': 'lookup', 'arguments': {}},
                        'result': {'content': 'paired MCP result'},
                    },
                },
                {
                    'timestamp': '2026-08-12T00:00:07Z',
                    'type': 'event_msg',
                    'payload': {
                        'type': 'patch_apply_end',
                        'turn_id': 'completion-turn',
                        'call_id': 'patch-standalone',
                        'success': False,
                        'changes': {'modified': ['other-synthetic.txt']},
                        'stdout': 'standalone patch output that is deliberately long',
                        'stderr': 'synthetic failure',
                    },
                },
                {
                    'timestamp': '2026-08-12T00:00:08Z',
                    'type': 'event_msg',
                    'payload': {'type': 'task_complete', 'turn_id': 'completion-turn'},
                },
            ]
            path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')

            result = analyze_journals([path], include_trace=True, trace_char_limit=150)

        turn = result.turns[0]
        self.assertEqual(turn.tool_call_total, 4)
        self.assertEqual(dict(turn.tool_breakdown), {'apply_patch': 2, 'lookup': 1, 'paired_lookup': 1})
        self.assertEqual(result.sessions[0].tool_call_total, 4)
        tool_events = [event for event in result.traces[0].events if event.category == 'tool']
        self.assertEqual(len(tool_events), 4)
        by_call_id = {event.call_id: event for event in tool_events}
        standalone_mcp = by_call_id['mcp-standalone']
        self.assertEqual(standalone_mcp.kind, 'mcp_tool_call_end')
        self.assertEqual(standalone_mcp.tool_name, 'lookup')
        self.assertIn('synthetic-server', standalone_mcp.input_text)
        self.assertIn('synthetic MCP result', standalone_mcp.output_text)
        self.assertEqual(standalone_mcp.duration_secs, 0.25)
        self.assertEqual(by_call_id['mcp-paired'].output_line_number, 7)
        paired_patch = by_call_id['patch-paired']
        self.assertEqual(paired_patch.output_line_number, 5)
        self.assertIn('paired patch output', paired_patch.output_text)
        standalone_patch = by_call_id['patch-standalone']
        self.assertEqual(standalone_patch.turn_id, 'completion-turn')
        self.assertEqual(standalone_patch.status, 'failed')
        self.assertIn('other-synthetic.txt', standalone_patch.input_text)
        self.assertIn('output_text', standalone_patch.truncated_fields)
        self.assertIn('[truncated:', standalone_patch.output_text)

    def test_normalizes_structured_mcp_durations_tolerantly(self) -> None:
        duration_fields = (
            ('fractional', {'duration': {'secs': 1, 'nanos': 250_000_000}}),
            ('zero', {'duration': {'secs': 0, 'nanos': 0}}),
            ('malformed', {'duration': {'secs': 'invalid', 'nanos': 1}}),
            ('negative', {'duration': {'secs': -1, 'nanos': -250_000_000}}),
            ('oversized-secs', {'duration': {'secs': 10**400, 'nanos': 0}}),
            ('oversized-nanos', {'duration': {'secs': 0, 'nanos': 10**400}}),
            ('infinite', {'duration': {'secs': float('inf'), 'nanos': 0}}),
            ('nan', {'duration': {'secs': 0, 'nanos': float('nan')}}),
            ('oversized-ms', {'duration_ms': 10**400}),
            ('infinite-ms', {'duration_ms': float('inf')}),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mcp-durations.jsonl'
            rows = [
                {
                    'timestamp': '2026-08-12T00:00:00Z',
                    'type': 'session_meta',
                    'payload': {'id': 'duration-session', 'timestamp': '2026-08-12T00:00:00Z'},
                },
                {
                    'timestamp': '2026-08-12T00:00:01Z',
                    'type': 'event_msg',
                    'payload': {'type': 'task_started', 'turn_id': 'duration-turn'},
                },
                *[
                    {
                        'timestamp': f'2026-08-12T00:00:{index:02d}Z',
                        'type': 'event_msg',
                        'payload': {
                            'type': 'mcp_tool_call_end',
                            'call_id': call_id,
                            'invocation': {'server': 'synthetic-server', 'tool': 'lookup'},
                            **fields,
                            'result': {'content': 'synthetic result'},
                        },
                    }
                    for index, (call_id, fields) in enumerate(duration_fields, start=2)
                ],
                {
                    'timestamp': '2026-08-12T00:00:12Z',
                    'type': 'event_msg',
                    'payload': {'type': 'task_complete', 'turn_id': 'duration-turn'},
                },
            ]
            path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')

            result = analyze_journals([path], include_trace=True)

        by_call_id = {event.call_id: event for event in result.traces[0].events if event.category == 'tool'}
        self.assertEqual(by_call_id['fractional'].duration_secs, 1.25)
        self.assertEqual(by_call_id['zero'].duration_secs, 0.0)
        self.assertIsNone(by_call_id['malformed'].duration_secs)
        self.assertEqual(by_call_id['negative'].duration_secs, 0.0)
        for call_id, _ in duration_fields[4:]:
            with self.subTest(call_id=call_id):
                self.assertIsNone(by_call_id[call_id].duration_secs)

    def test_surfaces_malformed_rows_without_dropping_valid_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path, include_malformed=True)
            path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')

            result = analyze_journals([path])

        self.assertEqual(len(result.sessions), 1)
        self.assertEqual(result.sessions[0].skipped_lines, 2)
        issues = {(issue.reason, issue.count) for issue in result.issues}
        self.assertEqual(issues, {('malformed_json_line', 1), ('non_object_json_line', 1)})

    def test_marks_open_turn_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'open.jsonl'
            rows = [
                {
                    'timestamp': '2026-08-11T00:00:00Z',
                    'type': 'session_meta',
                    'payload': {'id': 'open-session', 'timestamp': '2026-08-11T00:00:00Z'},
                },
                {
                    'timestamp': '2026-08-11T00:00:01Z',
                    'type': 'event_msg',
                    'payload': {'type': 'task_started', 'turn_id': 'open-turn'},
                },
                {
                    'timestamp': '2026-08-11T00:00:02Z',
                    'type': 'response_item',
                    'payload': {'type': 'function_call', 'name': 'read_file'},
                },
            ]
            path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')

            result = analyze_journals([path])

        self.assertEqual(result.turns[0].status, 'incomplete')
        self.assertEqual(result.turns[0].duration_secs, 1.0)

    def test_primary_session_id_owns_imported_history_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path, session_id='root-session')
            rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            second_turn_index = next(
                index for index, row in enumerate(rows) if row.get('payload', {}).get('turn_id') == 'turn-2'
            )
            rows.insert(
                second_turn_index,
                {
                    'timestamp': '2026-08-10T01:00:11.500Z',
                    'type': 'session_meta',
                    'payload': {'id': 'imported-ancestor', 'timestamp': '2026-08-01T00:00:00Z'},
                },
            )
            path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')

            result = analyze_journals([path], include_trace=True)

        self.assertEqual(result.sessions[0].session_id, 'root-session')
        self.assertEqual({turn.session_id for turn in result.turns}, {'root-session'})
        self.assertEqual(result.traces[0].session_id, 'root-session')

    def test_filters_turns_by_inclusive_timestamp_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)

            result = analyze_journals(
                [path],
                from_time=datetime(2026, 8, 10, 1, 0, 12, tzinfo=UTC),
                to_time=datetime(2026, 8, 10, 1, 0, 12, tzinfo=UTC),
            )

        self.assertEqual([turn.turn_id for turn in result.turns], ['turn-2'])
        self.assertEqual(result.sessions[0].turns_total, 1)

    def test_discovers_files_directories_and_globs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / 'first.jsonl'
            second = root / 'nested' / 'second.jsonl'
            second.parent.mkdir()
            write_journal(first)
            write_journal(second)

            directory_paths = discover_journal_paths([root])
            glob_paths = discover_journal_paths([str(root / '**' / '*.jsonl')])

        self.assertEqual(set(directory_paths), {first.resolve(), second.resolve()})
        self.assertEqual(set(glob_paths), {first.resolve(), second.resolve()})

    def test_resolves_exact_session_id_from_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            session_id = '00000000-0000-4000-8000-000000000001'
            path = codex_home / 'sessions' / '2026' / '08' / '17' / f'rollout-{session_id}.jsonl'
            path.parent.mkdir(parents=True)
            write_journal(path, session_id=session_id)

            resolved = resolve_session_ids([session_id], codex_home=codex_home)

        self.assertEqual(resolved, (path.resolve(),))

    def test_session_id_resolution_requires_exact_metadata_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            requested = '00000000-0000-4000-8000-000000000001'
            path = codex_home / 'sessions' / f'rollout-{requested}.jsonl'
            path.parent.mkdir(parents=True)
            write_journal(path, session_id='different-session')

            with self.assertRaisesRegex(SessionResolutionError, 'was not found under'):
                resolve_session_ids([requested], codex_home=codex_home)

    def test_parses_json_array_and_content_text_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jsonl_path = root / 'source.jsonl'
            write_journal(jsonl_path)
            rows = [json.loads(line) for line in jsonl_path.read_text(encoding='utf-8').splitlines()]
            array_path = root / 'session-array.json'
            array_path.write_text(json.dumps(rows), encoding='utf-8')
            wrapper_path = root / 'session-wrapper.json'
            wrapper_path.write_text(
                json.dumps({'content_text': jsonl_path.read_text(encoding='utf-8')}),
                encoding='utf-8',
            )

            array_result = analyze_journals([array_path])
            wrapper_result = analyze_journals([wrapper_path])

        self.assertEqual(len(array_result.sessions), 1)
        self.assertEqual(len(array_result.turns), 2)
        self.assertEqual(len(wrapper_result.sessions), 1)
        self.assertEqual(len(wrapper_result.turns), 2)

    def test_parses_claude_code_transcript_into_normalized_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'claude-session.jsonl'
            rows = [
                {
                    'type': 'user',
                    'sessionId': 'claude-session-1',
                    'uuid': 'prompt-1',
                    'timestamp': '2026-08-20T00:00:00Z',
                    'cwd': '/workspace/claude-example',
                    'version': '2.1.0',
                    'message': {'role': 'user', 'content': 'Inspect the parser.'},
                },
                {
                    'type': 'assistant',
                    'sessionId': 'claude-session-1',
                    'timestamp': '2026-08-20T00:00:01Z',
                    'cwd': '/workspace/claude-example',
                    'message': {
                        'role': 'assistant',
                        'model': 'claude-sonnet-test',
                        'content': [
                            {
                                'type': 'tool_use',
                                'id': 'tool-1',
                                'name': 'Read',
                                'input': {'file_path': 'parser.py'},
                            }
                        ],
                        'usage': {'input_tokens': 10, 'cache_read_input_tokens': 2, 'output_tokens': 4},
                        'stop_reason': 'tool_use',
                    },
                },
                {
                    'type': 'user',
                    'sessionId': 'claude-session-1',
                    'timestamp': '2026-08-20T00:00:02Z',
                    'message': {
                        'role': 'user',
                        'content': [{'type': 'tool_result', 'tool_use_id': 'tool-1', 'content': 'file contents'}],
                    },
                },
                {
                    'type': 'assistant',
                    'sessionId': 'claude-session-1',
                    'timestamp': '2026-08-20T00:00:03Z',
                    'message': {
                        'role': 'assistant',
                        'model': 'claude-sonnet-test',
                        'content': [{'type': 'text', 'text': 'Parser inspected.'}],
                        'stop_reason': 'end_turn',
                    },
                },
            ]
            path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')

            result = analyze_journals([path], include_trace=True)

        self.assertEqual(result.sessions[0].session_id, 'claude-session-1')
        self.assertEqual(result.sessions[0].source, 'claude_code')
        self.assertEqual(result.turns[0].status, 'completed')
        self.assertEqual(result.turns[0].tool_breakdown, (('Read', 1),))
        self.assertEqual(result.turns[0].total_tokens, 16)
        tool = next(event for event in result.traces[0].events if event.tool_name == 'Read')
        self.assertIn('parser.py', tool.input_text)
        self.assertEqual(tool.output_text, 'file contents')
        self.assertTrue(any(event.text == 'Parser inspected.' for event in result.traces[0].events))

    def test_parses_canonical_live_event_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stream.jsonl'
            rows = [
                {
                    'timestamp': '2026-08-20T01:00:00Z',
                    'type': 'session_meta',
                    'payload': {'id': 'stream-1', 'source': 'langgraph', 'cwd': '/workspace/graph'},
                },
                {
                    'timestamp': '2026-08-20T01:00:00Z',
                    'type': 'event_msg',
                    'payload': {'type': 'task_started', 'turn_id': 'thread-1'},
                },
                {
                    'timestamp': '2026-08-20T01:00:01Z',
                    'type': 'agent_trace_event',
                    'payload': {
                        'turn_id': 'thread-1',
                        'category': 'tool',
                        'kind': 'task_finished',
                        'tool_name': 'search_docs',
                        'call_id': 'call-1',
                        'status': 'completed',
                        'input': {'query': 'live tracing'},
                        'output': {'matches': 3},
                    },
                },
                {
                    'timestamp': '2026-08-20T01:00:02Z',
                    'type': 'event_msg',
                    'payload': {'type': 'task_complete', 'turn_id': 'thread-1'},
                },
            ]
            path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')

            result = analyze_journals([path], include_trace=True)

        self.assertEqual(result.sessions[0].source, 'langgraph')
        self.assertEqual(result.turns[0].tool_breakdown, (('search_docs', 1),))
        event = next(event for event in result.traces[0].events if event.kind == 'task_finished')
        self.assertEqual(event.title, 'Tool: search_docs')
        self.assertIn('live tracing', event.input_text)
        self.assertIn('3', event.output_text)


if __name__ == '__main__':
    unittest.main()
