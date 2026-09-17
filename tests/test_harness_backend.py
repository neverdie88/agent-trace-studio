from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from pydantic_ai.messages import ModelMessagesTypeAdapter

from agent_trace_studio.agent_backend import ParserAudit, SessionCheckpointSummary
from agent_trace_studio.harness_backend import (
    HarnessStatus,
    OpenCodeFixerSession,
    SelectableRepairAgents,
    _execute_trace_context_plan,
    _parse_checkpoint_retrieval_plan,
    _parse_checkpoint_summary,
    _probe_opencode,
    _retrieve_checkpoint_evidence,
    _run_codex_trace_qa,
    _run_opencode_controller,
    _run_opencode_trace_qa,
    _run_studio_subprocess,
    _safe_subprocess_environment,
)
from agent_trace_studio.parser import analyze_journals
from agent_trace_studio.qa import (
    AgentMessageDecision,
    AgentMessageResult,
    DashboardResourceAccess,
    QASettings,
    QAUnavailableError,
    StudioAgentCancelled,
    TraceContextPlan,
    TraceQAAgentResult,
    TraceQAContext,
    parse_agent_message_decision,
)
from helpers import write_journal


def _ready(harness_id: str, label: str) -> HarnessStatus:
    return HarnessStatus(harness_id, label, True, 'Ready for test.')  # type: ignore[arg-type]


class SubprocessEnvironmentTests(unittest.TestCase):
    def test_windows_runtime_variables_survive_without_forwarding_secrets(self) -> None:
        windows = {'SYSTEMROOT': r'C:\Windows', 'WINDIR': r'C:\Windows', 'TEMP': r'C:\Temp', 'TMP': r'C:\Temp'}
        with mock.patch.dict(
            os.environ,
            {**windows, 'PATH': 'synthetic-path', 'OPENAI_API_KEY': 'DO_NOT_FORWARD', 'NODE_OPTIONS': 'DO_NOT_FORWARD'},
            clear=True,
        ):
            environment = _safe_subprocess_environment()
        self.assertEqual(environment, {**windows, 'PATH': 'synthetic-path'})


class OpenCodeReadinessTests(unittest.TestCase):
    def probe(self, stdout: str, *, stderr: str = '', code: int = 0, version_code: int = 0, version: str = '1.18.18'):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'opencode'
            binary.touch()
            results = [
                subprocess.CompletedProcess([], version_code, version, ''),
                subprocess.CompletedProcess([], code, stdout, stderr),
            ]
            with (
                mock.patch('agent_trace_studio.harness_backend._opencode_binary', return_value=binary),
                mock.patch('agent_trace_studio.harness_backend.subprocess.run', side_effect=results) as run,
            ):
                return _probe_opencode(), run.call_args_list

    def test_positive_credential_counts_are_not_mistaken_for_zero(self) -> None:
        for count in (1, 10, 20, 100):
            with self.subTest(count=count):
                status, _ = self.probe(f'\x1b[32m└  {count} credentials\x1b[0m\n')
                self.assertTrue(status.available)
                self.assertEqual(status.version, '1.18.18')

    def test_only_successful_zero_count_requests_login(self) -> None:
        status, _ = self.probe('└  0 credentials\n')
        self.assertFalse(status.available)
        self.assertIn('auth login', status.detail)
        status, _ = self.probe('0 credentials', code=1)
        self.assertFalse(status.available)
        self.assertNotIn('auth login', status.detail)
        self.assertIn('check failed', status.detail)

    def test_log_open_failure_is_not_a_missing_credential(self) -> None:
        status, _ = self.probe(
            '',
            code=1,
            stderr=(
                '\x1b[91mError\x1b[0m\nUnknown: FileSystem.open '
                '(/synthetic/private/opencode/log/opencode.log) synthetic-key'
            ),
        )
        self.assertFalse(status.available)
        self.assertIn('cannot open its local log file', status.detail)
        self.assertIn('local terminal', status.detail)
        self.assertNotIn('auth login', status.detail)
        self.assertNotIn('/synthetic/private', str(status.public_dict()))
        self.assertNotIn('synthetic-key', str(status.public_dict()))

    def test_other_failures_and_unrecognized_outputs_fail_closed(self) -> None:
        for stdout, stderr, code in (
            ('', 'EACCES: synthetic-key', 1),
            ('', 'synthetic-key', 2),
            ('', '', 0),
            ('10 credentials\n0 credentials', '', 0),
        ):
            with self.subTest(code=code, output=stdout):
                status, _ = self.probe(stdout, stderr=stderr, code=code)
                self.assertFalse(status.available)
                self.assertNotIn('auth login', status.detail)
                self.assertNotIn('synthetic-key', status.detail)

    def test_version_failure_stops_before_auth_and_private_output_is_omitted(self) -> None:
        status, calls = self.probe('1 credentials', version_code=1, version='synthetic-key')
        self.assertFalse(status.available)
        self.assertEqual(len(calls), 1)
        self.assertIn('version check failed', status.detail)
        self.assertNotIn('synthetic-key', str(status.public_dict()))
        status, _ = self.probe('1 credentials', version='synthetic-key')
        self.assertEqual(status.version, '')
        self.assertFalse(status.available)

    def test_probes_keep_native_directory_overrides_but_not_api_keys(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                'OPENAI_API_KEY': 'synthetic-key',
                'OPENCODE_AUTH_CONTENT': 'synthetic-auth',
                'XDG_DATA_HOME': '/synthetic/native-data',
            },
        ):
            _, calls = self.probe('1 credentials')
        for call in calls:
            environment = call.kwargs['env']
            self.assertNotIn('OPENAI_API_KEY', environment)
            self.assertNotIn('OPENCODE_AUTH_CONTENT', environment)
            self.assertEqual(environment['XDG_DATA_HOME'], '/synthetic/native-data')

    def test_timeout_is_reported_without_private_exception_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'opencode'
            binary.touch()
            with (
                mock.patch('agent_trace_studio.harness_backend._opencode_binary', return_value=binary),
                mock.patch(
                    'agent_trace_studio.harness_backend.subprocess.run',
                    side_effect=subprocess.TimeoutExpired('synthetic-secret', 20),
                ),
            ):
                status = _probe_opencode()
        self.assertFalse(status.available)
        self.assertIn('TimeoutExpired', status.detail)
        self.assertNotIn('synthetic-secret', status.detail)


