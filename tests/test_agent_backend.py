from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pydantic_ai import Agent, AgentRunResultEvent
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.test import TestModel

from agent_trace_studio.agent_backend import (
    AgentRunOutcome,
    MemoryExtraction,
    ParserAudit,
    PydanticFixerSession,
    PydanticRepairAgents,
    SessionCheckpointSummary,
    SessionInvestigation,
    VerificationVerdict,
    _model_for_settings,
    _progress_for_event,
    _run_agent,
    _run_agent_with_history,
)
from agent_trace_studio.parser import analyze_journals
from agent_trace_studio.qa import QASettings, TraceQAContext
from helpers import write_journal


class AgentBackendTest(unittest.TestCase):
    def test_trace_analysis_roles_return_structured_results(self) -> None:
        agents = PydanticRepairAgents()
        settings = QASettings(api_key='test-key', model='test-model')
        with mock.patch(
            'agent_trace_studio.agent_backend._model_for_settings',
            return_value=TestModel(
                custom_output_args={
                    'title': 'Synthetic investigation',
                    'summary': 'A grounded summary.',
                    'objective': 'Test the workflow.',
                    'outcome': 'completed',
                    'key_actions': [],
                    'findings': [],
                    'issues': [],
                    'lessons': [],
                    'evidence': [],
                }
            ),
        ):
            investigation = agents.investigate('bounded evidence', settings)
        with mock.patch(
            'agent_trace_studio.agent_backend._model_for_settings',
            return_value=TestModel(
                custom_output_args={
                    'summary': 'One reusable lesson.',
                    'candidates': [
                        {
                            'category': 'workflow',
                            'title': 'Bound context',
                            'memory': 'Keep trace context bounded.',
                            'why_reusable': 'It limits irrelevant evidence.',
                            'confidence': 'high',
                            'evidence_anchors': ['[turn turn-1, line 3]'],
                        }
                    ],
                }
            ),
        ):
            memories = agents.extract_memories('bounded evidence', settings)
        with mock.patch(
            'agent_trace_studio.agent_backend._model_for_settings',
            return_value=TestModel(
                custom_output_args={
                    'title': 'Synthetic session brief',
                    'summary': 'The session completed one bounded workflow.',
                    'objective': 'Test checkpoint extraction.',
                    'outcome': 'completed',
                    'checkpoints': [
                        {
                            'title': 'Validated the trace',
                            'status': 'completed',
                            'summary': 'The synthetic trace was inspected.',
                            'turn_ids': ['turn-1'],
                            'start_event_sequence': 1,
                            'end_event_sequence': 4,
                            'actions': ['Read the normalized session evidence.'],
                            'achievements': ['Confirmed the selected event.'],
                            'blockers': [],
                            'artifacts': ['Checkpoint report'],
                            'next_steps': [],
                            'evidence_anchors': ['[turn turn-1, line 3]'],
                        }
                    ],
                    'artifacts': [],
                    'blockers': [],
                    'next_steps': [],
                }
            ),
        ):
            checkpoints = agents.summarize_checkpoints('bounded session evidence', settings)

        self.assertIsInstance(investigation, SessionInvestigation)
        self.assertEqual(investigation.outcome, 'completed')
        self.assertIsInstance(memories, MemoryExtraction)
        self.assertEqual(memories.candidates[0].category, 'workflow')
        self.assertIsInstance(checkpoints, SessionCheckpointSummary)
        self.assertEqual(checkpoints.checkpoints[0].status, 'completed')
        self.assertEqual(checkpoints.checkpoints[0].achievements, ['Confirmed the selected event.'])

    def test_run_agent_streams_safe_progress_and_returns_structured_output(self) -> None:
        progress = []
        agent = Agent(
            TestModel(
                custom_output_args={
                    'summary': 'No parser defect found.',
                    'requires_fix': False,
                    'confidence': 'high',
                    'issues': [],
                }
            ),
            output_type=ParserAudit,
        )

        result = _run_agent(agent, 'Audit the parser.', role='audit', progress=progress.append)

        self.assertIsInstance(result, ParserAudit)
        self.assertEqual(progress[0].message, 'Sending the audit request.')
        self.assertEqual(progress[-1].message, 'Audit response received.')

    def test_checkpoint_agent_can_direct_bounded_trace_retrieval(self) -> None:
        output = {
            'title': 'Retrieved session brief',
            'summary': 'The agent inspected additional normalized evidence.',
            'objective': 'Verify agent-directed summary retrieval.',
            'outcome': 'completed',
            'checkpoints': [],
            'artifacts': [],
            'blockers': [],
            'next_steps': [],
        }
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
            with mock.patch(
                'agent_trace_studio.agent_backend._model_for_settings',
                return_value=TestModel(call_tools=['search_trace'], custom_output_args=output),
            ):
                summary = PydanticRepairAgents().summarize_checkpoints(
                    'sampled chronological seed',
                    QASettings(api_key='test-key', model='test-model'),
                    context=context,
                )

        self.assertEqual(summary.title, 'Retrieved session brief')

    def test_progress_events_do_not_expose_tool_names_arguments_or_model_text(self) -> None:
        event = SimpleNamespace(
            event_kind='function_tool_call',
            part=SimpleNamespace(
                tool_name='TOP_SECRET_DYNAMIC_TOOL',
                args={'api_key': 'TOP_SECRET_ARGUMENT'},
                content='TOP_SECRET_MODEL_TEXT',
            ),
        )

        progress = _progress_for_event(event, role='audit')

        self.assertIsNotNone(progress)
        serialized = f'{progress.phase} {progress.message}'
        self.assertNotIn('TOP_SECRET', serialized)
        self.assertEqual(progress.message, 'Inspecting the isolated workspace with a read-only tool.')

    def test_transport_timeout_retries_without_consuming_a_repair_attempt(self) -> None:
        progress = []
        agent = _RetryAgent()
        with mock.patch('agent_trace_studio.agent_backend.asyncio.sleep', new=mock.AsyncMock()):
            outcome = _run_agent_with_history(
                agent,
                'continue repair',
                role='repair',
                progress=progress.append,
                conversation_id='logical-session',
            )

        self.assertEqual(outcome.output, 'completed after retry')
        self.assertEqual(outcome.transport_retries, 1)
        self.assertEqual(agent.prompts, ['continue repair', 'continue repair'])
        self.assertIn('retry 1 of 2', ' '.join(item.message for item in progress))

    def test_fixer_session_preserves_history_across_verification_cycles(self) -> None:
        audit = ParserAudit(summary='Synthetic issue.', requires_fix=True, confidence='high', issues=[])
        first_message = ModelRequest.user_text_prompt('first repair turn')
        second_message = ModelRequest.user_text_prompt('second repair turn')
        outcomes = [
            AgentRunOutcome('First repair.', [first_message], 0),
            AgentRunOutcome('Second repair.', [first_message, second_message], 0),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            (workspace / 'AGENTS.md').write_text('# Synthetic\n', encoding='utf-8')
            with (
                mock.patch('agent_trace_studio.agent_backend._model_for_settings', return_value=TestModel()),
                mock.patch(
                    'agent_trace_studio.agent_backend._run_agent_with_history',
                    side_effect=outcomes,
                ) as run_agent,
            ):
                session = PydanticFixerSession(
                    workspace,
                    QASettings(api_key='test-key', model='test-model'),
                    run_id='logical-session',
                    state_dir=root / 'state',
                )
                first = session.fix(attempt=1, audit=audit, feedback=None)
                session = PydanticFixerSession(
                    workspace,
                    QASettings(api_key='test-key', model='test-model'),
                    run_id='logical-session',
                    state_dir=root / 'state',
                )
                second = session.fix(
                    attempt=2,
                    audit=audit,
                    feedback={'reason': 'verification_failed'},
                )

            self.assertEqual(first, 'First repair.')
            self.assertEqual(second, 'Second repair.')
            self.assertEqual(run_agent.call_args_list[0].kwargs['message_history'], [])
            self.assertEqual(run_agent.call_args_list[1].kwargs['message_history'], [first_message])
            self.assertEqual(
                {call.kwargs['conversation_id'] for call in run_agent.call_args_list},
                {'logical-session'},
            )
            persisted = (root / 'state/fixer-messages.json').read_bytes()
            self.assertIn(b'second repair turn', persisted)

    def test_verifier_receives_complete_host_confirmed_artifacts_when_hidden_paths_are_unavailable(self) -> None:
        agents = PydanticRepairAgents()
        settings = QASettings(api_key='test-key', model='test-model')
        verdict = VerificationVerdict(status='pass', summary='Synthetic artifacts verified.')
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            control = workspace / '.agent-trace-studio'
            control.mkdir()
            contents = {
                'audit-report.json': '{"requires_fix":true}',
                'change.patch': 'synthetic patch',
                'verification.json': '{"deterministic_gates_passed":true}',
            }
            for name, content in contents.items():
                (control / name).write_text(content, encoding='utf-8')
            (control / 'verification-feedback.json').write_text('SENSITIVE_NON_ARTIFACT', encoding='utf-8')

            with (
                mock.patch(
                    'agent_trace_studio.agent_backend._model_for_settings',
                    return_value=TestModel(),
                ),
                mock.patch('agent_trace_studio.agent_backend._run_agent', return_value=verdict) as run_agent,
            ):
                result = agents.verify(workspace, settings)

        self.assertIs(result, verdict)
        prompt = run_agent.call_args.args[1]
        self.assertIn('complete host-confirmed artifact snapshot', prompt)
        for name, content in contents.items():
            self.assertIn(f'.agent-trace-studio/{name}', prompt)
            self.assertIn(json.dumps(content), prompt)
        self.assertNotIn('SENSITIVE_NON_ARTIFACT', prompt)

    def test_verifier_rejects_an_incomplete_artifact_set_before_model_request(self) -> None:
        agents = PydanticRepairAgents()
        settings = QASettings(api_key='test-key', model='test-model')
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / '.agent-trace-studio').mkdir()
            with self.assertRaisesRegex(RuntimeError, 'missing verification artifact:'):
                agents.verify(workspace, settings)

    def test_builds_provider_native_models_from_memory_only_settings(self) -> None:
        cases = (
            (
                QASettings(
                    api_key='openai-test-key',
                    provider='openai',
                    model='test-openai',
                    base_url='https://api.openai.com/v1',
                ),
                OpenAIResponsesModel,
                'https://api.openai.com/v1/',
            ),
            (
                QASettings(
                    api_key='anthropic-test-key',
                    provider='anthropic',
                    model='test-anthropic',
                    base_url='https://api.anthropic.com/v1',
                ),
                AnthropicModel,
                'https://api.anthropic.com',
            ),
            (
                QASettings(
                    api_key='google-test-key',
                    provider='google',
                    model='models/test-google',
                    base_url='https://generativelanguage.googleapis.com/v1beta',
                ),
                GoogleModel,
                'https://generativelanguage.googleapis.com',
            ),
        )

        for settings, expected_type, expected_base_url in cases:
            with self.subTest(provider=settings.provider):
                model = _model_for_settings(settings)
                self.assertIsInstance(model, expected_type)
                self.assertEqual(str(model.provider.base_url), expected_base_url)


class ReadTimeout(Exception):
    pass


class _FakeResult:
    output = 'completed after retry'

    @staticmethod
    def all_messages() -> list[ModelRequest]:
        return []


class _EventStream:
    def __aiter__(self):
        async def iterate():
            yield AgentRunResultEvent(_FakeResult())

        return iterate()


class _RetryAgent:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str | None] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def run_stream_events(self, prompt, **_kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        call = self.calls

        @asynccontextmanager
        async def events():
            if call == 1:
                raise ReadTimeout()
            yield _EventStream()

        return events()


if __name__ == '__main__':
    unittest.main()
