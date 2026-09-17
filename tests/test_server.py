from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from uuid import uuid4

from agent_trace_studio.agent_backend import AgentProgress
from agent_trace_studio.audit_rules import AuditRuleDraft, AuditRuleProposalContent, AuditRuleStore
from agent_trace_studio.credentials import APIConfigStore
from agent_trace_studio.live import send_live_hook_event
from agent_trace_studio.live_actions import AuditActionDispatcher
from agent_trace_studio.parser import analyze_journals
from agent_trace_studio.qa import (
    AgentMessageDecision,
    AgentMessageResult,
    AuditRuleAgentResult,
    JournalQA,
    QASettings,
)
from agent_trace_studio.server import (
    DashboardState,
    _agent_run_conversation_answer,
    create_dashboard_server,
    dashboard_server_url,
)
from helpers import write_journal


class _RecordingSessionSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.called = threading.Event()

    def send(self, *, session_id: str, message: str) -> None:
        self.calls.append((session_id, message))
        self.called.set()


class _FakeQA(JournalQA):
    controller_action = 'answer'

    def answer(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.last_answer_kwargs = kwargs
        return {'answer': 'Grounded answer [turn turn-1, line 3]', 'model': self.settings.model, 'usage': {}}

    def route_message(self, *args: object, **kwargs: object) -> AgentMessageResult:
        self.last_route_kwargs = kwargs
        return AgentMessageResult(
            decision=AgentMessageDecision(
                action=self.controller_action,
                answer='Grounded controller answer [turn turn-1, line 3]',
            ),
            model=self.settings.model,
            provider='Test provider',
            harness='Test harness',
            usage={'requests': 1},
            tools=['load_capability'],
        )

    def propose_audit_rule(self, *args: object, **kwargs: object) -> AuditRuleAgentResult:
        self.last_rule_kwargs = kwargs
        return AuditRuleAgentResult(
            content=AuditRuleProposalContent(
                summary='Require validation after a source edit.',
                rule=AuditRuleDraft.model_validate(
                    {
                        'id': 'validate-after-source-edit',
                        'title': 'Validate source changes',
                        'expectation': 'A completed validation must follow every source edit.',
                        'severity': 'high',
                        'enabled': True,
                        'rule_type': 'require_after',
                        'event': {'tool_name': 'apply_patch'},
                        'required_event': {'tool_name': 'exec_command', 'status': 'completed'},
                        'same_turn': True,
                    }
                ),
            ),
            model=self.settings.model,
            provider='Test provider',
            harness='Test harness',
            usage={'requests': 1},
            tools=[],
        )

    def clear_conversation(self, *, conversation_id: str, session_id: str) -> int:
        self.last_clear_kwargs = {'conversation_id': conversation_id, 'session_id': session_id}
        return 2


class _UnavailableSecretStore:
    available = False
    label = 'No native credential store'

    def get(self) -> str | None:
        return None

    def set(self, value: str) -> None:
        raise AssertionError(f'native store should not receive {len(value)} characters')

    def delete(self) -> None:
        pass


class _FakeRepair:
    def __init__(self) -> None:
        self.run = {
            'run_id': 'repairrun1',
            'kind': 'repair',
            'status': 'queued',
            'message': 'queued',
            'attempt': 0,
            'max_attempts': 5,
            'activity': [],
        }
        self.dashboard_state: dict[str, object] | None = None
        self.audit_run_id: str | None = None
        self.instruction: str | None = None
        self.action: dict[str, object] | None = None
        self.prepared_actions: list[dict[str, object]] = []
        self.approved_actions: list[dict[str, object]] = []
        self.deployment: dict[str, object] | None = None
        self.harness = 'opencode'
        self.brief: dict[str, object] = {
            'available': False,
            'session_id': 'session-1',
            'status': 'missing',
            'result': None,
        }

    def status(self) -> dict[str, object]:
        return {
            'available': True,
            'backend': 'Fake agents',
            'harness_id': self.harness,
            'harness_label': 'OpenCode' if self.harness == 'opencode' else 'Pydantic AI Harness',
            'harness_available': True,
            'harness_detail': 'Ready for test.',
            'harnesses': [
                {'id': 'opencode', 'label': 'OpenCode', 'available': True, 'detail': 'Ready for test.'},
                {'id': 'pydantic', 'label': 'Pydantic AI Harness', 'available': True, 'detail': 'Ready for test.'},
            ],
            'source_workspace': '/tmp/source',
            'workflows': {
                'investigate': True,
                'memories': True,
                'checkpoints': True,
                'audit': True,
                'repair': True,
                'customize': True,
            },
            'max_attempts': 5,
            'active_run_id': None,
            'latest_run': None,
        }

    def configure_harness(self, harness: str) -> dict[str, object]:
        if harness not in {'opencode', 'pydantic'}:
            raise ValueError('unknown test harness')
        self.harness = harness
        return self.status()

    def session_brief(self, result: object, session_id: str) -> dict[str, object]:
        _ = result
        return {**self.brief, 'session_id': session_id}

    def start(self, kind: str, **kwargs: object) -> dict[str, object]:
        self.run['kind'] = kind
        self.dashboard_state = kwargs['dashboard_state']
        if isinstance(kwargs.get('audit_run_id'), str):
            self.audit_run_id = kwargs['audit_run_id']
        if isinstance(kwargs.get('instruction'), str):
            self.instruction = kwargs['instruction']
        return dict(self.run)

    def reusable_audit_run_id(self, **kwargs: object) -> str | None:
        return self.audit_run_id

    def prepare_source_action(
        self,
        action: str,
        *,
        dashboard_state: dict[str, object],
        instruction: str,
        client_session_nonce: str,
        audit_run_id: str | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        prepared = {
            'id': f'auth{len(self.prepared_actions) + 1}',
            'token': f'token{len(self.prepared_actions) + 1}',
            'action': action,
            'message': instruction,
            'request_hash': '1' * 64,
            'selection_hash': '2' * 64,
            'source_workspace': '/tmp/source',
            'baseline_digest': '3' * 64,
            'activation_mode': 'activate_verified_runtime',
            'activation_effect': 'Apply verified changes and activate the updated local dashboard.',
            'audit_run_id': audit_run_id,
            'run_id': None,
            'expires_at': '2026-08-26T00:00:00+00:00',
            'expires_in_seconds': 120,
            'client_session_nonce': client_session_nonce,
            'dashboard_state': dashboard_state,
        }
        self.prepared_actions.append(prepared)
        return {key: value for key, value in prepared.items() if key not in {'client_session_nonce', 'dashboard_state'}}

    def prepare_run_action(
        self,
        run_id: str,
        *,
        action: str,
        instruction: str | None,
        client_session_nonce: str,
        **kwargs: object,
    ) -> dict[str, object]:
        self.get(run_id)
        action_name = {
            'continue': 'continue_run',
            'restart': 'restart_run',
            'activate': 'activate_run',
        }[action]
        prepared = {
            'id': f'auth{len(self.prepared_actions) + 1}',
            'token': f'token{len(self.prepared_actions) + 1}',
            'action': action_name,
            'message': instruction or action,
            'request_hash': '4' * 64,
            'selection_hash': '5' * 64,
            'source_workspace': '/tmp/source',
            'baseline_digest': '6' * 64,
            'activation_mode': 'activate_verified_runtime',
            'activation_effect': 'Apply verified changes and activate the updated local dashboard.',
            'audit_run_id': None,
            'run_id': run_id,
            'change_digest': (
                self.run.get('result', {}).get('change_digest')
                if action == 'activate' and isinstance(self.run.get('result'), dict)
                else None
            ),
            'expires_at': '2026-08-26T00:00:00+00:00',
            'expires_in_seconds': 120,
            'client_session_nonce': client_session_nonce,
        }
        self.prepared_actions.append(prepared)
        return {key: value for key, value in prepared.items() if key != 'client_session_nonce'}

    def approve_source_action(
        self,
        *,
        authorization_id: str,
        token: str,
        client_session_nonce: str,
        **kwargs: object,
    ) -> dict[str, object]:
        prepared = next((item for item in self.prepared_actions if item['id'] == authorization_id), None)
        if prepared is None:
            raise ValueError('source action authorization was not found')
        if prepared.get('used'):
            raise ValueError('source action authorization was already used or cancelled')
        if token != prepared['token']:
            raise ValueError('source action authorization token is invalid')
        if client_session_nonce != prepared['client_session_nonce']:
            raise ValueError('source action authorization belongs to a different client session')
        prepared['used'] = True
        self.approved_actions.append(prepared)
        action = str(prepared['action'])
        if action == 'continue_run':
            return self.act(
                str(prepared['run_id']),
                action='continue',
                settings=kwargs['settings'],
                instruction=prepared['message'],
            )
        if action == 'restart_run':
            return self.act(
                str(prepared['run_id']),
                action='restart',
                settings=kwargs['settings'],
                instruction=prepared['message'],
            )
        if action == 'activate_run':
            self.run['source_authorization'] = {
                'activation_mode': 'activate_verified_runtime',
                'change_digest': prepared.get('change_digest'),
            }
            return dict(self.run)
        kind = 'repair' if action == 'repair_parser' else 'customize'
        return self.start(
            kind,
            dashboard_state=prepared['dashboard_state'],
            audit_run_id=prepared['audit_run_id'],
            instruction=prepared['message'],
        )

    def cancel_source_action(
        self,
        *,
        authorization_id: str,
        token: str,
        client_session_nonce: str,
    ) -> None:
        prepared = next((item for item in self.prepared_actions if item['id'] == authorization_id), None)
        if prepared is None:
            raise ValueError('source action authorization was not found')
        if token != prepared['token'] or client_session_nonce != prepared['client_session_nonce']:
            raise ValueError('source action authorization token is invalid')
        prepared['used'] = True

    def get(self, run_id: str) -> dict[str, object]:
        if run_id != self.run['run_id']:
            raise ValueError('agent run not found')
        return dict(self.run)

    def cancel(self, run_id: str) -> dict[str, object]:
        self.get(run_id)
        self.run['message'] = 'Cancellation requested'
        return dict(self.run)

    def act(self, run_id: str, *, action: str, **kwargs: object) -> dict[str, object]:
        self.get(run_id)
        settings = kwargs['settings']
        self.action = {
            'action': action,
            'instruction': kwargs.get('instruction'),
            'model': settings.model,
        }
        self.run['message'] = f'{action} requested'
        return dict(self.run)

    def record_deployment_result(
        self,
        run_id: str,
        deployment: dict[str, object],
    ) -> dict[str, object]:
        self.get(run_id)
        self.deployment = deployment
        response = dict(self.run)
        result = dict(response.get('result') or {})
        result['deployment'] = deployment
        result['restart_required'] = False
        response['result'] = result
        response['deployment'] = deployment
        return response

    def record_bundle_refresh(
        self,
        run_id: str,
        refresh: dict[str, object],
    ) -> dict[str, object]:
        self.get(run_id)
        response = dict(self.run)
        result = dict(response.get('result') or {})
        result['restart_required'] = False
        result['dashboard_refreshed'] = True
        result['dashboard_refresh'] = refresh
        response['result'] = result
        return response

    def close(self) -> None:
        pass


def _wait_for(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('condition was not satisfied before timeout')


class DashboardServerTest(unittest.TestCase):
    def test_agent_can_read_bounded_audit_resources_without_journal_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'private-session.jsonl'
            write_journal(journal, session_id='session-resource')
            result = analyze_journals([journal], include_trace=True)
            rules = AuditRuleStore(root / 'state' / 'audit-rules.json')
            rules.upsert(
                AuditRuleDraft.model_validate(
                    {
                        'id': 'forbid-shell-command',
                        'title': 'Forbid shell commands',
                        'expectation': 'The agent must not invoke exec_command.',
                        'severity': 'high',
                        'enabled': True,
                        'rule_type': 'forbid_event',
                        'event': {'tool_name': 'exec_command'},
                    }
                ),
                expected_version=None,
            )
            state = DashboardState(
                result,
                title='Resource Test',
                qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
                audit_rules=rules,
            )
            try:
                resources = state._dashboard_resource_access(
                    result,
                    session_id='session-resource',
                    turn_id='turn-1',
                    event_sequence=3,
                    view_state={},
                )
                rule = resources.read('audit_rules', 'forbid-shell-command', 5)
                finding = resources.read('audit_findings', 'forbid-shell-command', 5)
            finally:
                state.close()

        self.assertEqual(rule['items'][0]['event']['tool_name'], 'exec_command')
        self.assertEqual(finding['items'][0]['status'], 'violated')
        serialized = json.dumps([rule, finding], sort_keys=True)
        self.assertNotIn(str(journal), serialized)
        self.assertNotIn('session_file', serialized)

    def test_studio_context_activity_updates_one_row_and_preserves_distinct_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'synthetic.jsonl'
            write_journal(journal)
            state = DashboardState(
                analyze_journals([journal], include_trace=True),
                title='Synthetic activity test',
                qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
            )
            request_id = str(uuid4())
            cancel = state.begin_studio_request(request_id)
            details = {
                'kind': 'context_action',
                'id': 'synthetic-call-1',
                'tool': 'read_dashboard_resource',
                'status': 'running',
                'arguments': {'resource': 'audit_rules'},
            }
            try:
                with mock.patch('agent_trace_studio.server._studio_activity_timestamp', return_value='same-time'):
                    state.record_studio_activity(request_id, AgentProgress('Tool', 'Reading rules.', details))
                    first = state.studio_request(request_id)
                    details.update(
                        status='completed',
                        output='{"title":"Require tests","api_key":"PRIVATE_API_KEY"}',
                        raw_journal='PRIVATE_RAW_ROW',
                    )
                    state.record_studio_activity(request_id, AgentProgress('Tool', 'Read rules.', details))
                    second = state.studio_request(request_id)
                    self.assertEqual(len(second['activity']), 1)
                    self.assertGreater(second['activity_revision'], first['activity_revision'])
                    self.assertEqual(second['activity'][0]['details']['status'], 'completed')
                    self.assertEqual(first['activity'][0]['details']['status'], 'running')
                    self.assertNotIn('PRIVATE_API_KEY', json.dumps(second))
                    self.assertNotIn('PRIVATE_RAW_ROW', json.dumps(second))
                    details.update(id='synthetic-call-2', status='running', output='')
                    state.record_studio_activity(request_id, AgentProgress('Tool', 'Reading rules.', details))
                    state.finish_studio_request(request_id, cancel, status='cancelled')
                    finished = state.studio_request(request_id)
                    calls = [item for item in finished['activity'] if item.get('details')]
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[0]['details']['status'], 'completed')
                    self.assertEqual(calls[1]['details']['status'], 'cancelled')
            finally:
                state.close()

    def test_studio_request_cancel_endpoint_signals_the_active_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-cancel-studio')
            state = DashboardState(
                analyze_journals([journal], include_trace=True),
                title='Cancel Studio Test',
                qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
            )
            request_id = str(uuid4())
            cancel_event = state.begin_studio_request(request_id)
            server = create_dashboard_server(output_dir=root, state=state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(
                    f'{dashboard_server_url(server).rstrip("/")}/api/agent/messages/{request_id}',
                    timeout=5,
                ) as response:
                    running = json.loads(response.read())
                self.assertEqual(running['status'], 'running')
                self.assertEqual(running['activity'], [])
                cancel = urllib.request.Request(
                    f'{dashboard_server_url(server).rstrip("/")}/api/agent/messages/{request_id}',
                    method='DELETE',
                )
                with urllib.request.urlopen(cancel, timeout=5) as response:
                    payload = json.loads(response.read())
                self.assertTrue(payload['cancelled'])
                self.assertTrue(cancel_event.wait(1))
                with urllib.request.urlopen(
                    f'{dashboard_server_url(server).rstrip("/")}/api/agent/messages/{request_id}',
                    timeout=5,
                ) as response:
                    cancelling = json.loads(response.read())
                self.assertEqual(cancelling['status'], 'cancelling')
                self.assertEqual(cancelling['activity'][-1]['phase'], 'Stop')
            finally:
                state.finish_studio_request(request_id, cancel_event)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                state.close()

    def test_terminal_agent_run_has_a_bounded_conversation_answer(self) -> None:
        audit_answer = _agent_run_conversation_answer(
            {
                'run_id': 'audit1',
                'kind': 'audit',
                'status': 'audited',
                'message': 'Audit completed.',
                'audit': {
                    'summary': 'One parser gap is supported by the observed journal shape.',
                    'requires_fix': True,
                    'issues': [
                        {
                            'severity': 'medium',
                            'title': 'Nested durations are ignored',
                            'evidence': 'The input uses duration.secs and duration.nanos.',
                        }
                    ],
                },
            }
        )
        customize_answer = _agent_run_conversation_answer(
            {
                'run_id': 'customize1',
                'kind': 'customize',
                'status': 'passed',
                'message': 'The verified dashboard source was activated.',
                'result': {
                    'changed_files': ['src/agent_trace_studio/assets/dashboard.css'],
                    'verifier': {
                        'summary': 'The requested palette is applied without layout regressions.',
                        'fixed_items': ['Updated the dashboard color tokens.'],
                    },
                },
                'dashboard_refresh': {'status': 'ready', 'message': 'The updated dashboard bundle is ready.'},
            }
        )
        paused_answer = _agent_run_conversation_answer(
            {
                'run_id': 'repair1',
                'kind': 'repair',
                'status': 'paused',
                'message': 'The model request timed out.',
                'recovery': {'reason': 'Provider timeout.', 'actions': ['continue', 'restart', 'discard']},
            }
        )

        self.assertIn('### Parser audit completed', audit_answer or '')
        self.assertIn('Nested durations are ignored', audit_answer or '')
        self.assertIn('### Dashboard customization completed', customize_answer or '')
        self.assertIn('Updated the dashboard color tokens.', customize_answer or '')
        self.assertIn('`src/agent_trace_studio/assets/dashboard.css`', customize_answer or '')
        self.assertIn('### Parser repair paused', paused_answer or '')
        self.assertIn('**Available actions:** continue, restart, discard', paused_answer or '')
        self.assertIsNone(
            _agent_run_conversation_answer(
                {'run_id': 'running1', 'kind': 'audit', 'status': 'auditing', 'message': 'Still working.'}
            )
        )

    def test_verified_python_change_is_activated_by_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-deploy')
            repair = _FakeRepair()
            repair.run.update(
                {
                    'kind': 'repair',
                    'status': 'passed',
                    'source_authorization': {'activation_mode': 'activate_verified_runtime'},
                    'result': {
                        'changed_files': ['src/agent_trace_studio/parser.py'],
                        'change_digest': 'abc123',
                        'backup_path': str(root / 'backup'),
                        'source_delta': {
                            'added': [],
                            'modified': ['src/agent_trace_studio/parser.py'],
                            'deleted': [],
                        },
                        'applied_records': {
                            'src/agent_trace_studio/parser.py': {
                                'digest': 'abc123',
                                'size': 10,
                                'mode': 420,
                                'kind': 'file',
                            }
                        },
                        'restart_required': True,
                    },
                }
            )
            supervisor = mock.Mock()
            supervisor.activate.return_value = {
                'status': 'promoted',
                'generation': 'generation-2',
                'message': 'Candidate promoted.',
            }
            with mock.patch(
                'agent_trace_studio.server.SupervisorClient.from_environment',
                return_value=supervisor,
            ):
                state = DashboardState(
                    analyze_journals([journal], include_trace=True),
                    title='Deploy Test',
                    qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
                    repair=repair,
                    output_dir=root / 'dashboard',
                )
            try:
                run = state.agent_run('repairrun1')
            finally:
                state.close()

        self.assertEqual(run['deployment']['status'], 'promoted')
        self.assertFalse(run['result']['restart_required'])
        self.assertEqual(repair.deployment['generation'], 'generation-2')
        activation = supervisor.activate.call_args.args[0]
        self.assertEqual(activation['run_id'], 'repairrun1')
        self.assertEqual(
            activation['rollback_manifest']['source_delta']['modified'], ['src/agent_trace_studio/parser.py']
        )

    def test_verified_python_change_without_activation_scope_is_not_deployed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-deploy-no-scope')
            repair = _FakeRepair()
            repair.run.update(
                {
                    'kind': 'repair',
                    'status': 'passed',
                    'result': {
                        'changed_files': ['src/agent_trace_studio/parser.py'],
                        'change_digest': 'abc123',
                        'restart_required': True,
                    },
                }
            )
            supervisor = mock.Mock()
            supervisor.activate.return_value = {
                'status': 'promoted',
                'generation': 'generation-approved',
                'message': 'Candidate promoted after explicit activation approval.',
            }
            with mock.patch(
                'agent_trace_studio.server.SupervisorClient.from_environment',
                return_value=supervisor,
            ):
                state = DashboardState(
                    analyze_journals([journal], include_trace=True),
                    title='Deploy Scope Test',
                    qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
                    repair=repair,
                    output_dir=root / 'dashboard',
                )
            try:
                run = state.agent_run('repairrun1')
                prepared = state.act_on_agent_run(
                    'repairrun1',
                    action='activate',
                    instruction=None,
                    client_session_nonce='client-session-nonce-000001',
                )
                approved = state.approve_source_action(
                    authorization_id=str(prepared['authorization']['id']),
                    token=str(prepared['authorization']['token']),
                    client_session_nonce='client-session-nonce-000001',
                )
            finally:
                state.close()

        self.assertEqual(run['dashboard_refresh']['status'], 'approval_required')
        self.assertEqual(run['recovery']['actions'], ['activate'])
        self.assertTrue(run['result']['restart_required'])
        self.assertEqual(prepared['authorization']['action'], 'activate_run')
        self.assertEqual(approved['deployment']['status'], 'promoted')
        supervisor.activate.assert_called_once()

    def test_transient_supervisor_failure_is_retried_instead_of_cached_as_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-deploy-retry')
            repair = _FakeRepair()
            repair.run.update(
                {
                    'kind': 'repair',
                    'status': 'passed',
                    'source_authorization': {'activation_mode': 'activate_verified_runtime'},
                    'result': {
                        'changed_files': ['src/agent_trace_studio/parser.py'],
                        'change_digest': 'a' * 64,
                        'backup_path': str(root / 'backup'),
                        'source_delta': {
                            'added': [],
                            'modified': ['src/agent_trace_studio/parser.py'],
                            'deleted': [],
                        },
                        'applied_records': {},
                        'restart_required': True,
                    },
                }
            )
            supervisor = mock.Mock()
            supervisor.activate.side_effect = [
                urllib.error.URLError('temporary supervisor disconnect'),
                {'status': 'promoted', 'generation': 'generation-retried', 'message': 'Candidate promoted.'},
            ]
            with mock.patch(
                'agent_trace_studio.server.SupervisorClient.from_environment',
                return_value=supervisor,
            ):
                state = DashboardState(
                    analyze_journals([journal], include_trace=True),
                    title='Deploy Retry Test',
                    qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
                    repair=repair,
                    output_dir=root / 'dashboard',
                )
            try:
                retrying = state.agent_run('repairrun1')
                for deployment in state._runtime_deployments.values():
                    deployment['_retry_at'] = 0
                promoted = state.agent_run('repairrun1')
            finally:
                state.close()

        self.assertEqual(retrying['status'], 'applying')
        self.assertEqual(retrying['deployment']['status'], 'retrying')
        self.assertEqual(promoted['deployment']['status'], 'promoted')
        self.assertEqual(supervisor.activate.call_count, 2)

    def test_verified_dashboard_asset_change_rebuilds_the_served_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            output_dir = root / 'dashboard'
            output_dir.mkdir()
            (output_dir / 'index.html').write_text('stale dashboard', encoding='utf-8')
            write_journal(journal, session_id='session-refresh')
            repair = _FakeRepair()
            repair.run.update(
                {
                    'kind': 'customize',
                    'status': 'passed',
                    'source_authorization': {'activation_mode': 'activate_verified_runtime'},
                    'result': {
                        'changed_files': [
                            'src/agent_trace_studio/assets/dashboard.css',
                            'tests/test_report.py',
                        ],
                        'restart_required': True,
                    },
                }
            )
            state = DashboardState(
                analyze_journals([journal], include_trace=True),
                title='Refresh Test',
                qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
                repair=repair,
                output_dir=output_dir,
            )
            try:
                run = state.agent_run('repairrun1')
                html = (output_dir / 'index.html').read_text(encoding='utf-8')
            finally:
                state.close()

        self.assertEqual(run['dashboard_refresh']['status'], 'ready')
        self.assertTrue(run['dashboard_refresh']['auto_reload'])
        self.assertFalse(run['result']['restart_required'])
        self.assertTrue(run['result']['dashboard_refreshed'])
        self.assertIn('### Dashboard customization completed', run['conversation_answer'])
        self.assertIn('`src/agent_trace_studio/assets/dashboard.css`', run['conversation_answer'])
        self.assertIn('<title>Agent Trace Studio</title>', html)
        self.assertIn('Refresh Test', html)
        self.assertNotIn('stale dashboard', html)

    def test_lifecycle_hook_registers_and_completes_a_native_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'hook-session.jsonl'
            write_journal(journal, session_id='hook-session')
            descriptor = root / 'server.json'
            state = DashboardState(
                analyze_journals([], include_trace=True),
                title='Hook Dashboard',
                qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
                live=True,
                live_poll_interval=0.05,
                live_state_dir=root / 'live-state',
                live_server_path=descriptor,
            )
            server = create_dashboard_server(output_dir=root, state=state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertTrue(
                    send_live_hook_event(
                        {
                            'hook_event_name': 'SessionStart',
                            'adapter': 'codex',
                            'session_id': 'hook-session',
                            'transcript_path': str(journal),
                            'cwd': str(root),
                        },
                        descriptor_path=descriptor,
                    )
                )
                _wait_for(lambda: state.status()['sessions'] == 1)
                self.assertEqual(state.payload()['traces'][0]['session_id'], 'hook-session')
                self.assertTrue(
                    send_live_hook_event(
                        {
                            'hook_event_name': 'SessionEnd',
                            'adapter': 'codex',
                            'session_id': 'hook-session',
                            'transcript_path': str(journal),
                        },
                        descriptor_path=descriptor,
                    )
                )
                source = next(
                    item for item in state.status()['live']['sources'] if item['path'] == str(journal.resolve())
                )
                self.assertEqual(source['status'], 'completed')
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                state.close()

    def test_live_monitor_refreshes_an_appended_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='live-session')
            state = DashboardState(
                analyze_journals([journal], include_trace=True),
                title='Live Dashboard',
                qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
                live=True,
                live_poll_interval=0.05,
                live_state_dir=root / 'live-state',
                live_server_path=root / 'server.json',
            )
            try:
                before = state.status()['live']['revision']
                with journal.open('a', encoding='utf-8') as stream:
                    stream.write(
                        json.dumps(
                            {
                                'timestamp': '2026-08-10T01:00:15Z',
                                'type': 'event_msg',
                                'payload': {'type': 'task_started', 'turn_id': 'turn-3'},
                            }
                        )
                        + '\n'
                    )
                    stream.write(
                        json.dumps(
                            {
                                'timestamp': '2026-08-10T01:00:16Z',
                                'type': 'event_msg',
                                'payload': {'type': 'task_complete', 'turn_id': 'turn-3'},
                            }
                        )
                        + '\n'
                    )

                _wait_for(lambda: len(state.payload()['turns']) == 3)

                live = state.status()['live']
                self.assertGreater(live['revision'], before)
                self.assertEqual(live['state'], 'starting')
                self.assertEqual(live['monitored_files'], 1)
                update = state.wait_for_live_update(before, timeout=0.1)
                self.assertIsNotNone(update)
                self.assertGreater(update['revision'], before)
            finally:
                state.close()

    def test_dashboard_can_start_live_audit_and_explicitly_message_a_violating_codex_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_id = '00000000-0000-4000-8000-000000000002'
            journal = root / f'rollout-{session_id}.jsonl'
            write_journal(journal, session_id=session_id)
            rules = AuditRuleStore(root / 'state' / 'audit-rules.json')
            rules.upsert(
                AuditRuleDraft.model_validate(
                    {
                        'id': 'forbid-shell-command',
                        'title': 'Forbid shell commands',
                        'expectation': 'The agent must not invoke exec_command.',
                        'severity': 'high',
                        'enabled': True,
                        'rule_type': 'forbid_event',
                        'event': {'tool_name': 'exec_command'},
                        'required_event': None,
                        'same_turn': False,
                    }
                ),
                expected_version=None,
            )
            sender = _RecordingSessionSender()
            actions = AuditActionDispatcher(root / 'state' / 'audit-actions.jsonl', sender=sender)
            state = DashboardState(
                analyze_journals([journal], include_trace=True),
                title='Live Action Dashboard',
                qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
                live=False,
                live_poll_interval=0.05,
                live_state_dir=root / 'live-state',
                live_server_path=root / 'server.json',
                audit_rules=rules,
                audit_actions=actions,
            )
            server = create_dashboard_server(output_dir=root, state=state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = dashboard_server_url(server).rstrip('/')
                start = urllib.request.Request(f'{base_url}/api/live/start', data=b'', method='POST')
                with urllib.request.urlopen(start, timeout=5) as response:
                    started = json.loads(response.read())
                self.assertTrue(started['live']['enabled'])
                self.assertTrue(started['live']['audit']['enabled'])

                with urllib.request.urlopen(
                    f'{base_url}/api/audit-rules?session_id={session_id}', timeout=5
                ) as response:
                    catalog = json.loads(response.read())
                contract = catalog['assurance']['contracts'][0]
                self.assertEqual(contract['status'], 'violated')
                self.assertTrue(contract['action']['available'])
                self.assertTrue(contract['action']['requires_user_action'])

                request = urllib.request.Request(
                    f'{base_url}/api/live/audit-actions/message',
                    data=json.dumps(
                        {
                            'session_id': session_id,
                            'rule_id': contract['id'],
                            'rule_version': 1,
                            'message': 'TOP_SECRET_CLIENT_INSTRUCTION',
                        }
                    ).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    queued = json.loads(response.read())
                self.assertEqual(response.status, 202)
                self.assertTrue(sender.called.wait(2))
                _wait_for(
                    lambda: (
                        actions.latest(
                            session_id=session_id,
                            rule_id='forbid-shell-command',
                            rule_version=1,
                        )
                        or {}
                    ).get('status')
                    == 'delivered'
                )

                with urllib.request.urlopen(request, timeout=5) as response:
                    duplicate = json.loads(response.read())
                self.assertEqual(queued['action']['id'], duplicate['action']['id'])
                self.assertEqual(len(sender.calls), 1)
                self.assertNotIn('TOP_SECRET_TOOL', sender.calls[0][1])
                self.assertNotIn('TOP_SECRET_CLIENT_INSTRUCTION', sender.calls[0][1])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                state.close()

    def test_live_audit_automatic_action_ignores_history_and_sends_once_for_new_violation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_id = '00000000-0000-4000-8000-000000000002'
            journal = root / f'rollout-{session_id}.jsonl'
            write_journal(journal, session_id=session_id)
            rules = AuditRuleStore(root / 'state' / 'audit-rules.json')
            rules.upsert(
                AuditRuleDraft.model_validate(
                    {
                        'id': 'automatic-command-response',
                        'title': 'Respond to unsafe commands',
                        'expectation': 'A new shell command requires an automatic session response.',
                        'severity': 'high',
                        'enabled': True,
                        'rule_type': 'forbid_event',
                        'event': {'tool_name': 'exec_command'},
                        'required_event': None,
                        'same_turn': False,
                        'automatic_action': {
                            'type': 'send_session_message',
                            'message': 'Approval is withheld. Stop and ask the user.',
                        },
                    }
                ),
                expected_version=None,
            )
            sender = _RecordingSessionSender()
            actions = AuditActionDispatcher(root / 'state' / 'audit-actions.jsonl', sender=sender)
            state = DashboardState(
                analyze_journals([journal], include_trace=True),
                title='Automatic Live Audit',
                qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
                live=False,
                live_poll_interval=0.03,
                live_state_dir=root / 'live-state',
                live_server_path=root / 'server.json',
                audit_rules=rules,
                audit_actions=actions,
            )
            try:
                live = state.start_live_monitoring()
                self.assertEqual(live['audit']['automatic_actions'], 1)
                self.assertFalse(sender.called.wait(0.1), 'arming must not replay historical violations')
                with journal.open('a', encoding='utf-8') as stream:
                    stream.write(
                        json.dumps(
                            {
                                'timestamp': '2026-08-10T01:00:20Z',
                                'type': 'response_item',
                                'payload': {
                                    'type': 'function_call',
                                    'name': 'exec_command',
                                    'call_id': 'automatic-call',
                                    'arguments': '{"secret":"TOP_SECRET_NEW_EVENT"}',
                                },
                            }
                        )
                        + '\n'
                    )
                self.assertTrue(sender.called.wait(2))
                _wait_for(
                    lambda: (
                        actions.latest(
                            session_id=session_id,
                            rule_id='automatic-command-response',
                            rule_version=1,
                        )
                        or {}
                    ).get('status')
                    == 'delivered'
                )
                with journal.open('a', encoding='utf-8') as stream:
                    stream.write(
                        json.dumps(
                            {
                                'timestamp': '2026-08-10T01:00:21Z',
                                'type': 'event_msg',
                                'payload': {'type': 'token_count', 'info': {}},
                            }
                        )
                        + '\n'
                    )
                _wait_for(lambda: state.status()['live']['revision'] >= 3)
                time.sleep(0.05)

                self.assertEqual(len(sender.calls), 1)
                sent_message = sender.calls[0][1]
                self.assertIn('Approval is withheld. Stop and ask the user.', sent_message)
                self.assertIn('read-only turn', sent_message)
                self.assertNotIn('TOP_SECRET_NEW_EVENT', sent_message)
                catalog = state.audit_rule_catalog(session_id)
                action = catalog['assurance']['contracts'][0]['action']
                self.assertTrue(action['automatic'])
                self.assertEqual(action['receipt']['trigger'], 'automatic')
            finally:
                state.close()

    def test_accepts_authenticated_canonical_live_events_over_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = DashboardState(
                analyze_journals([], include_trace=True),
                title='Streaming Dashboard',
                qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
                live=True,
                live_poll_interval=0.05,
                live_state_dir=root / 'live-state',
                live_server_path=root / 'server.json',
            )
            server = create_dashboard_server(output_dir=root, state=state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = dashboard_server_url(server).rstrip('/')
                descriptor = json.loads((root / 'server.json').read_text(encoding='utf-8'))
                body = json.dumps(
                    {
                        'adapter': 'langgraph',
                        'session_id': 'graph-run-1',
                        'turn_id': 'thread-1',
                        'cwd': '/workspace/graph',
                        'event': {
                            'timestamp': '2026-08-20T01:00:01Z',
                            'category': 'tool',
                            'kind': 'task_finished',
                            'tool_name': 'search_docs',
                            'call_id': 'call-1',
                            'status': 'completed',
                        },
                    }
                ).encode()
                unauthorized = urllib.request.Request(
                    f'{base_url}/api/live/events',
                    data=body,
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(unauthorized, timeout=5)
                self.assertEqual(rejected.exception.code, 401)

                request = urllib.request.Request(
                    f'{base_url}/api/live/events',
                    data=body,
                    headers={
                        'Authorization': f'Bearer {descriptor["token"]}',
                        'Content-Type': 'application/json',
                    },
                    method='POST',
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    accepted = json.loads(response.read())
                self.assertEqual(response.status, 202)
                self.assertTrue(accepted['accepted'])

                _wait_for(lambda: state.status()['sessions'] == 1)
                payload = state.payload()
                self.assertEqual(payload['sessions'][0]['source'], 'langgraph')
                self.assertEqual(payload['traces'][0]['events'][-1]['tool_name'], 'search_docs')
                self.assertTrue(state.status()['live']['enabled'])
                self.assertEqual(state.status()['live']['state'], 'watching')
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                state.close()

    def test_live_stream_emits_a_revision_after_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='sse-session')
            state = DashboardState(
                analyze_journals([journal], include_trace=True),
                title='SSE Dashboard',
                qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
                live=True,
                live_poll_interval=0.05,
                live_state_dir=root / 'live-state',
                live_server_path=root / 'server.json',
            )
            server = create_dashboard_server(output_dir=root, state=state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stream = None
            try:
                revision = state.status()['live']['revision']
                stream = urllib.request.urlopen(
                    f'{dashboard_server_url(server)}api/live/stream?after={revision}',
                    timeout=5,
                )
                self.assertEqual(stream.readline().decode().strip(), 'retry: 1500')
                self.assertEqual(stream.readline().decode().strip(), '')
                with journal.open('a', encoding='utf-8') as output:
                    output.write(
                        json.dumps(
                            {
                                'timestamp': '2026-08-10T01:00:15Z',
                                'type': 'event_msg',
                                'payload': {'type': 'task_started', 'turn_id': 'turn-sse'},
                            }
                        )
                        + '\n'
                    )
                lines = [stream.readline().decode().strip() for _ in range(3)]
                self.assertTrue(lines[0].startswith('id: '))
                self.assertEqual(lines[1], 'event: trace-update')
                self.assertTrue(lines[2].startswith('data: {'))
                self.assertIn('"state":"watching"', lines[2])
            finally:
                if stream is not None:
                    stream.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                state.close()

    def test_loads_path_upload_and_answers_qa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / 'first.jsonl'
            second = root / 'second.jsonl'
            upload = root / 'upload.jsonl'
            duplicate = root / 'duplicate.jsonl'
            write_journal(first, session_id='session-1')
            write_journal(second, session_id='session-2')
            write_journal(upload, session_id='session-upload')
            write_journal(duplicate, session_id='session-2')
            result = analyze_journals([first], include_trace=True)
            qa = _FakeQA(QASettings(api_key='test-key', model='test-model'))
            state = DashboardState(
                result,
                title='Test Dashboard',
                qa=qa,
            )
            try:
                status = state.status()
                self.assertTrue(status['qa']['configured'])

                path_payload = state.load_path(str(second))
                self.assertEqual(
                    {trace['session_id'] for trace in path_payload['traces']},
                    {'session-1', 'session-2'},
                )
                self.assertEqual(path_payload['meta']['title'], 'Agent Trace Studio')
                self.assertEqual(path_payload['meta']['trace_set_title'], 'Test Dashboard')

                upload_body = upload.read_bytes()
                upload_payload = state.load_upload('uploaded.jsonl', upload_body)
                self.assertEqual(
                    {trace['session_id'] for trace in upload_payload['traces']},
                    {'session-1', 'session-2', 'session-upload'},
                )
                self.assertEqual(len(state.status()['source_paths']), 3)

                with self.assertRaisesRegex(ValueError, 'session session-2 is already loaded'):
                    state.load_path(str(duplicate))

                qa_payload = state.ask(
                    question='What happened?',
                    scope='turn',
                    session_id='session-upload',
                    turn_id='turn-1',
                    history=[],
                    event_sequence=3,
                    view_state={'category': 'tool', 'tool': 'exec_command'},
                )
                self.assertIn('[turn turn-1, line 3]', qa_payload['answer'])
                self.assertEqual(qa.last_answer_kwargs['event_sequence'], 3)
                self.assertEqual(qa.last_answer_kwargs['view_state'], {'category': 'tool', 'tool': 'exec_command'})

                qa_status = state.configure_qa(
                    api_key='replacement-secret',
                    provider='openai',
                    model='second-model',
                    base_url='https://example.test/v1',
                    remember=False,
                )
                self.assertTrue(qa_status['configured'])
                self.assertNotIn('replacement-secret', json.dumps(qa_status))
                self.assertFalse(state.clear_qa_api_key()['configured'])
            finally:
                state.close()

    def test_loads_exact_codex_session_id_over_loopback_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initial = root / 'initial.jsonl'
            write_journal(initial, session_id='initial-session')
            codex_home = root / 'codex-home'
            session_id = '00000000-0000-4000-8000-000000000001'
            journal = codex_home / 'sessions' / '2026' / '08' / f'rollout-{session_id}.jsonl'
            journal.parent.mkdir(parents=True)
            write_journal(journal, session_id=session_id)
            state = DashboardState(
                analyze_journals([initial], include_trace=True),
                title='Session ID Test',
                qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
                codex_home=codex_home,
            )
            server = create_dashboard_server(output_dir=root, state=state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = dashboard_server_url(server).rstrip('/')
                request = urllib.request.Request(
                    f'{base_url}/api/session/id',
                    data=json.dumps({'session_id': session_id}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read())

                self.assertEqual(response.status, 200)
                self.assertEqual(
                    {trace['session_id'] for trace in payload['traces']},
                    {'initial-session', session_id},
                )
                self.assertIn(str(journal.resolve()), state.status()['source_paths'])

                invalid = urllib.request.Request(
                    f'{base_url}/api/session/id',
                    data=json.dumps({'session_id': '../outside'}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(invalid, timeout=5)
                self.assertEqual(rejected.exception.code, 400)
                self.assertIn('invalid session ID', rejected.exception.read().decode('utf-8'))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                state.close()

    def test_configures_and_clears_qa_over_loopback_api_without_echoing_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal)
            state = DashboardState(
                analyze_journals([journal], include_trace=True),
                title='Test Dashboard',
                qa=_FakeQA(QASettings(api_key='')),
            )
            server = create_dashboard_server(output_dir=root, state=state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = dashboard_server_url(server).rstrip('/')
                body = json.dumps(
                    {
                        'api_key': 'sk-loopback-test-secret',
                        'provider': 'openai',
                        'model': 'test-model',
                        'base_url': 'https://example.test/v1',
                        'remember': False,
                    }
                ).encode('utf-8')
                request = urllib.request.Request(
                    f'{base_url}/api/qa/config',
                    data=body,
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    configured = json.loads(response.read())

                self.assertTrue(configured['qa']['configured'])
                self.assertFalse(configured['qa']['remembered'])
                self.assertNotIn('sk-loopback-test-secret', json.dumps(configured))

                with urllib.request.urlopen(f'{base_url}/api/status', timeout=5) as response:
                    status = json.loads(response.read())
                self.assertTrue(status['qa']['configured'])
                self.assertNotIn('sk-loopback-test-secret', json.dumps(status))

                clear_request = urllib.request.Request(f'{base_url}/api/qa/config', method='DELETE')
                with urllib.request.urlopen(clear_request, timeout=5) as response:
                    cleared = json.loads(response.read())
                self.assertFalse(cleared['qa']['configured'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                state.close()

    def test_unlocks_encrypted_vault_over_loopback_api_without_echoing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            config_path = root / 'model-api.json'
            password = 'loopback vault password'
            write_journal(journal)
            initial = JournalQA(
                QASettings(api_key=''),
                config_store=APIConfigStore(config_path, secret_store=_UnavailableSecretStore()),
            )
            initial.configure(
                api_key='loopback-encrypted-secret',
                provider='openai',
                model='test-model',
                base_url='https://example.test/v1',
                remember=True,
                vault_password=password,
            )
            state = DashboardState(
                analyze_journals([journal], include_trace=True),
                title='Test Dashboard',
                qa=JournalQA(
                    QASettings(api_key=''),
                    config_store=APIConfigStore(config_path, secret_store=_UnavailableSecretStore()),
                ),
            )
            server = create_dashboard_server(output_dir=root, state=state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = dashboard_server_url(server).rstrip('/')
                self.assertTrue(state.status()['qa']['credential_locked'])
                request = urllib.request.Request(
                    f'{base_url}/api/qa/unlock',
                    data=json.dumps({'vault_password': password}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    unlocked = json.loads(response.read())

                self.assertTrue(unlocked['qa']['configured'])
                self.assertFalse(unlocked['qa']['credential_locked'])
                self.assertNotIn(password, json.dumps(unlocked))
                self.assertNotIn('loopback-encrypted-secret', json.dumps(unlocked))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                state.close()

    def test_starts_and_reads_button_triggered_agent_workflows_over_loopback_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-agent')
            repair = _FakeRepair()
            repair.brief = {
                'available': True,
                'session_id': 'session-agent',
                'status': 'current',
                'run_id': 'brief-run-1',
                'revision': 2,
                'event_count': 12,
                'through_event_sequence': 12,
                'latest_event_sequence': 12,
                'new_event_count': 0,
                'stale': False,
                'result': {
                    'workflow': 'checkpoints',
                    'scope': 'session',
                    'title': 'Synthetic session brief',
                    'summary': 'The complete session was summarized.',
                    'objective': 'Exercise the parser.',
                    'outcome': 'completed',
                    'checkpoints': [],
                    'blockers': [],
                    'next_steps': [],
                },
            }
            state = DashboardState(
                analyze_journals([journal], include_trace=True),
                title='Test Dashboard',
                qa=_FakeQA(QASettings(api_key='test-key', model='test-model')),
                repair=repair,
            )
            server = create_dashboard_server(output_dir=root, state=state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = dashboard_server_url(server).rstrip('/')
                with urllib.request.urlopen(
                    f'{base_url}/api/agent/session-brief?session_id=session-agent', timeout=5
                ) as response:
                    brief = json.loads(response.read())
                self.assertEqual(brief['result']['scope'], 'session')
                self.assertEqual(brief['revision'], 2)
                dashboard_state = {
                    'source': {'session_id': 'session-agent'},
                    'selection': {
                        'turn_id': 'turn-1',
                        'event_sequence': 1,
                        'highlight': {'text': ' selected trace text ', 'origin': 'trace event'},
                        'checkpoint': {
                            'run_id': 'checkpoint-run',
                            'turn_id': 'turn-1',
                            'index': 2,
                            'title': 'Verified the parser',
                            'status': 'completed',
                            'summary': 'The parser checks passed.',
                            'actions': ['Ran deterministic parser checks.'],
                            'achievements': ['Confirmed normalized event coverage.'],
                            'blockers': ['One live result was unavailable.'],
                            'artifacts': ['tests/test_parser.py'],
                            'next_steps': ['Inspect the missing live result.'],
                            'evidence_anchors': ['[turn turn-1, line 3]'],
                        },
                        'contract': {
                            'id': 'approval-before-production',
                            'version': '2.1',
                            'turn_id': 'turn-1',
                            'title': 'Require approval before production actions',
                            'severity': 'critical',
                            'status': 'violated',
                            'expectation': 'Approval must precede deployment.',
                            'observation': 'Deployment was attempted without approval.',
                            'evidence_anchors': ['[turn turn-1, line 3]'],
                        },
                    },
                    'view': {'active_tab': 'trace', 'category': 'tool'},
                }
                harness_request = urllib.request.Request(
                    f'{base_url}/api/agent/config',
                    data=json.dumps({'harness': 'pydantic'}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(harness_request, timeout=5) as response:
                    harness_status = json.loads(response.read())
                self.assertEqual(harness_status['agent']['harness_id'], 'pydantic')
                self.assertEqual(harness_status['qa']['agent_harness'], 'Pydantic AI')
                self.assertEqual(repair.harness, 'pydantic')
                client_session_nonce = 'client-session-nonce-000001'
                for endpoint, kind in (
                    ('investigate', 'investigate'),
                    ('memories', 'memories'),
                    ('checkpoints', 'checkpoints'),
                    ('audit', 'audit'),
                    ('repair', 'repair'),
                    ('customize', 'customize'),
                ):
                    request_payload: dict[str, object] = {'dashboard_state': dashboard_state}
                    if kind == 'repair':
                        request_payload['audit_run_id'] = 'completedaudit1'
                        request_payload['instruction'] = 'Fix this parser.'
                        request_payload['client_session_nonce'] = client_session_nonce
                    if kind == 'customize':
                        request_payload['instruction'] = 'Make the trace detail panel wider.'
                        request_payload['client_session_nonce'] = client_session_nonce
                    request = urllib.request.Request(
                        f'{base_url}/api/agent/{endpoint}',
                        data=json.dumps(request_payload).encode('utf-8'),
                        headers={'Content-Type': 'application/json'},
                        method='POST',
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        started = json.loads(response.read())
                    self.assertEqual(response.status, 202)
                    if kind in {'repair', 'customize'}:
                        approvals_before = len(repair.approved_actions)
                        self.assertEqual(started['kind'], 'authorization')
                        expected_action = 'repair_parser' if kind == 'repair' else 'customize_dashboard'
                        self.assertEqual(started['authorization']['action'], expected_action)
                        self.assertEqual(started['authorization']['activation_mode'], 'activate_verified_runtime')
                        self.assertEqual(len(repair.approved_actions), approvals_before)
                        approve = urllib.request.Request(
                            f'{base_url}/api/agent/source-actions/{started["authorization"]["id"]}/approve',
                            data=json.dumps(
                                {
                                    'token': started['authorization']['token'],
                                    'client_session_nonce': client_session_nonce,
                                }
                            ).encode('utf-8'),
                            headers={'Content-Type': 'application/json'},
                            method='POST',
                        )
                        with urllib.request.urlopen(approve, timeout=5) as approved_response:
                            approved = json.loads(approved_response.read())
                        self.assertEqual(approved_response.status, 202)
                        self.assertEqual(approved['run']['run_id'], 'repairrun1')
                        self.assertEqual(approved['run']['kind'], kind)
                    else:
                        self.assertEqual(started['run_id'], 'repairrun1')
                        self.assertEqual(started['kind'], kind)
                    self.assertEqual(repair.dashboard_state, dashboard_state)
                self.assertEqual(repair.audit_run_id, 'completedaudit1')
                self.assertEqual(repair.instruction, 'Make the trace detail panel wider.')

                with urllib.request.urlopen(f'{base_url}/api/agent/runs/repairrun1', timeout=5) as response:
                    loaded = json.loads(response.read())
                self.assertEqual(loaded['kind'], 'customize')

                cancel = urllib.request.Request(f'{base_url}/api/agent/runs/repairrun1', method='DELETE')
                with urllib.request.urlopen(cancel, timeout=5) as response:
                    cancelled = json.loads(response.read())
                self.assertIn('Cancellation requested', cancelled['message'])

                action = urllib.request.Request(
                    f'{base_url}/api/agent/runs/repairrun1/actions',
                    data=json.dumps(
                        {
                            'action': 'continue',
                            'instruction': 'Resume after configuring a fallback model.',
                            'client_session_nonce': client_session_nonce,
                        }
                    ).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(action, timeout=5) as response:
                    continued = json.loads(response.read())
                self.assertEqual(response.status, 202)
                self.assertEqual(continued['kind'], 'authorization')
                self.assertEqual(continued['authorization']['action'], 'continue_run')
                approve_continue = urllib.request.Request(
                    f'{base_url}/api/agent/source-actions/{continued["authorization"]["id"]}/approve',
                    data=json.dumps(
                        {
                            'token': continued['authorization']['token'],
                            'client_session_nonce': client_session_nonce,
                        }
                    ).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(approve_continue, timeout=5) as response:
                    continued_run = json.loads(response.read())
                self.assertIn('continue requested', continued_run['run']['message'])
                self.assertEqual(
                    repair.action,
                    {
                        'action': 'continue',
                        'instruction': 'Resume after configuring a fallback model.',
                        'model': 'test-model',
                    },
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                state.close()

    def test_agent_message_routes_answers_and_explicit_parser_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-agent')
            repair = _FakeRepair()
            repair.brief = {
                'available': True,
                'session_id': 'session-agent',
                'status': 'out_of_date',
                'run_id': 'brief-run-1',
                'revision': 3,
                'event_count': 12,
                'through_event_sequence': 10,
                'latest_event_sequence': 12,
                'new_event_count': 2,
                'stale': True,
                'result': {
                    'title': 'Session brief',
                    'summary': 'The parser workflow is still running.',
                    'objective': 'Validate the parser.',
                    'outcome': 'in_progress',
                    'checkpoints': [{'title': 'Validation', 'status': 'in_progress'}],
                    'blockers': ['A check is pending.'],
                    'next_steps': ['Read the latest result.'],
                },
            }
            qa = _FakeQA(QASettings(api_key='test-key', model='test-model'))
            state = DashboardState(
                analyze_journals([journal], include_trace=True),
                title='Test Dashboard',
                qa=qa,
                repair=repair,
            )
            server = create_dashboard_server(output_dir=root, state=state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = dashboard_server_url(server).rstrip('/')
                dashboard_state = {
                    'source': {'session_id': 'session-agent'},
                    'selection': {
                        'turn_id': 'turn-1',
                        'event_sequence': 1,
                        'highlight': {'text': ' selected trace text ', 'origin': 'trace event'},
                        'ask_target': {
                            'kind': 'message',
                            'turn_id': 'turn-1',
                            'event_sequence': 1,
                            'line_number': 3,
                            'label': 'Assistant message',
                            'summary': 'Implemented the requested change.',
                            'text': 'The parser checks passed.',
                            'role': 'assistant',
                            'category': 'message',
                            'status': 'completed',
                        },
                        'checkpoint': {
                            'run_id': 'checkpoint-run',
                            'turn_id': 'turn-1',
                            'index': 2,
                            'title': 'Verified the parser',
                            'status': 'completed',
                            'summary': 'The parser checks passed.',
                            'turn_ids': ['turn-1', 'turn-2'],
                            'start_event_sequence': 2,
                            'end_event_sequence': 8,
                            'actions': ['Ran deterministic parser checks.'],
                            'achievements': ['Confirmed normalized event coverage.'],
                            'blockers': ['One live result was unavailable.'],
                            'artifacts': ['tests/test_parser.py'],
                            'next_steps': ['Inspect the missing live result.'],
                            'evidence_anchors': ['[turn turn-1, line 3]'],
                        },
                        'contract': {
                            'id': 'approval-before-production',
                            'version': '2.1',
                            'turn_id': 'turn-1',
                            'title': 'Require approval before production actions',
                            'severity': 'critical',
                            'status': 'violated',
                            'expectation': 'Approval must precede deployment.',
                            'observation': 'Deployment was attempted without approval.',
                            'evidence_anchors': ['[turn turn-1, line 3]'],
                        },
                    },
                    'view': {'active_tab': 'trace', 'category': 'tool'},
                }
                client_session_nonce = 'client-session-nonce-000001'
                conversation_id = str(uuid4())

                def request(message: str) -> tuple[int, dict[str, object]]:
                    value = urllib.request.Request(
                        f'{base_url}/api/agent/message',
                        data=json.dumps(
                            {
                                'message': message,
                                'session_id': 'session-agent',
                                'turn_id': 'turn-1',
                                'dashboard_state': dashboard_state,
                                'history': [],
                                'client_session_nonce': client_session_nonce,
                                'request_id': str(uuid4()),
                                'conversation_id': conversation_id,
                            }
                        ).encode('utf-8'),
                        headers={'Content-Type': 'application/json'},
                        method='POST',
                    )
                    with urllib.request.urlopen(value, timeout=5) as response:
                        return response.status, json.loads(response.read())

                status, answer = request('What happened?')
                self.assertEqual(status, 200)
                self.assertEqual(answer['kind'], 'answer')
                self.assertEqual(answer['action'], 'answer')
                self.assertIn('Grounded controller answer', answer['answer'])
                self.assertEqual(answer['request_activity']['status'], 'completed')
                self.assertEqual(answer['request_activity']['activity'], [])
                self.assertEqual(qa.last_route_kwargs['scope'], 'journal')
                self.assertEqual(qa.last_route_kwargs['conversation_id'], conversation_id)
                self.assertIsInstance(qa.last_route_kwargs['cancellation_event'], threading.Event)
                self.assertEqual(
                    qa.last_route_kwargs['view_state'],
                    {
                        'highlighted_text': 'selected trace text',
                        'highlight_origin': 'trace event',
                        'ask_target_kind': 'message',
                        'ask_target_event_sequence': '1',
                        'ask_target_line_number': '3',
                        'ask_target_label': 'Assistant message',
                        'ask_target_summary': 'Implemented the requested change.',
                        'ask_target_text': 'The parser checks passed.',
                        'ask_target_role': 'assistant',
                        'ask_target_category': 'message',
                        'ask_target_status': 'completed',
                        'checkpoint_index': '2',
                        'checkpoint_title': 'Verified the parser',
                        'checkpoint_status': 'completed',
                        'checkpoint_summary': 'The parser checks passed.',
                        'checkpoint_turn_ids': 'turn-1 | turn-2',
                        'checkpoint_start_event_sequence': '2',
                        'checkpoint_end_event_sequence': '8',
                        'checkpoint_actions': 'Ran deterministic parser checks.',
                        'checkpoint_achievements': 'Confirmed normalized event coverage.',
                        'checkpoint_blockers': 'One live result was unavailable.',
                        'checkpoint_artifacts': 'tests/test_parser.py',
                        'checkpoint_next_steps': 'Inspect the missing live result.',
                        'checkpoint_anchors': '[turn turn-1, line 3]',
                        'contract_id': 'approval-before-production',
                        'contract_version': '2.1',
                        'contract_title': 'Require approval before production actions',
                        'contract_severity': 'critical',
                        'contract_status': 'violated',
                        'contract_expectation': 'Approval must precede deployment.',
                        'contract_observation': 'Deployment was attempted without approval.',
                        'contract_anchors': '[turn turn-1, line 3]',
                        'category': 'tool',
                        'session_brief_revision': '3',
                        'session_brief_through_sequence': '10',
                        'session_brief_event_count': '12',
                        'session_brief_new_event_count': '2',
                        'session_brief_title': 'Session brief',
                        'session_brief_summary': 'The parser workflow is still running.',
                        'session_brief_objective': 'Validate the parser.',
                        'session_brief_outcome': 'in_progress',
                        'session_brief_checkpoints': '[{"title":"Validation","status":"in_progress"}]',
                        'session_brief_blockers': '["A check is pending."]',
                        'session_brief_next_steps': '["Read the latest result."]',
                    },
                )
                resources = qa.last_route_kwargs['dashboard_resources']
                catalog_names = {item['name'] for item in resources.catalog}
                self.assertEqual(
                    catalog_names,
                    {
                        'audit_rules',
                        'audit_findings',
                        'session_brief',
                        'agent_runs',
                        'session_metrics',
                        'current_selection',
                    },
                )
                brief_resource = resources.read('session_brief', '', 20)
                metrics_resource = resources.read('session_metrics', '', 20)
                selection_resource = resources.read('current_selection', '', 20)
                self.assertEqual(brief_resource['result']['title'], 'Session brief')
                self.assertEqual(metrics_resource['session_id'], 'session-agent')
                self.assertEqual(selection_resource['turn_id'], 'turn-1')
                serialized_resources = json.dumps(
                    [brief_resource, metrics_resource, selection_resource],
                    sort_keys=True,
                )
                self.assertNotIn(str(journal), serialized_resources)
                self.assertNotIn('session_file', serialized_resources)

                dashboard_state['selection']['checkpoint']['turn_id'] = 'turn-2'
                status, _ = request('What happened?')
                self.assertEqual(status, 200)
                dashboard_state['selection']['checkpoint']['turn_id'] = 'turn-1'

                dashboard_state['selection']['contract']['turn_id'] = 'turn-2'
                with self.assertRaises(urllib.error.HTTPError) as rejected_contract:
                    request('What happened?')
                self.assertEqual(rejected_contract.exception.code, 400)
                dashboard_state['selection']['contract']['turn_id'] = 'turn-1'

                dashboard_state['selection']['ask_target']['event_sequence'] = 2
                with self.assertRaises(urllib.error.HTTPError) as rejected_ask_target:
                    request('What happened?')
                self.assertEqual(rejected_ask_target.exception.code, 400)
                dashboard_state['selection']['ask_target']['event_sequence'] = 1

                dashboard_state['selection']['checkpoint']['actions'] = 'not-a-list'
                with self.assertRaises(urllib.error.HTTPError) as rejected_checkpoint_detail:
                    request('What happened?')
                self.assertEqual(rejected_checkpoint_detail.exception.code, 400)
                dashboard_state['selection']['checkpoint']['actions'] = ['Ran deterministic parser checks.']

                qa.controller_action = 'repair_parser'
                status, pending = request('Fix this parser.')
                self.assertEqual(status, 202)
                self.assertEqual(pending['action'], 'repair_parser')
                self.assertEqual(pending['kind'], 'authorization')
                self.assertEqual(pending['authorization']['action'], 'repair_parser')
                approve_repair = urllib.request.Request(
                    f'{base_url}/api/agent/source-actions/{pending["authorization"]["id"]}/approve',
                    data=json.dumps(
                        {
                            'token': pending['authorization']['token'],
                            'client_session_nonce': client_session_nonce,
                        }
                    ).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(approve_repair, timeout=5) as response:
                    started = json.loads(response.read())
                self.assertEqual(started['run']['kind'], 'repair')
                self.assertEqual(repair.instruction, 'Fix this parser.')

                qa.controller_action = 'customize_dashboard'
                status, pending = request('turn the theme into red')
                self.assertEqual(status, 202)
                self.assertEqual(pending['action'], 'customize_dashboard')
                self.assertEqual(pending['kind'], 'authorization')
                self.assertEqual(pending['authorization']['action'], 'customize_dashboard')
                approve_customize = urllib.request.Request(
                    f'{base_url}/api/agent/source-actions/{pending["authorization"]["id"]}/approve',
                    data=json.dumps(
                        {
                            'token': pending['authorization']['token'],
                            'client_session_nonce': client_session_nonce,
                        }
                    ).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(approve_customize, timeout=5) as response:
                    started = json.loads(response.read())
                self.assertEqual(started['run']['kind'], 'customize')
                self.assertEqual(repair.instruction, 'turn the theme into red')

                status, broad_pending = request('make this denser')
                self.assertEqual(status, 202)
                self.assertEqual(broad_pending['kind'], 'authorization')
                self.assertEqual(broad_pending['authorization']['action'], 'customize_dashboard')

                with self.assertRaises(urllib.error.HTTPError) as rejected_question:
                    request('How would you make it blue?')
                self.assertEqual(rejected_question.exception.code, 400)
                question_error = json.loads(rejected_question.exception.read())
                self.assertIn('approval suppressed', question_error['error'])

                with self.assertRaises(urllib.error.HTTPError) as rejected_reported:
                    request('The previous user said make it blue.')
                self.assertEqual(rejected_reported.exception.code, 400)
                reported_error = json.loads(rejected_reported.exception.read())
                self.assertIn('reported speech', reported_error['error'])

                qa.controller_action = 'summarize_checkpoints'
                status, started = request('Summarize checkpoints for this turn.')
                self.assertEqual(status, 202)
                self.assertEqual(started['action'], 'summarize_checkpoints')
                self.assertEqual(started['run']['kind'], 'checkpoints')

                qa.controller_action = 'repair_parser'
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    request('What would fixing this parser do?')
                self.assertEqual(rejected.exception.code, 400)
                error = json.loads(rejected.exception.read())
                self.assertIn('approval suppressed', error['error'])

                clear = urllib.request.Request(
                    f'{base_url}/api/agent/conversations/{conversation_id}?session_id=session-agent',
                    method='DELETE',
                )
                with urllib.request.urlopen(clear, timeout=5) as response:
                    cleared = json.loads(response.read())
                self.assertEqual(cleared['harness_branches_removed'], 2)
                self.assertEqual(
                    qa.last_clear_kwargs,
                    {'conversation_id': conversation_id, 'session_id': 'session-agent'},
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                state.close()

    def test_audit_rule_api_supports_manual_updates_and_agent_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-rules')
            qa = _FakeQA(QASettings(api_key='test-key', model='test-model'))
            rules = AuditRuleStore(root / 'state' / 'audit-rules.json')
            state = DashboardState(
                analyze_journals([journal], include_trace=True),
                title='Rule Test',
                qa=qa,
                repair=_FakeRepair(),
                audit_rules=rules,
            )
            server = create_dashboard_server(output_dir=root, state=state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = dashboard_server_url(server).rstrip('/')

                def post(path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
                    request = urllib.request.Request(
                        f'{base_url}{path}',
                        data=json.dumps(payload).encode('utf-8'),
                        headers={'Content-Type': 'application/json'},
                        method='POST',
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status, json.loads(response.read())

                manual_rule = {
                    'id': 'forbid-web-search',
                    'title': 'Forbid web search',
                    'expectation': 'The agent must not search the web.',
                    'severity': 'high',
                    'enabled': True,
                    'rule_type': 'forbid_event',
                    'event': {'category': 'tool', 'tool_name': 'search'},
                    'required_event': None,
                    'same_turn': False,
                }
                status, saved = post('/api/audit-rules', {'rule': manual_rule, 'expected_version': None})
                self.assertEqual(status, 200)
                self.assertEqual(saved['rule']['version'], 1)

                with urllib.request.urlopen(
                    f'{base_url}/api/audit-rules?session_id=session-rules', timeout=5
                ) as response:
                    catalog = json.loads(response.read())
                self.assertEqual(catalog['rule_set']['active_count'], 1)
                self.assertEqual(catalog['assurance']['contracts'][0]['status'], 'violated')
                self.assertTrue(catalog['assurance']['contracts'][0]['evidence'])

                dashboard_state = {
                    'source': {'session_id': 'session-rules'},
                    'selection': {'turn_id': 'turn-1', 'event_sequence': 1},
                    'view': {'active_tab': 'trace'},
                }
                qa.controller_action = 'manage_audit_rules'
                status, drafted = post(
                    '/api/agent/message',
                    {
                        'message': 'Create an audit rule requiring validation after source edits.',
                        'scope': 'journal',
                        'session_id': 'session-rules',
                        'turn_id': 'turn-1',
                        'dashboard_state': dashboard_state,
                        'history': [],
                        'client_session_nonce': 'client-session-nonce-000001',
                        'request_id': str(uuid4()),
                    },
                )
                self.assertEqual(status, 202)
                self.assertEqual(drafted['kind'], 'audit_rule_proposal')
                self.assertEqual(drafted['proposal']['rule']['id'], 'validate-after-source-edit')
                self.assertEqual(rules.snapshot()['active_count'], 1)

                status, approved = post(
                    f'/api/audit-rules/proposals/{drafted["proposal"]["id"]}/approve',
                    {
                        'token': drafted['proposal']['token'],
                        'client_session_nonce': 'client-session-nonce-000001',
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(approved['rule']['updated_by'], 'agent')
                self.assertEqual(approved['rule_set']['active_count'], 2)
                self.assertEqual(qa.last_rule_kwargs['current_rules'][0]['id'], 'forbid-web-search')
                self.assertIs(
                    qa.last_route_kwargs['cancellation_event'],
                    qa.last_rule_kwargs['cancellation_event'],
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                state.close()


if __name__ == '__main__':
    unittest.main()