class HarnessSelectionTests(unittest.TestCase):
    def test_external_harnesses_can_request_dashboard_resources_before_answering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal)
            result = analyze_journals([journal], include_trace=True)
            resources = DashboardResourceAccess(
                catalog=(
                    {
                        'name': 'audit_rules',
                        'description': 'Saved rules.',
                        'revision': 1,
                        'item_count': 1,
                        'available': True,
                    },
                ),
                reader=lambda resource, resource_id, _limit: {
                    'resource': resource,
                    'items': [{'id': resource_id, 'title': 'Deployment Approval Request'}],
                },
            )
            context = TraceQAContext(
                result=result,
                question='Summarize the current audit rule.',
                scope='journal',
                session_id='session-1',
                turn_id='turn-1',
                event_sequence=3,
                view_state={},
                max_context_chars=20_000,
                dashboard_resources=resources,
            )
            plan = TraceQAAgentResult(
                '{"context_requests":[{"tool":"read_dashboard_resource",'
                '"resource":"audit_rules","resource_id":"deployment-approval-request"}]}',
                'gpt-test',
                'test-provider',
                'test-harness',
                {'requests': 1},
                [],
            )
            answer = TraceQAAgentResult(
                '{"action":"answer","answer":"The deployment approval rule is active."}',
                'gpt-test',
                'test-provider',
                'test-harness',
                {'requests': 1},
                [],
            )
            for harness_id in ('codex-sdk', 'opencode'):
                with self.subTest(harness=harness_id):
                    registry = SelectableRepairAgents(root / harness_id, default_harness=harness_id)
                    activity = []
                    with (
                        mock.patch.object(registry, 'trace_qa_status', return_value={'available': True}),
                        mock.patch.object(
                            registry,
                            '_run_external_studio_turn',
                            side_effect=[plan, answer],
                        ) as run_turn,
                    ):
                        response = registry.route_message(
                            prompt='CURRENT CONTROLLER REQUEST',
                            context=context,
                            history=[],
                            settings=QASettings(api_key=''),
                            progress=activity.append,
                        )

                    self.assertEqual(response.decision.action, 'answer')
                    self.assertEqual(response.tools, ['read_dashboard_resource'])
                    self.assertIn('Deployment Approval Request', run_turn.call_args_list[1].args[0])
                    self.assertIn('dashboard resource audit_rules', activity[1].message)

    def test_defaults_to_opencode_and_persists_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            registry = SelectableRepairAgents(state_dir)
            self.assertEqual(registry.harness_id, 'opencode')

            with (
                mock.patch(
                    'agent_trace_studio.harness_backend._probe_opencode',
                    return_value=_ready('opencode', 'OpenCode'),
                ),
                mock.patch(
                    'agent_trace_studio.harness_backend._probe_codex_sdk',
                    return_value=_ready('codex-sdk', 'Codex SDK'),
                ),
            ):
                selected = registry.select_harness('pydantic')

            self.assertEqual(selected['id'], 'pydantic')
            self.assertEqual(json.loads((state_dir / 'agent-harness.json').read_text())['harness'], 'pydantic')
            restored = SelectableRepairAgents(state_dir)
            self.assertEqual(restored.harness_id, 'pydantic')

    def test_rejects_an_unavailable_harness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = SelectableRepairAgents(Path(directory))
            unavailable = HarnessStatus('opencode', 'OpenCode', False, 'OpenCode login required.')
            with (
                mock.patch('agent_trace_studio.harness_backend._probe_opencode', return_value=unavailable),
                mock.patch(
                    'agent_trace_studio.harness_backend._probe_codex_sdk',
                    return_value=_ready('codex-sdk', 'Codex SDK'),
                ),
                self.assertRaisesRegex(ValueError, 'login required'),
            ):
                registry.select_harness('opencode')

    def test_selected_agent_type_controls_qa_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = SelectableRepairAgents(Path(directory))
            settings = QASettings(api_key='')
            with (
                mock.patch(
                    'agent_trace_studio.harness_backend._probe_opencode',
                    return_value=_ready('opencode', 'OpenCode'),
                ),
                mock.patch(
                    'agent_trace_studio.harness_backend._probe_codex_sdk',
                    return_value=_ready('codex-sdk', 'Codex SDK'),
                ),
            ):
                opencode = registry.trace_qa_status(settings)
                registry.select_harness('pydantic')
                pydantic = registry.trace_qa_status(settings)

        self.assertTrue(opencode['available'])
        self.assertFalse(opencode['uses_api_settings'])
        self.assertEqual(opencode['model'], 'openai/gpt-5.6-sol')
        self.assertFalse(pydantic['available'])
        self.assertTrue(pydantic['uses_api_settings'])
        self.assertIn('Configure a model API', pydantic['detail'])

    def test_external_controller_selects_its_own_trace_context_before_answering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal)
            result = analyze_journals([journal], include_trace=True)
            context = TraceQAContext(
                result=result,
                question='What did exec_command return?',
                scope='journal',
                session_id='session-1',
                turn_id='turn-1',
                event_sequence=3,
                view_state={},
                max_context_chars=20_000,
            )
            responses = [
                TraceQAAgentResult(
                    '{"context_requests":[{"tool":"search_trace","query":"exec_command","limit":2}]}',
                    'gpt-test',
                    'Codex authentication',
                    'Codex SDK',
                    {'requests': 1},
                    [],
                    native_session_id='thread-1',
                ),
                TraceQAAgentResult(
                    '{"action":"answer","answer":"The command completed [turn turn-1, line 4]."}',
                    'gpt-test',
                    'Codex authentication',
                    'Codex SDK',
                    {'requests': 1},
                    [],
                    native_session_id='thread-1',
                ),
            ]
            registry = SelectableRepairAgents(root / 'state', default_harness='codex-sdk')
            activity = []
            with (
                mock.patch.object(registry, 'trace_qa_status', return_value={'available': True}),
                mock.patch.object(registry, '_run_external_studio_turn', side_effect=responses) as run_turn,
            ):
                response = registry.route_message(
                    prompt='CURRENT CONTROLLER REQUEST',
                    context=context,
                    history=[],
                    settings=QASettings(api_key=''),
                    conversation_id='00000000-0000-4000-8000-000000000003',
                    progress=activity.append,
                )

        self.assertEqual(response.decision.action, 'answer')
        self.assertEqual(response.tools, ['search_trace'])
        self.assertEqual(response.usage['requests'], 2)
        self.assertEqual(run_turn.call_count, 2)
        self.assertIn('HOST-EXECUTED CONTEXT ACTION RESULTS', run_turn.call_args_list[1].args[0])
        self.assertIn('tool=exec_command', run_turn.call_args_list[1].args[0])
        self.assertNotIn('CURRENT CONTROLLER REQUEST', run_turn.call_args_list[1].args[0])
        self.assertEqual(
            [item.phase for item in activity],
            ['Context', 'Tool', 'Tool', 'Context', 'Response'],
        )
        self.assertIn('Searching bounded normalized trace evidence', activity[1].message)
        self.assertEqual(activity[1].details['status'], 'running')
        self.assertEqual(activity[2].details['status'], 'completed')
        self.assertEqual(activity[1].details['id'], activity[2].details['id'])
        self.assertEqual(activity[2].details['arguments']['query'], 'exec_command')
        self.assertIn('tool=exec_command', activity[2].details['output'])
        self.assertEqual(activity[-1].message, 'Codex SDK returned the final response.')

    def test_context_call_details_match_delivered_evidence_and_mark_host_truncation(self) -> None:
        context = TraceQAContext(
            result=mock.Mock(),
            question='Read the synthetic rules.',
            scope='journal',
            session_id='synthetic',
            turn_id=None,
            event_sequence=None,
            view_state={},
            max_context_chars=500,
        )
        plan = TraceContextPlan.model_validate(
            {'context_requests': [{'tool': 'read_dashboard_resource', 'resource': 'audit_rules', 'limit': 3}]}
        )
        activity = []
        with mock.patch('agent_trace_studio.harness_backend.read_dashboard_resource', return_value='x' * 5_000):
            evidence, invoked = _execute_trace_context_plan(context, plan, max_chars=500, progress=activity.append)
        self.assertEqual(invoked, ['read_dashboard_resource'])
        self.assertEqual(len(activity), 2)
        self.assertEqual(activity[0].details['id'], activity[1].details['id'])
        self.assertEqual(activity[1].details['output'], evidence)
        self.assertEqual(activity[1].details['output_chars'], len(evidence))
        self.assertTrue(activity[1].details['context_truncated'])
        self.assertLessEqual(len(evidence), 500)

    def test_context_call_failure_updates_its_step_without_emitting_exception_secrets(self) -> None:
        context = TraceQAContext(
            result=mock.Mock(),
            question='Read the synthetic rules.',
            scope='journal',
            session_id='synthetic',
            turn_id=None,
            event_sequence=None,
            view_state={},
            max_context_chars=2_000,
        )
        plan = TraceContextPlan.model_validate(
            {'context_requests': [{'tool': 'read_dashboard_resource', 'resource': 'audit_rules'}]}
        )
        activity = []
        with (
            mock.patch(
                'agent_trace_studio.harness_backend.read_dashboard_resource',
                side_effect=RuntimeError('PRIVATE_EXCEPTION_SECRET'),
            ),
            self.assertRaises(RuntimeError),
        ):
            _execute_trace_context_plan(context, plan, max_chars=2_000, progress=activity.append)
        self.assertEqual(activity[1].details['status'], 'failed')
        self.assertEqual(activity[0].details['id'], activity[1].details['id'])
        self.assertNotIn('PRIVATE_EXCEPTION_SECRET', json.dumps(activity[1].details))

    def test_context_call_reports_trace_and_field_truncation_from_real_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'synthetic.jsonl'
            write_journal(journal)
            result = analyze_journals([journal], include_trace=True)
            trace = result.traces[0]
            event = replace(trace.events[0], turn_id='turn-1', text='x' * 7_000)
            result = replace(result, traces=(replace(trace, events=(event,)),))
            plan = TraceContextPlan.model_validate(
                {'context_requests': [{'tool': 'read_trace_turn', 'turn_id': 'turn-1'}]}
            )
            for budget, marker in (
                (500, '[tool evidence truncated by context limit]'),
                (20_000, '[truncated]'),
            ):
                with self.subTest(budget=budget):
                    context = TraceQAContext(
                        result=result,
                        question='Read the synthetic turn.',
                        scope='journal',
                        session_id='session-1',
                        turn_id=None,
                        event_sequence=None,
                        view_state={},
                        max_context_chars=budget,
                    )
                    activity = []
                    evidence, _tools = _execute_trace_context_plan(
                        context, plan, max_chars=20_000, progress=activity.append
                    )
                    self.assertIn(marker, evidence)
                    self.assertTrue(activity[1].details['context_truncated'])
                    self.assertEqual(activity[1].details['output_chars'], len(evidence))

    def test_external_native_session_is_restored_after_registry_restart(self) -> None:
        context = TraceQAContext(
            result=mock.Mock(),
            question='What happened?',
            scope='journal',
            session_id='trace-1',
            turn_id=None,
            event_sequence=None,
            view_state={},
            max_context_chars=20_000,
        )
        response = TraceQAAgentResult(
            'Grounded answer.',
            'gpt-test',
            'Codex authentication',
            'Codex SDK',
            {'requests': 1},
            [],
            native_session_id='thread-1',
        )
        conversation_id = '00000000-0000-4000-8000-000000000003'
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            first = SelectableRepairAgents(state_dir, default_harness='codex-sdk')
            with (
                mock.patch.object(first, 'trace_qa_status', return_value={'available': True}),
                mock.patch.object(first, '_run_external_studio_turn', return_value=response),
            ):
                first.answer_trace(
                    prompt='FIRST FULL PROMPT',
                    context=context,
                    history=[],
                    settings=QASettings(api_key=''),
                    conversation_id=conversation_id,
                )

            restored = SelectableRepairAgents(state_dir, default_harness='codex-sdk')
            with (
                mock.patch.object(restored, 'trace_qa_status', return_value={'available': True}),
                mock.patch.object(restored, '_run_external_studio_turn', return_value=response) as resumed,
            ):
                restored.answer_trace(
                    prompt='SECOND FULL PROMPT',
                    context=context,
                    history=[{'question': 'old question', 'answer': 'old answer'}],
                    settings=QASettings(api_key=''),
                    conversation_id=conversation_id,
                )

        self.assertEqual(resumed.call_args.kwargs['native_session_id'], 'thread-1')
        self.assertNotIn('old question', resumed.call_args.args[0])

    def test_pydantic_conversation_restores_normalized_messages(self) -> None:
        context = TraceQAContext(
            result=mock.Mock(),
            question='What happened?',
            scope='journal',
            session_id='trace-1',
            turn_id=None,
            event_sequence=None,
            view_state={},
            max_context_chars=20_000,
        )
        response = AgentMessageResult(
            decision=AgentMessageDecision(action='answer', answer='It completed.'),
            model='test-model',
            provider='test-provider',
            harness='Pydantic AI',
            usage={'requests': 1},
            tools=[],
        )
        conversation_id = '00000000-0000-4000-8000-000000000003'
        settings = QASettings(api_key='test-key')
        activity = []
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            first = SelectableRepairAgents(state_dir, default_harness='pydantic')
            with mock.patch(
                'agent_trace_studio.harness_backend.run_pydantic_agent_controller',
                return_value=response,
            ):
                first.route_message(
                    prompt='FULL CONTROLLER PROMPT WITH DASHBOARD STATE',
                    context=context,
                    history=[{'question': 'Earlier question', 'answer': 'Earlier answer'}],
                    settings=settings,
                    conversation_id=conversation_id,
                    progress=activity.append,
                )

            restored = SelectableRepairAgents(state_dir, default_harness='pydantic')
            with mock.patch(
                'agent_trace_studio.harness_backend.run_pydantic_agent_controller',
                return_value=response,
            ) as resumed:
                restored.route_message(
                    prompt='NEXT FULL CONTROLLER PROMPT',
                    context=context,
                    history=[],
                    settings=settings,
                    conversation_id=conversation_id,
                )

        persisted = resumed.call_args.kwargs['message_history']
        serialized = ModelMessagesTypeAdapter.dump_json(persisted).decode()
        self.assertEqual(len(persisted), 4)
        self.assertIn('Earlier question', serialized)
        self.assertIn('What happened?', serialized)
        self.assertNotIn('FULL CONTROLLER PROMPT WITH DASHBOARD STATE', serialized)
        self.assertEqual([item.phase for item in activity], ['Turn', 'Usage', 'Response'])
        self.assertIn('Pydantic AI usage', activity[1].message)
        self.assertNotIn('completed', activity[1].message)
        self.assertEqual(activity[2].message, 'Pydantic AI returned the final response.')

    def test_parses_structured_checkpoint_summary_from_external_harness(self) -> None:
        result = _parse_checkpoint_summary(
            '```json\n'
            '{"title":"Session brief","summary":"Completed the work.",'
            '"objective":"Implement the feature.","outcome":"completed",'
            '"checkpoints":[{"title":"Verified","status":"completed",'
            '"summary":"Tests passed.","turn_ids":["turn-1"],'
            '"start_event_sequence":1,"end_event_sequence":9,"actions":["Ran tests."],'
            '"achievements":["Tests passed."],"blockers":[],"artifacts":[],"next_steps":[],'
            '"evidence_anchors":["[turn turn-1, line 9]"]}],'
            '"artifacts":[],"blockers":[],"next_steps":[]}\n'
            '```'
        )

        self.assertIsInstance(result, SessionCheckpointSummary)
        self.assertEqual(result.checkpoints[0].title, 'Verified')
        self.assertEqual(result.checkpoints[0].actions, ['Ran tests.'])

    def test_external_harness_corrects_checkpoint_answer_after_validation_failure(self) -> None:
        checkpoint = {
            'title': 'Published artifacts',
            'status': 'completed',
            'summary': 'Published the requested artifacts.',
            'turn_ids': ['turn-1'],
            'start_event_sequence': 1,
            'end_event_sequence': 9,
            'actions': [],
            'achievements': [],
            'blockers': [],
            'artifacts': [f'checkpoint-{index}' for index in range(12)],
            'next_steps': [],
            'evidence_anchors': ['[turn turn-1, line 9]'],
        }
        invalid = {
            'title': 'Session brief',
            'summary': 'Completed the work.',
            'objective': 'Implement the feature.',
            'outcome': 'completed',
            'checkpoints': [checkpoint],
            'artifacts': [f'artifact-{index}' for index in range(14)],
            'blockers': [],
            'next_steps': [],
        }
        corrected = {
            **invalid,
            'checkpoints': [{**checkpoint, 'artifacts': checkpoint['artifacts'][:10]}],
            'artifacts': invalid['artifacts'][:10],
        }
        responses = [
            TraceQAAgentResult(json.dumps(invalid), 'test-model', 'test-provider', 'Codex SDK', {'requests': 1}, []),
            TraceQAAgentResult(json.dumps(corrected), 'test-model', 'test-provider', 'Codex SDK', {'requests': 1}, []),
        ]
        progress = []
        with tempfile.TemporaryDirectory() as directory:
            registry = SelectableRepairAgents(Path(directory), default_harness='codex-sdk')
            with (
                mock.patch.object(registry, 'trace_qa_status', return_value={'available': True}),
                mock.patch.object(registry, '_run_checkpoint_turn', side_effect=responses) as run_turn,
            ):
                summary = registry.summarize_checkpoints(
                    'bounded evidence',
                    QASettings(api_key=''),
                    progress=progress.append,
                )

        self.assertEqual(len(summary.artifacts), 10)
        self.assertEqual(len(summary.checkpoints[0].artifacts), 10)
        self.assertEqual(run_turn.call_count, 2)
        correction_prompt = run_turn.call_args_list[1].args[0]
        self.assertIn('checkpoints.0.artifacts: List should have at most 10 items', correction_prompt)
        self.assertIn('artifacts: List should have at most 10 items', correction_prompt)
        self.assertIn('Each checkpoint may contain at most', correction_prompt)
        self.assertNotIn('<session_evidence>', correction_prompt)
        self.assertTrue(any('requesting correction 1/2' in item.message for item in progress))

    def test_external_harness_checkpoint_corrections_are_bounded(self) -> None:
        invalid = json.dumps(
            {
                'title': 'Session brief',
                'summary': 'Completed the work.',
                'objective': 'Implement the feature.',
                'outcome': 'completed',
                'checkpoints': [],
                'artifacts': [f'artifact-{index}' for index in range(11)],
                'blockers': [],
                'next_steps': [],
            }
        )
        response = TraceQAAgentResult(invalid, 'test-model', 'test-provider', 'OpenCode', {'requests': 1}, [])
        with tempfile.TemporaryDirectory() as directory:
            registry = SelectableRepairAgents(Path(directory))
            with (
                mock.patch.object(registry, 'trace_qa_status', return_value={'available': True}),
                mock.patch.object(registry, '_run_checkpoint_turn', return_value=response) as run_turn,
                self.assertRaisesRegex(QAUnavailableError, 'artifacts: List should have at most 10 items'),
            ):
                registry.summarize_checkpoints('bounded evidence', QASettings(api_key=''))

        self.assertEqual(run_turn.call_count, 3)

    def test_external_checkpoint_plan_retrieves_only_normalized_bounded_evidence(self) -> None:
        queries, turn_ids = _parse_checkpoint_retrieval_plan(
            'Planning follows. {"queries":["exec_command result"],"turn_ids":["turn-2"]}'
        )
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'session.jsonl'
            write_journal(journal)
            result = analyze_journals([journal], include_trace=True)
            context = TraceQAContext(
                result=result,
                question='Update the complete session checkpoint brief.',
                scope='session',
                session_id='session-1',
                turn_id=None,
                event_sequence=None,
                view_state={},
                max_context_chars=12_000,
            )
            evidence = _retrieve_checkpoint_evidence(
                context,
                queries=queries,
                turn_ids=turn_ids,
                max_chars=8_000,
            )

        self.assertIn('AGENT-DIRECTED NORMALIZED TRACE RETRIEVAL', evidence)
        self.assertIn('TRACE SEARCH RESULT', evidence)
        self.assertIn('TRACE TURN RESULT', evidence)
        self.assertNotIn(str(journal), evidence)
        self.assertNotIn('TOP_SECRET_ENCRYPTED_REASONING', evidence)

    def test_worker_pools_reuse_one_live_worker_per_conversation(self) -> None:
        codex_worker = mock.Mock(alive=True)
        opencode_worker = mock.Mock(alive=True)
        key = ('conversation-1', 'trace-1')
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                'agent_trace_studio.harness_backend.CodexThreadWorker',
                return_value=codex_worker,
            ) as codex_factory,
            mock.patch(
                'agent_trace_studio.harness_backend.OpenCodeServerWorker',
                return_value=opencode_worker,
            ) as opencode_factory,
        ):
            registry = SelectableRepairAgents(Path(directory), default_harness='codex-sdk')
            first_codex = registry._codex_thread_worker(key, thread_id='thread-1')
            second_codex = registry._codex_thread_worker(key, thread_id='thread-1')
            first_opencode = registry._opencode_server(key)
            second_opencode = registry._opencode_server(key)
            registry.close()

        self.assertIs(first_codex, second_codex)
        self.assertIs(first_opencode, second_opencode)
        codex_factory.assert_called_once()
        opencode_factory.assert_called_once()
        codex_worker.close.assert_called_once()
        opencode_worker.close.assert_called_once()

    def test_clear_conversation_closes_its_live_harness_workers(self) -> None:
        key = ('conversation-1', 'trace-1')
        with tempfile.TemporaryDirectory() as directory:
            registry = SelectableRepairAgents(Path(directory), default_harness='codex-sdk')
            codex_worker = mock.Mock()
            opencode_worker = mock.Mock()
            registry._codex_thread_workers[key] = codex_worker
            registry._opencode_server_workers[key] = opencode_worker

            registry.clear_conversation(conversation_id=key[0], session_id=key[1])

        codex_worker.close.assert_called_once()
        opencode_worker.close.assert_called_once()


