from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_trace_studio.assurance_demo import (
    DEMO_SESSION_ID,
    DEMO_TURN_ID,
    build_assurance_demo,
    write_assurance_demo_journal,
)
from agent_trace_studio.parser import analyze_journals


class AssuranceDemoTest(unittest.TestCase):
    def test_builds_contract_results_from_normalized_synthetic_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = write_assurance_demo_journal(Path(directory) / 'release-agent.jsonl')
            result = analyze_journals([journal], include_trace=True)
            assurance = build_assurance_demo(result)

        self.assertEqual(result.sessions[0].session_id, DEMO_SESSION_ID)
        self.assertEqual(result.turns[0].turn_id, DEMO_TURN_ID)
        self.assertEqual(
            [contract['status'] for contract in assurance['contracts']],
            ['satisfied', 'violated', 'pending'],
        )
        violation = assurance['contracts'][1]
        self.assertEqual(violation['id'], 'approval-before-production')
        self.assertEqual(violation['evidence'][-1]['event_title'], 'Tool: deploy_release')
        self.assertEqual(len(assurance['replay_steps']), 5)


if __name__ == '__main__':
    unittest.main()
