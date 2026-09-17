from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from agent_trace_studio.live import (
    CanonicalJournalStore,
    publish_live_event,
    write_server_descriptor,
)
from agent_trace_studio.parser import analyze_journals


class LiveTraceTest(unittest.TestCase):
    def test_canonical_store_replays_multiple_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CanonicalJournalStore(Path(directory))
            path = store.append_event(
                adapter='langgraph',
                session_id='graph-run-1',
                turn_id='turn-1',
                cwd='/workspace/graph',
                event={
                    'timestamp': '2026-08-20T01:00:01Z',
                    'category': 'message',
                    'kind': 'user_message',
                    'role': 'user',
                    'text': 'First turn',
                },
            )
            store.append_event(
                adapter='langgraph',
                session_id='graph-run-1',
                turn_id='turn-2',
                cwd='/workspace/graph',
                event={
                    'timestamp': '2026-08-20T01:00:02Z',
                    'category': 'tool',
                    'kind': 'tool_finished',
                    'tool_name': 'search_docs',
                    'call_id': 'call-1',
                    'status': 'completed',
                },
            )
            store.finish(adapter='langgraph', session_id='graph-run-1')

            result = analyze_journals([path], include_trace=True)

        statuses = {turn.turn_id: turn.status for turn in result.turns}
        self.assertEqual(statuses, {'turn-1': 'completed', 'turn-2': 'completed'})
        self.assertEqual(result.sessions[0].source, 'langgraph')
        by_id = {turn.turn_id: turn for turn in result.turns}
        self.assertEqual(by_id['turn-2'].tool_breakdown, (('search_docs', 1),))
        self.assertEqual(
            {event.turn_id for event in result.traces[0].events if event.kind == 'tool_finished'}, {'turn-2'}
        )

    def test_finish_does_not_create_an_empty_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CanonicalJournalStore(Path(directory))
            path = store.finish(adapter='openai-agents-sdk', session_id='missing')

            self.assertFalse(path.exists())

    def test_descriptor_is_private_and_publish_rejects_non_loopback_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            descriptor = Path(directory) / 'state' / 'live-server.json'
            write_server_descriptor(descriptor, url='https://example.com', token='local-token')

            accepted = publish_live_event(
                adapter='test',
                session_id='session-1',
                event={'category': 'context', 'kind': 'test'},
                descriptor_path=descriptor,
            )

            self.assertFalse(accepted)
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(descriptor.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(descriptor.parent.stat().st_mode), 0o700)


if __name__ == '__main__':
    unittest.main()