class OpenCodeFixerSessionTests(unittest.TestCase):
    def test_persists_session_id_without_forwarding_api_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            binary = root / 'opencode'
            binary.write_text('', encoding='utf-8')
            state_dir = root / 'state'
            audit = ParserAudit(summary='Fix the parser.', requires_fix=True, confidence='high')
            outputs = [
                subprocess.CompletedProcess(
                    (),
                    0,
                    stdout='{"sessionID":"session-1"}\n{"text":"first repair complete"}\n',
                    stderr='',
                ),
                subprocess.CompletedProcess(
                    (),
                    0,
                    stdout='{"sessionID":"session-1"}\n{"text":"second repair complete"}\n',
                    stderr='',
                ),
            ]

            with (
                mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'must-not-leak'}),
                mock.patch('agent_trace_studio.harness_backend.subprocess.run', side_effect=outputs) as run,
            ):
                first = OpenCodeFixerSession(workspace, state_dir=state_dir, binary=binary)
                self.assertEqual(first.fix(attempt=1, audit=audit, feedback=None), 'first repair complete')
                second = OpenCodeFixerSession(workspace, state_dir=state_dir, binary=binary)
                self.assertEqual(
                    second.fix(attempt=2, audit=audit, feedback={'reason': 'verification failed'}),
                    'second repair complete',
                )

            first_command = run.call_args_list[0].args[0]
            second_command = run.call_args_list[1].args[0]
            self.assertNotIn('--session', first_command)
            self.assertEqual(second_command[second_command.index('--session') + 1], 'session-1')
            self.assertNotIn('OPENAI_API_KEY', run.call_args_list[0].kwargs['env'])
            self.assertEqual(json.loads((state_dir / 'opencode-session.json').read_text())['session_id'], 'session-1')


