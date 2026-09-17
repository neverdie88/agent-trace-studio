from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart

from agent_trace_studio.studio_conversations import StudioConversationStore


class StudioConversationStoreTests(unittest.TestCase):
    def test_persists_harness_scoped_native_sessions_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'conversations'
            store = StudioConversationStore(root)
            store.save_native_session_id(
                conversation_id='conversation-1',
                session_id='trace-1',
                harness='codex-sdk',
                native_session_id='thread-1',
            )
            store.save_native_session_id(
                conversation_id='conversation-1',
                session_id='trace-1',
                harness='opencode',
                native_session_id='session-1',
            )

            restored = StudioConversationStore(root)

            self.assertEqual(
                restored.native_session_id(
                    conversation_id='conversation-1',
                    session_id='trace-1',
                    harness='codex-sdk',
                ),
                'thread-1',
            )
            self.assertEqual(
                restored.native_session_id(
                    conversation_id='conversation-1',
                    session_id='trace-1',
                    harness='opencode',
                ),
                'session-1',
            )
            metadata_files = list(root.glob('*/metadata.json'))
            self.assertEqual(len(metadata_files), 2)
            # Windows uses ACLs; retain the mode checks on POSIX platforms.
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in metadata_files))

    def test_round_trips_pydantic_messages_and_clears_all_harness_branches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'conversations'
            store = StudioConversationStore(root)
            messages = [
                ModelRequest.user_text_prompt('What happened?'),
                ModelResponse(parts=[TextPart('The command completed.')], model_name='test-model'),
            ]
            store.save_messages(
                conversation_id='conversation-1',
                session_id='trace-1',
                harness='pydantic',
                messages=messages,
            )
            store.save_native_session_id(
                conversation_id='conversation-1',
                session_id='trace-1',
                harness='opencode',
                native_session_id='session-1',
            )

            restored = StudioConversationStore(root)
            loaded = restored.load_messages(
                conversation_id='conversation-1',
                session_id='trace-1',
                harness='pydantic',
            )

            self.assertEqual(len(loaded), 2)
            self.assertEqual(restored.clear(conversation_id='conversation-1', session_id='trace-1'), 2)
            self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
