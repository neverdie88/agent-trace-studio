from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from agent_trace_studio.audit_rules import AuditRule, AuditRuleDraft, AuditRuleStore
from agent_trace_studio.live_actions import AuditActionDispatcher, CodexSessionMessageSender


class _RecordingSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.called = threading.Event()

    def send(self, *, session_id: str, message: str) -> None:
        self.calls.append((session_id, message))
        self.called.set()
        if self.fail:
            raise RuntimeError('synthetic transport detail must not escape')


def _rule() -> AuditRule:
    draft = AuditRuleDraft.model_validate(
        {
            'id': 'forbid-unreviewed-command',
            'title': 'Forbid unreviewed commands',
            'expectation': 'The agent must not run an unreviewed command.',
            'severity': 'high',
            'enabled': True,
            'rule_type': 'forbid_event',
            'event': {'tool_name': 'exec_command'},
            'required_event': None,
            'same_turn': False,
        }
    )
    with tempfile.TemporaryDirectory() as directory:
        store = AuditRuleStore(Path(directory) / 'rules.json')
        return store.upsert(draft, expected_version=None)


class AuditActionDispatcherTest(unittest.TestCase):
    def test_codex_sender_uses_read_only_mode_and_scrubs_secret_environment(self) -> None:
        session_id = '00000000-0000-4000-8000-000000000002'
        with (
            mock.patch('agent_trace_studio.live_actions.shutil.which', return_value='/bin/codex'),
            mock.patch(
                'agent_trace_studio.live_actions.subprocess.run',
                return_value=subprocess.CompletedProcess([], 0),
            ) as run,
            mock.patch.dict(os.environ, {'SYNTHETIC_API_KEY': 'TOP_SECRET_KEY'}, clear=False),
        ):
            CodexSessionMessageSender().send(session_id=session_id, message='Bounded notification')

        command = run.call_args.args[0]
        environment = run.call_args.kwargs['env']
        self.assertIn('read-only', command)
        self.assertIn('never', command)
        self.assertEqual(command[-2:], [session_id, '-'])
        self.assertEqual(run.call_args.kwargs['input'], 'Bounded notification')
        self.assertNotIn('SYNTHETIC_API_KEY', environment)

    def test_dispatches_bounded_read_only_notification_and_deduplicates(self) -> None:
        sender = _RecordingSender()
        session_id = '00000000-0000-4000-8000-000000000002'
        contract = {
            'status': 'violated',
            'observation': 'Found one forbidden event.',
            'evidence': [
                {
                    'turn_id': 'turn-1',
                    'line_number': 7,
                    'text': 'TOP_SECRET_TRACE_TEXT',
                }
            ],
        }
        rule = _rule()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audit-actions.jsonl'
            dispatcher = AuditActionDispatcher(path, sender=sender)
            first = dispatcher.dispatch(session_id=session_id, rule=rule, contract=contract)
            self.assertTrue(sender.called.wait(2))
            _wait_for(
                lambda: dispatcher.latest(session_id=session_id, rule_id=rule.id, rule_version=1),
                terminal=True,
            )
            second = dispatcher.dispatch(session_id=session_id, rule=rule, contract=contract)
            next_contract = {
                **contract,
                'evidence': [{'turn_id': 'turn-2', 'line_number': 11, 'text': 'ANOTHER_SECRET'}],
            }
            third = dispatcher.dispatch(session_id=session_id, rule=rule, contract=next_contract)
            _wait_for(
                lambda: dispatcher.latest(
                    session_id=session_id,
                    rule_id=rule.id,
                    rule_version=1,
                    violation_key=dispatcher.violation_key(
                        session_id=session_id,
                        rule=rule,
                        contract=next_contract,
                    ),
                ),
                terminal=True,
            )
            records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

        self.assertEqual(first['id'], second['id'])
        self.assertNotEqual(first['id'], third['id'])
        self.assertEqual(len(sender.calls), 2)
        sent_session_id, message = sender.calls[0]
        self.assertEqual(sent_session_id, session_id)
        self.assertIn('[turn turn-1, line 7]', message)
        self.assertIn('Do not modify files or execute tools', message)
        self.assertNotIn('TOP_SECRET_TRACE_TEXT', message)
        self.assertNotIn('TOP_SECRET_TRACE_TEXT', json.dumps(records))
        self.assertNotIn('ANOTHER_SECRET', json.dumps(records))
        self.assertEqual(records[-1]['status'], 'delivered')

    def test_rejects_non_uuid_and_hides_transport_errors(self) -> None:
        sender = _RecordingSender(fail=True)
        contract = {'status': 'violated', 'observation': 'Synthetic violation.', 'evidence': []}
        rule = _rule()
        with tempfile.TemporaryDirectory() as directory:
            dispatcher = AuditActionDispatcher(Path(directory) / 'actions.jsonl', sender=sender)
            with self.assertRaisesRegex(ValueError, 'exact Codex UUID'):
                dispatcher.dispatch(session_id='session-1', rule=rule, contract=contract)
            receipt = dispatcher.dispatch(
                session_id='00000000-0000-4000-8000-000000000002',
                rule=rule,
                contract=contract,
            )
            self.assertTrue(sender.called.wait(2))
            latest = _wait_for(
                lambda: dispatcher.latest(
                    session_id='00000000-0000-4000-8000-000000000002',
                    rule_id=rule.id,
                    rule_version=1,
                ),
                terminal=True,
            )

        self.assertEqual(receipt['status'], 'queued')
        self.assertEqual(latest['status'], 'failed')
        self.assertEqual(latest['message'], 'Session-agent notification failed.')
        self.assertNotIn('synthetic transport detail', json.dumps(latest))


def _wait_for(
    callback: Callable[[], dict[str, object] | None],
    *,
    terminal: bool = False,
) -> dict[str, object]:
    deadline = time.monotonic() + 2
    value = None
    while time.monotonic() < deadline:
        value = callback()
        if value and (not terminal or value.get('status') in {'delivered', 'failed', 'interrupted'}):
            return value
        time.sleep(0.01)
    raise AssertionError('timed out waiting for audit action')


if __name__ == '__main__':
    unittest.main()