class TraceQAHarnessTests(unittest.TestCase):
    def test_studio_stop_terminates_an_active_harness_process(self) -> None:
        cancel_event = threading.Event()
        timer = threading.Timer(0.1, cancel_event.set)
        started = time.monotonic()
        timer.start()
        try:
            with (
                tempfile.TemporaryDirectory() as directory,
                self.assertRaisesRegex(StudioAgentCancelled, 'stopped'),
            ):
                _run_studio_subprocess(
                    [sys.executable, '-c', 'import time; time.sleep(30)'],
                    cwd=Path(directory),
                    env=dict(os.environ),
                    cancel_event=cancel_event,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)

    def test_opencode_qa_uses_read_only_attachment_and_harness_auth(self) -> None:
        captured: dict[str, object] = {}

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            context_path = Path(command[command.index('--file') + 1])
            captured['prompt'] = context_path.read_text(encoding='utf-8')
            captured['command'] = command
            captured['environment'] = kwargs['env']
            return subprocess.CompletedProcess(command, 0, stdout='{"text":"grounded answer"}\n', stderr='')

        with (
            mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'must-not-leak'}),
            mock.patch('agent_trace_studio.harness_backend.subprocess.run', side_effect=run),
        ):
            result = _run_opencode_trace_qa(
                'CURRENT DASHBOARD REQUEST\nQuestion: what happened?',
                QASettings(api_key='', model='test-model'),
            )

        command = captured['command']
        environment = captured['environment']
        self.assertEqual(result.answer, 'grounded answer')
        self.assertEqual(result.harness, 'OpenCode')
        self.assertIn('CURRENT DASHBOARD REQUEST', captured['prompt'])
        self.assertNotIn('--auto', command)
        self.assertNotIn('OPENAI_API_KEY', environment)
        config = json.loads(environment['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(config['permission']['edit'], 'deny')
        self.assertEqual(config['permission']['bash'], 'deny')

    def test_opencode_controller_exposes_portable_skills_and_parses_decision(self) -> None:
        captured: dict[str, object] = {}

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            workspace = Path(kwargs['cwd'])
            captured['command'] = command
            captured['skills'] = sorted(path.parent.name for path in workspace.glob('.agents/skills/*/SKILL.md'))
            captured['prompt'] = Path(command[command.index('--file') + 1]).read_text(encoding='utf-8')
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"text":"{\\"action\\":\\"repair_parser\\",\\"answer\\":\\"Starting.\\"}"}\n',
                stderr='',
            )

        with mock.patch('agent_trace_studio.harness_backend.subprocess.run', side_effect=run):
            result = _run_opencode_controller(
                'CURRENT CONTROLLER REQUEST',
                QASettings(api_key=''),
                session_id='session-existing',
            )

        decision = parse_agent_message_decision(result.answer)
        self.assertEqual(decision.action, 'repair_parser')
        self.assertIn('parser-repair', captured['skills'])
        self.assertIn('trace-qa', captured['skills'])
        self.assertIn('CURRENT CONTROLLER REQUEST', captured['prompt'])
        self.assertEqual(captured['command'][captured['command'].index('--session') + 1], 'session-existing')
        self.assertEqual(result.native_session_id, 'session-existing')

    def test_opencode_controller_calls_the_live_session_server_directly(self) -> None:
        server = mock.Mock()
        server.run.return_value = {
            'answer': '{"action":"answer","answer":"Done."}',
            'session_id': 'session-live',
        }

        result = _run_opencode_controller(
            'CURRENT CONTROLLER REQUEST',
            QASettings(api_key='', model='gpt-test'),
            session_id='session-live',
            server=server,
        )

        server.run.assert_called_once()
        self.assertEqual(server.run.call_args.args[0], 'CURRENT CONTROLLER REQUEST')
        self.assertEqual(server.run.call_args.kwargs['model'], 'openai/gpt-test')
        self.assertEqual(server.run.call_args.kwargs['session_id'], 'session-live')
        self.assertEqual(result.native_session_id, 'session-live')

    def test_codex_qa_requests_read_only_sandbox_without_api_keys(self) -> None:
        completed = subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps({'finalResponse': 'codex grounded answer', 'threadId': 'thread-1'}),
            stderr='',
        )
        with (
            mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'must-not-leak'}),
            mock.patch('agent_trace_studio.harness_backend.subprocess.run', return_value=completed) as run,
        ):
            result = _run_codex_trace_qa('bounded trace prompt')

        request = json.loads(run.call_args.kwargs['input'])
        self.assertTrue(request['readOnly'])
        self.assertIsNone(request['threadId'])
        self.assertNotIn('OPENAI_API_KEY', run.call_args.kwargs['env'])
        self.assertEqual(result.answer, 'codex grounded answer')
        self.assertEqual(result.harness, 'Codex SDK')

    def test_codex_qa_resumes_the_persisted_thread(self) -> None:
        completed = subprocess.CompletedProcess(
            (),
            0,
            stdout=json.dumps({'finalResponse': 'continued answer', 'threadId': 'thread-1'}),
            stderr='',
        )
        with mock.patch(
            'agent_trace_studio.harness_backend.subprocess.run',
            return_value=completed,
        ) as run:
            result = _run_codex_trace_qa('follow-up', thread_id='thread-1')

        request = json.loads(run.call_args.kwargs['input'])
        self.assertEqual(request['threadId'], 'thread-1')
        self.assertEqual(result.native_session_id, 'thread-1')


if __name__ == '__main__':
    unittest.main()
