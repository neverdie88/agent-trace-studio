from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.test import TestModel

from agent_trace_studio.audit_rules import AuditRuleDraft, AuditRuleProposalContent
from agent_trace_studio.credentials import APIConfigStore, CredentialPersistenceError
from agent_trace_studio.models import TraceEvent
from agent_trace_studio.parser import analyze_journals
from agent_trace_studio.qa import (
    AgentMessageDecision,
    AgentMessageResult,
    AuditRuleAgentResult,
    DashboardResourceAccess,
    JournalQA,
    QASettings,
    QAUnavailableError,
    TraceQAAgentResult,
    TraceQAContext,
    _pydantic_event_stream_handler,
    _qa_message_history,
    _qa_model_for_settings,
    _select_qa_events,
    build_agent_workflow_context,
    build_qa_context,
    build_session_checkpoint_context,
    build_studio_state_context,
    inspect_current_context,
    list_dashboard_resources,
    parse_audit_rule_proposal,
    parse_trace_context_plan,
    qa_provider_catalog,
    read_dashboard_resource,
    read_trace_turn,
    search_trace,
)
from helpers import write_journal


class _FakeSecretStore:
    available = True
    label = 'Test credential store'

    def __init__(self) -> None:
        self.value: str | None = None

    def get(self) -> str | None:
        return self.value

    def set(self, value: str) -> None:
        self.value = value

    def delete(self) -> None:
        self.value = None


class _UnavailableSecretStore(_FakeSecretStore):
    available = False
    label = 'No native credential store'


class _FakeTraceAgentBackend:
    def trace_qa_status(self, settings: QASettings) -> dict[str, object]:
        return {
            'id': 'opencode',
            'label': 'OpenCode',
            'available': True,
            'detail': 'Ready for test.',
            'model': 'openai/test-model',
            'uses_api_settings': False,
        }

    def answer_trace(self, **kwargs: object) -> TraceQAAgentResult:
        self.answer_kwargs = kwargs
        return TraceQAAgentResult(
            answer='OpenCode grounded answer [turn turn-1, line 3]',
            model='openai/test-model',
            provider='OpenCode credential store',
            harness='OpenCode',
            usage={'requests': 1},
            tools=[],
        )

    def route_message(self, **kwargs: object) -> AgentMessageResult:
        self.route_kwargs = kwargs
        return AgentMessageResult(
            decision=AgentMessageDecision(
                action='answer',
                answer='OpenCode controller answer [turn turn-1, line 3]',
            ),
            model='openai/test-model',
            provider='OpenCode credential store',
            harness='OpenCode',
            usage={'requests': 1},
            tools=['skill'],
        )

    def propose_audit_rule(self, **kwargs: object) -> AuditRuleAgentResult:
        self.rule_kwargs = kwargs
        return AuditRuleAgentResult(
            content=AuditRuleProposalContent(
                summary='Drafted a tool-order rule.',
                rule=AuditRuleDraft.model_validate(
                    {
                        'id': 'validate-after-edit',
                        'title': 'Validate after edits',
                        'expectation': 'Validation follows source edits.',
                        'severity': 'high',
                        'rule_type': 'require_after',
                        'event': {'tool_name': 'apply_patch'},
                        'required_event': {'tool_name': 'exec_command'},
                    }
                ),
            ),
            model='openai/test-model',
            provider='OpenCode credential store',
            harness='OpenCode',
            usage={'requests': 1},
            tools=[],
        )


class JournalQATest(unittest.TestCase):
    def test_public_status_never_exposes_api_key(self) -> None:
        settings = QASettings(
            api_key='sk-test-secret',
            model='test-model',
            base_url='https://example.test/v1',
        )

        status = settings.public_status()

        self.assertTrue(status['configured'])
        self.assertEqual(status['provider_id'], 'openai')
        self.assertEqual(status['base_url'], 'https://example.test/v1')
        self.assertEqual({provider['id'] for provider in status['providers']}, {'openai', 'anthropic', 'google'})
        qa_status = JournalQA(settings).public_status()
        self.assertTrue(qa_status['agent_enabled'])
        self.assertEqual(qa_status['agent_harness'], 'Pydantic AI')
        self.assertNotIn('api_key', status)
        self.assertNotIn('sk-test-secret', json.dumps(status))

    def test_configures_and_clears_process_only_api_settings(self) -> None:
        qa = JournalQA(QASettings(api_key=''))

        status = qa.configure(
            api_key=' sk-test-secret ',
            provider='openai',
            model=' test-model ',
            base_url='https://example.test/v1/',
        )

        self.assertTrue(status['configured'])
        self.assertEqual(qa.settings.api_key, 'sk-test-secret')
        self.assertEqual(qa.settings.model, 'test-model')
        self.assertEqual(qa.settings.base_url, 'https://example.test/v1')

        qa.configure(
            api_key=None,
            provider='openai',
            model='second-model',
            base_url='http://localhost:8080/v1/',
        )
        self.assertEqual(qa.settings.api_key, 'sk-test-secret')
        self.assertEqual(qa.settings.model, 'second-model')
        self.assertEqual(qa.settings.base_url, 'http://localhost:8080/v1')

        cleared = qa.clear_api_key()
        self.assertFalse(cleared['configured'])
        self.assertEqual(qa.settings.api_key, '')

    def test_remembers_api_settings_without_writing_key_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / 'model-api.json'
            secrets = _FakeSecretStore()
            store = APIConfigStore(config_path, secret_store=secrets)
            qa = JournalQA(QASettings(api_key=''), config_store=store)

            status = qa.configure(
                api_key='persisted-test-secret',
                provider='anthropic',
                model='claude-sonnet-5',
                base_url='https://api.anthropic.com/v1',
                remember=True,
            )

            self.assertTrue(status['configured'])
            self.assertTrue(status['remembered'])
            self.assertEqual(status['credential_store'], 'Test credential store')
            self.assertEqual(secrets.value, 'persisted-test-secret')
            self.assertNotIn('persisted-test-secret', config_path.read_text(encoding='utf-8'))

            restored = JournalQA(QASettings(api_key=''), config_store=store)
            restored_status = restored.public_status()
            self.assertTrue(restored_status['configured'])
            self.assertTrue(restored_status['remembered'])
            self.assertEqual(restored.settings.provider, 'anthropic')
            self.assertEqual(restored.settings.api_key, 'persisted-test-secret')
            self.assertNotIn('persisted-test-secret', json.dumps(restored_status))

            cleared = restored.clear_api_key()
            self.assertFalse(cleared['configured'])
            self.assertFalse(cleared['remembered'])
            self.assertIsNone(secrets.value)
            self.assertFalse(config_path.exists())

    def test_encrypted_vault_survives_restart_and_requires_unlock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / 'model-api.json'
            password = 'correct horse battery staple'
            store = APIConfigStore(config_path, secret_store=_UnavailableSecretStore())
            qa = JournalQA(QASettings(api_key=''), config_store=store)

            status = qa.configure(
                api_key='encrypted-test-secret',
                provider='google',
                model='gemini-3.6-flash',
                base_url='https://generativelanguage.googleapis.com/v1beta',
                remember=True,
                vault_password=password,
            )

            saved_text = config_path.read_text(encoding='utf-8')
            self.assertTrue(status['configured'])
            self.assertTrue(status['remembered'])
            self.assertEqual(status['credential_mode'], 'encrypted_vault')
            self.assertEqual(status['credential_store'], 'encrypted local vault')
            self.assertNotIn('encrypted-test-secret', saved_text)
            self.assertNotIn(password, saved_text)

            restarted = JournalQA(
                QASettings(api_key=''),
                config_store=APIConfigStore(config_path, secret_store=_UnavailableSecretStore()),
            )
            restarted_status = restarted.public_status()
            self.assertFalse(restarted_status['configured'])
            self.assertTrue(restarted_status['remembered'])
            self.assertTrue(restarted_status['credential_locked'])
            self.assertTrue(restarted_status['vault_password_required'])

            with self.assertRaisesRegex(CredentialPersistenceError, 'incorrect'):
                restarted.unlock(vault_password='this password is wrong')

            unlocked = restarted.unlock(vault_password=password)
            self.assertTrue(unlocked['configured'])
            self.assertFalse(unlocked['credential_locked'])
            self.assertEqual(restarted.settings.api_key, 'encrypted-test-secret')
            self.assertNotIn('encrypted-test-secret', json.dumps(unlocked))
            self.assertNotIn(password, json.dumps(unlocked))

    def test_environment_password_auto_unlocks_encrypted_vault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / 'model-api.json'
            password = 'headless environment vault password'
            qa = JournalQA(
                QASettings(api_key=''),
                config_store=APIConfigStore(config_path, secret_store=_UnavailableSecretStore()),
            )
            qa.configure(
                api_key='headless-test-secret',
                provider='openai',
                model='test-model',
                base_url='https://example.test/v1',
                remember=True,
                vault_password=password,
            )

            with mock.patch.dict(os.environ, {'AGENT_TRACE_STUDIO_VAULT_PASSWORD': password}):
                restarted = JournalQA(
                    QASettings(api_key=''),
                    config_store=APIConfigStore(config_path, secret_store=_UnavailableSecretStore()),
                )

            status = restarted.public_status()
            self.assertTrue(status['configured'])
            self.assertTrue(status['vault_password_from_environment'])
            self.assertEqual(restarted.settings.api_key, 'headless-test-secret')

    def test_encrypted_vault_detects_metadata_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / 'model-api.json'
            password = 'metadata authentication password'
            qa = JournalQA(
                QASettings(api_key=''),
                config_store=APIConfigStore(config_path, secret_store=_UnavailableSecretStore()),
            )
            qa.configure(
                api_key='tamper-test-secret',
                provider='openai',
                model='test-model',
                base_url='https://example.test/v1',
                remember=True,
                vault_password=password,
            )
            metadata = json.loads(config_path.read_text(encoding='utf-8'))
            metadata['base_url'] = 'https://different.example.test/v1'
            config_path.write_text(json.dumps(metadata), encoding='utf-8')

            restarted = JournalQA(
                QASettings(api_key=''),
                config_store=APIConfigStore(config_path, secret_store=_UnavailableSecretStore()),
            )
            with self.assertRaisesRegex(CredentialPersistenceError, 'damaged'):
                restarted.unlock(vault_password=password)

    def test_loads_and_upgrades_version_one_native_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / 'model-api.json'
            config_path.write_text(
                json.dumps(
                    {
                        'version': 1,
                        'provider': 'openai',
                        'model': 'legacy-model',
                        'base_url': 'https://example.test/v1',
                    }
                ),
                encoding='utf-8',
            )
            secrets = _FakeSecretStore()
            secrets.value = 'legacy-native-secret'
            qa = JournalQA(QASettings(api_key=''), config_store=APIConfigStore(config_path, secret_store=secrets))

            self.assertTrue(qa.public_status()['configured'])
            qa.configure(
                api_key=None,
                provider='openai',
                model='upgraded-model',
                base_url='https://example.test/v1',
                remember=True,
            )

            upgraded = json.loads(config_path.read_text(encoding='utf-8'))
            self.assertEqual(upgraded['version'], 2)
            self.assertEqual(upgraded['credential'], {'kind': 'system'})

    def test_encrypted_vault_rejects_short_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            qa = JournalQA(
                QASettings(api_key=''),
                config_store=APIConfigStore(Path(directory) / 'model-api.json', secret_store=_UnavailableSecretStore()),
            )

            with self.assertRaisesRegex(CredentialPersistenceError, 'at least 12'):
                qa.configure(
                    api_key='test-secret',
                    provider='openai',
                    model='test-model',
                    base_url='https://example.test/v1',
                    remember=True,
                    vault_password='too-short',
                )

    def test_provider_switch_requires_a_new_key(self) -> None:
        qa = JournalQA(QASettings(api_key='openai-secret'))

        with self.assertRaisesRegex(ValueError, 'api_key is required'):
            qa.configure(
                api_key=None,
                provider='anthropic',
                model='claude-sonnet-5',
                base_url='https://api.anthropic.com/v1',
            )

        status = qa.configure(
            api_key='anthropic-secret',
            provider='anthropic',
            model='claude-sonnet-5',
            base_url='https://api.anthropic.com/v1',
        )
        self.assertEqual(status['provider_id'], 'anthropic')
        self.assertNotIn('anthropic-secret', json.dumps(status))

    def test_rejects_insecure_remote_api_endpoint(self) -> None:
        qa = JournalQA(QASettings(api_key=''))

        with self.assertRaisesRegex(ValueError, 'must use HTTPS'):
            qa.configure(
                api_key='sk-test-secret',
                provider='openai',
                model='test-model',
                base_url='http://example.test/v1',
            )

    def test_rejects_api_endpoint_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, 'must not include credentials'):
            QASettings(api_key='sk-test-secret', base_url='https://user:password@example.test/v1')

    def test_builds_bounded_context_with_turn_and_line_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)

            context = build_qa_context(
                result,
                question='What did exec_command return?',
                scope='journal',
                session_id='session-1',
                turn_id=None,
                max_chars=20_000,
            )

        self.assertIn('JOURNAL SUMMARY', context)
        self.assertIn('source_name: session.jsonl', context)
        self.assertIn('tool=exec_command', context)
        self.assertRegex(context, r'\[turn turn-1, line \d+\]')
        self.assertNotIn(str(path), context)
        self.assertNotIn('TOP_SECRET_ENCRYPTED_REASONING', context)

    def test_studio_state_exposes_resource_and_selection_without_preloading_trace_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)

            context = build_studio_state_context(
                result,
                session_id='session-1',
                turn_id='turn-1',
                event_sequence=3,
                view_state={'category': 'tool', 'session_brief_summary': 'Saved derived context.'},
            )

        self.assertIn('STUDIO SESSION RESOURCE', context)
        self.assertIn('journal_evidence_preloaded: false', context)
        self.assertIn('selected_turn: turn-1', context)
        self.assertIn('PERSISTED SESSION BRIEF\navailable: true', context)
        self.assertNotIn('Saved derived context.', context)
        self.assertNotIn('TOP_SECRET_TOOL_OUTPUT', context)

    def test_parses_strict_agent_selected_context_actions(self) -> None:
        plan = parse_trace_context_plan(
            '{"context_requests":[{"tool":"search_trace","query":"deployment approval","limit":3}]}'
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.context_requests[0].tool, 'search_trace')
        self.assertEqual(plan.context_requests[0].query, 'deployment approval')
        self.assertIsNone(parse_trace_context_plan('{"action":"answer","answer":"No lookup needed."}'))
        with self.assertRaisesRegex(QAUnavailableError, 'invalid context action'):
            parse_trace_context_plan('{"context_requests":[{"tool":"write_file"}]}')

    def test_turn_scope_excludes_other_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)

            context = build_qa_context(
                result,
                question='Summarize this turn',
                scope='turn',
                session_id='session-1',
                turn_id='turn-1',
                max_chars=20_000,
            )

        self.assertIn('[turn turn-1,', context)
        self.assertNotIn('[turn turn-2,', context)

    def test_journal_scope_leads_with_current_dashboard_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)
            selected = next(event for event in result.traces[0].events if event.turn_id == 'turn-2')

            context = build_qa_context(
                result,
                question='What is it currently doing?',
                scope='journal',
                session_id='session-1',
                turn_id='turn-2',
                event_sequence=selected.sequence,
                view_state={'category': 'tool', 'tool': 'exec_command'},
                max_chars=20_000,
            )

        self.assertIn('CURRENT DASHBOARD SELECTION', context)
        self.assertIn('selected_turn: turn-2', context)
        self.assertIn(f'selected_event_sequence: {selected.sequence}', context)
        self.assertIn('CURRENT SELECTION EVIDENCE', context)
        self.assertIn('[turn turn-2,', context)
        self.assertIn('category: tool', context)
        self.assertIn('tool: exec_command', context)

    def test_exact_selected_event_survives_large_turn_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)
            trace = result.traces[0]
            turn_events = [event for event in trace.events if event.turn_id == 'turn-1']
            selected = replace(turn_events[-1], title='Exact selected wait event')
            replacements = {
                turn_events[0].sequence: replace(turn_events[0], text='large earlier context ' * 2_000),
                selected.sequence: selected,
            }
            events = tuple(replacements.get(event.sequence, event) for event in trace.events)
            result = replace(result, traces=(replace(trace, events=events),))

            context = build_qa_context(
                result,
                question='What is the selected event doing?',
                scope='journal',
                session_id='session-1',
                turn_id='turn-1',
                event_sequence=selected.sequence,
                max_chars=2_000,
            )

        self.assertIn('Exact selected wait event', context)
        self.assertIn(f'[turn turn-1, line {selected.line_number}]', context)

    def test_generic_current_question_falls_back_to_latest_events(self) -> None:
        def event(sequence: int, text: str) -> TraceEvent:
            return TraceEvent(
                session_id='session-1',
                session_file='session.jsonl',
                turn_id=f'turn-{sequence}',
                sequence=sequence,
                line_number=sequence,
                output_line_number=None,
                timestamp=f'2026-08-21T00:00:0{sequence}Z',
                category='message',
                kind='assistant_message',
                role='assistant',
                phase='commentary',
                title='Assistant message',
                text=text,
                tool_name='',
                call_id='',
                status='',
                input_text='',
                output_text='',
                duration_secs=None,
                truncated_fields=(),
            )

        older = event(1, 'The demo agent reviews example code.')
        current = event(2, 'The agent is fixing dashboard question routing.')

        selected = _select_qa_events([older, current], question='what it currently do', max_events=1)

        self.assertEqual(selected, [current])

    def test_builds_state_aware_agent_context_without_source_path_or_encrypted_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)

            context = build_agent_workflow_context(
                result,
                session_id='session-1',
                turn_id='turn-1',
                event_sequence=1,
                max_chars=12_000,
            )

        self.assertIn('CURRENT DASHBOARD SELECTION', context)
        self.assertIn('selected_turn: turn-1', context)
        self.assertIn('SELECTED CONTEXT', context)
        self.assertRegex(context, r'\[turn turn-1, line \d+\]')
        self.assertNotIn(str(path), context)
        self.assertNotIn('TOP_SECRET_ENCRYPTED_REASONING', context)
        self.assertLessEqual(len(context), 12_000)

    def test_builds_explicit_message_ask_target_as_untrusted_ui_focus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)

            context = build_qa_context(
                result,
                question='Why did this happen?',
                scope='journal',
                session_id='session-1',
                turn_id='turn-1',
                event_sequence=1,
                view_state={
                    'ask_target_kind': 'message',
                    'ask_target_event_sequence': '1',
                    'ask_target_line_number': '3',
                    'ask_target_label': 'Assistant message',
                    'ask_target_summary': 'The parser checks passed.',
                    'ask_target_text': 'Implemented and verified the parser.',
                    'ask_target_role': 'assistant',
                    'ask_target_category': 'message',
                    'ask_target_status': 'completed',
                },
                max_chars=20_000,
            )

        self.assertIn('CURRENT ASK TARGET', context)
        self.assertIn('untrusted UI focus', context)
        self.assertIn('kind: message', context)
        self.assertIn('label: Assistant message', context)
        self.assertIn('event_sequence: 1', context)
        self.assertIn('line_number: 3', context)

    def test_builds_highlight_and_checkpoint_focus_as_untrusted_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)

            context = build_qa_context(
                result,
                question='Explain the current focus.',
                scope='journal',
                session_id='session-1',
                turn_id='turn-1',
                event_sequence=1,
                view_state={
                    'highlighted_text': 'The parser checks passed.',
                    'highlight_origin': 'checkpoint',
                    'checkpoint_index': '2',
                    'checkpoint_title': 'Verified the parser',
                    'checkpoint_status': 'completed',
                    'checkpoint_summary': 'All deterministic checks passed.',
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
                },
                max_chars=20_000,
            )

        self.assertIn('CURRENT HIGHLIGHTED TEXT', context)
        self.assertIn('untrusted UI focus', context)
        self.assertIn('CURRENT SELECTED CHECKPOINT', context)
        self.assertIn('model-generated UI focus', context)
        self.assertIn('title: Verified the parser', context)
        self.assertIn('what_agent_did: Ran deterministic parser checks.', context)
        self.assertIn('achievements: Confirmed normalized event coverage.', context)
        self.assertIn('blockers: One live result was unavailable.', context)
        self.assertIn('artifacts: tests/test_parser.py', context)
        self.assertIn('next_steps: Inspect the missing live result.', context)
        self.assertIn('CURRENT SELECTED EXECUTION CONTRACT', context)
        self.assertIn('contract_id: approval-before-production', context)
        self.assertIn('expectation: Approval must precede deployment.', context)
        self.assertIn('observation: Deployment was attempted without approval.', context)
        self.assertIn('[turn turn-1, line 3]', context)

    def test_builds_checkpoint_context_for_complete_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)

            context = build_session_checkpoint_context(
                result,
                session_id='session-1',
                turn_id='turn-1',
                event_sequence=1,
                max_chars=12_000,
            )

        self.assertIn('SESSION CHECKPOINT EVIDENCE', context)
        self.assertIn('scope: the complete selected session across all turns', context)
        self.assertIn('CHRONOLOGICAL SESSION TIMELINE', context)
        self.assertIn('[turn turn-1,', context)
        self.assertIn('[turn turn-2,', context)
        self.assertNotIn(str(path), context)
        self.assertNotIn('TOP_SECRET_ENCRYPTED_REASONING', context)

    def test_checkpoint_context_accepts_session_scope_and_incremental_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)

            context = build_session_checkpoint_context(
                result,
                session_id='session-1',
                turn_id=None,
                event_sequence=None,
                max_chars=12_000,
                previous_summary={'summary': 'Turn one was completed.', 'through_event_sequence': 4},
                after_sequence=4,
            )

        self.assertIn('PREVIOUS PERSISTED SESSION BRIEF', context)
        self.assertIn('previous_summary_through_sequence: 4', context)
        self.assertIn('CHRONOLOGICAL NEW EVENTS', context)
        self.assertIn('[turn turn-2,', context)

    def test_checkpoint_context_reserves_event_evidence_for_large_turn_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)
            base_turn = result.turns[0]
            turns = tuple(
                replace(
                    base_turn,
                    turn_id=f'turn-{index:04d}',
                    started_at=f'2026-08-10T{index // 3600:02d}:{(index // 60) % 60:02d}:{index % 60:02d}Z',
                )
                for index in range(700)
            )
            trace = result.traces[0]
            objective_event = replace(
                trace.events[0],
                turn_id='turn-0000',
                sequence=1,
                line_number=2,
                category='message',
                kind='user_message',
                role='user',
                title='User message',
                text='Locate the example analytics dashboard code.',
            )
            result = replace(
                result,
                turns=turns,
                traces=(replace(trace, events=(objective_event, *trace.events)),),
            )

            context = build_session_checkpoint_context(
                result,
                session_id='session-1',
                turn_id=None,
                event_sequence=None,
                max_chars=12_000,
            )

        self.assertLessEqual(len(context), 12_000)
        self.assertIn('TURN OVERVIEW', context)
        self.assertIn('[turn overview sampled:', context)
        self.assertIn('CHRONOLOGICAL SESSION TIMELINE', context)
        self.assertIn('Locate the example analytics dashboard code.', context)

    def test_requires_configuration_before_calling_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)

            with self.assertRaisesRegex(QAUnavailableError, 'Configure a model API'):
                JournalQA(QASettings(api_key='')).answer(
                    result,
                    question='What happened?',
                    scope='journal',
                    session_id='session-1',
                    turn_id=None,
                )

    def test_selected_external_agent_can_answer_without_model_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)
            backend = _FakeTraceAgentBackend()
            qa = JournalQA(QASettings(api_key=''), agent_backend=backend)

            status = qa.public_status()
            response = qa.answer(
                result,
                question='What happened?',
                scope='journal',
                session_id='session-1',
                turn_id='turn-1',
                history=[{'question': 'What ran?', 'answer': 'A tool ran.'}],
            )

        self.assertTrue(status['configured'])
        self.assertFalse(status['api_configured'])
        self.assertEqual(status['agent_type_id'], 'opencode')
        self.assertFalse(status['agent_uses_api_settings'])
        self.assertEqual(response['harness'], 'OpenCode')
        self.assertIn('CURRENT DASHBOARD REQUEST', backend.answer_kwargs['prompt'])
        self.assertEqual(len(backend.answer_kwargs['history']), 1)

    def test_controller_routes_with_current_state_and_selected_external_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)
            backend = _FakeTraceAgentBackend()
            qa = JournalQA(QASettings(api_key=''), agent_backend=backend)

            routed = qa.route_message(
                result,
                message='What happened?',
                scope='journal',
                session_id='session-1',
                turn_id='turn-1',
                workflow_state={'workflows': {'audit': True}, 'active_run_id': None},
            )

        self.assertEqual(routed.decision.action, 'answer')
        self.assertEqual(routed.harness, 'OpenCode')
        prompt = backend.route_kwargs['prompt']
        self.assertIn('CURRENT CONTROLLER REQUEST', prompt)
        self.assertIn('user_message_json: "What happened?"', prompt)
        self.assertIn('"audit": true', prompt)
        self.assertIn('journal_evidence_preloaded: false', prompt)
        self.assertNotIn('TOP_SECRET_TOOL_OUTPUT', prompt)
        self.assertNotIn(str(path), prompt)

    def test_studio_route_and_rule_draft_do_not_scan_events_before_a_context_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)
            backend = _FakeTraceAgentBackend()
            qa = JournalQA(QASettings(api_key=''), agent_backend=backend)

            with mock.patch(
                'agent_trace_studio.qa._select_qa_events',
                side_effect=AssertionError('eager journal scan'),
            ):
                routed = qa.route_message(
                    result,
                    message='What happened?',
                    scope='journal',
                    session_id='session-1',
                    turn_id='turn-1',
                )
                proposal = qa.propose_audit_rule(
                    result,
                    instruction='When deployment approval is requested, send approve.',
                    session_id='session-1',
                    turn_id='turn-1',
                    event_sequence=3,
                    view_state={},
                    current_rules=[],
                )

        self.assertEqual(routed.decision.action, 'answer')
        self.assertEqual(proposal.content.rule.id, 'validate-after-edit')

    def test_selected_external_agent_can_draft_audit_rule_without_writing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)
            backend = _FakeTraceAgentBackend()
            qa = JournalQA(QASettings(api_key=''), agent_backend=backend)
            resources = DashboardResourceAccess(
                catalog=(
                    {
                        'name': 'audit_rules',
                        'description': 'Saved rules.',
                        'revision': 2,
                        'item_count': 1,
                        'available': True,
                    },
                ),
                reader=lambda _resource, _resource_id, _limit: {'items': []},
            )

            response = qa.propose_audit_rule(
                result,
                instruction='Require validation after a source edit.',
                session_id='session-1',
                turn_id='turn-1',
                event_sequence=3,
                view_state={'selected_filter': 'tools'},
                current_rules=[{'id': 'existing-rule', 'version': 2}],
                dashboard_resources=resources,
            )

        self.assertEqual(response.harness, 'OpenCode')
        self.assertEqual(response.content.rule.id, 'validate-after-edit')
        prompt = backend.rule_kwargs['prompt']
        self.assertIn('CURRENT USER INSTRUCTION', prompt)
        self.assertIn('CURRENT AUDIT RULE STATE', prompt)
        self.assertIn('rule_count: 1', prompt)
        self.assertIn('ON-DEMAND DASHBOARD RESOURCES', prompt)
        self.assertNotIn('"existing-rule"', prompt)
        self.assertIn('AGENT-MANAGED CONTEXT', prompt)
        self.assertIn('journal_evidence_preloaded: false', prompt)
        self.assertNotIn('TOP_SECRET_TOOL_OUTPUT', prompt)
        self.assertNotIn(str(path), prompt)

    def test_dashboard_resources_are_listed_and_read_only_on_demand(self) -> None:
        calls = []

        def read(resource: str, resource_id: str, limit: int) -> dict[str, object]:
            calls.append((resource, resource_id, limit))
            return {
                'resource': resource,
                'items': [{'id': resource_id, 'title': 'Deployment Approval Request'}],
            }

        access = DashboardResourceAccess(
            catalog=(
                {
                    'name': 'audit_rules',
                    'description': 'Saved rules.',
                    'revision': 2,
                    'item_count': 1,
                    'available': True,
                },
            ),
            reader=read,
        )
        context = SimpleNamespace(deps=SimpleNamespace(dashboard_resources=access))

        catalog = list_dashboard_resources(context)
        rule = read_dashboard_resource(
            context,
            'audit_rules',
            resource_id='deployment-approval-request',
            limit=3,
        )

        self.assertIn('audit_rules', catalog)
        self.assertIn('Deployment Approval Request', rule)
        self.assertEqual(calls, [('audit_rules', 'deployment-approval-request', 3)])

    def test_pydantic_rule_author_selects_trace_context_with_native_tools(self) -> None:
        def model(messages: list[object], agent_info: object) -> ModelResponse:
            searched = any(
                isinstance(part, ToolReturnPart) and part.tool_name == 'search_trace'
                for message in messages
                for part in message.parts  # type: ignore[attr-defined]
            )
            if not searched:
                return ModelResponse(parts=[ToolCallPart('search_trace', {'query': 'exec_command', 'limit': 2})])
            output_tool = agent_info.output_tools[0]  # type: ignore[attr-defined]
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        output_tool.name,
                        {
                            'summary': 'Drafted from agent-selected evidence.',
                            'rule': {
                                'id': 'require-exec-command',
                                'title': 'Require command execution',
                                'expectation': 'The session invokes exec_command.',
                                'severity': 'medium',
                                'rule_type': 'require_event',
                                'event': {'tool_name': 'exec_command'},
                            },
                        },
                    )
                ]
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)
            qa = JournalQA(QASettings(api_key='test-key', model='test-model'))
            with mock.patch('agent_trace_studio.qa._qa_model_for_settings', return_value=FunctionModel(model)):
                response = qa.propose_audit_rule(
                    result,
                    instruction='Require the observed command tool.',
                    session_id='session-1',
                    turn_id='turn-1',
                    event_sequence=3,
                    view_state={},
                    current_rules=[],
                )

        self.assertEqual(response.content.rule.event.tool_name, 'exec_command')
        self.assertIn('search_trace', response.tools)

    def test_external_audit_rule_proposal_parser_rejects_trailing_narration(self) -> None:
        payload = {
            'summary': 'Drafted a validation rule.',
            'rule': {
                'id': 'validate-after-edit',
                'title': 'Validate after edits',
                'expectation': 'Validation follows edits.',
                'severity': 'high',
                'rule_type': 'require_after',
                'event': {'tool_name': 'apply_patch'},
                'required_event': {'tool_name': 'exec_command'},
            },
        }

        parsed = parse_audit_rule_proposal(json.dumps(payload))
        self.assertEqual(parsed.rule.id, 'validate-after-edit')
        with self.assertRaisesRegex(QAUnavailableError, 'invalid audit rule proposal'):
            parse_audit_rule_proposal(f'{json.dumps(payload)}\nI also changed the rule.')
        with self.assertRaisesRegex(QAUnavailableError, 'invalid audit rule proposal'):
            parse_audit_rule_proposal(f'Here is the rule:\n{json.dumps(payload)}')

    def test_pydantic_controller_loads_a_portable_skill_on_demand(self) -> None:
        requests: list[str] = []

        def model(messages: list[object], agent_info: object) -> ModelResponse:
            requests.append(repr(messages))
            loaded = any(
                isinstance(part, ToolReturnPart) and part.tool_name == 'load_capability'
                for message in messages
                for part in message.parts  # type: ignore[attr-defined]
            )
            if not loaded:
                return ModelResponse(parts=[ToolCallPart('load_capability', {'id': 'trace-qa'})])
            output_tool = agent_info.output_tools[0]  # type: ignore[attr-defined]
            return ModelResponse(
                parts=[ToolCallPart(output_tool.name, {'action': 'answer', 'answer': 'Grounded answer.'})]
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)
            qa = JournalQA(QASettings(api_key='test-key', model='test-model'))

            with mock.patch(
                'agent_trace_studio.qa._qa_model_for_settings',
                return_value=FunctionModel(model),
            ):
                routed = qa.route_message(
                    result,
                    message='What happened?',
                    scope='journal',
                    session_id='session-1',
                    turn_id='turn-1',
                )

        self.assertEqual(routed.decision.action, 'answer')
        self.assertEqual(routed.decision.answer, 'Grounded answer.')
        self.assertIn('load_capability', routed.tools)
        self.assertEqual(len(requests), 2)
        self.assertIn('# Skill: trace-qa', requests[1])

    def test_pydantic_controller_reads_dashboard_resources_on_demand(self) -> None:
        calls = []

        def read(resource: str, resource_id: str, limit: int) -> dict[str, object]:
            calls.append((resource, resource_id, limit))
            return {'items': [{'id': resource_id, 'title': 'Deployment Approval Request'}]}

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
            reader=read,
        )

        def model(messages: list[object], agent_info: object) -> ModelResponse:
            read_result = any(
                isinstance(part, ToolReturnPart) and part.tool_name == 'read_dashboard_resource'
                for message in messages
                for part in message.parts  # type: ignore[attr-defined]
            )
            if not read_result:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            'read_dashboard_resource',
                            {
                                'resource': 'audit_rules',
                                'resource_id': 'deployment-approval-request',
                                'limit': 5,
                            },
                        )
                    ]
                )
            output_tool = agent_info.output_tools[0]  # type: ignore[attr-defined]
            return ModelResponse(
                parts=[ToolCallPart(output_tool.name, {'action': 'answer', 'answer': 'The rule is active.'})]
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)
            qa = JournalQA(QASettings(api_key='test-key', model='test-model'))
            with mock.patch('agent_trace_studio.qa._qa_model_for_settings', return_value=FunctionModel(model)):
                routed = qa.route_message(
                    result,
                    message='Summarize the current audit rule.',
                    scope='journal',
                    session_id='session-1',
                    turn_id='turn-1',
                    dashboard_resources=resources,
                )

        self.assertEqual(routed.decision.answer, 'The rule is active.')
        self.assertIn('read_dashboard_resource', routed.tools)
        self.assertEqual(calls, [('audit_rules', 'deployment-approval-request', 5)])

    def test_pydantic_activity_adapter_reports_events_without_private_content(self) -> None:
        events = [
            SimpleNamespace(
                event_kind='part_start',
                part=SimpleNamespace(part_kind='thinking', content='PRIVATE_REASONING'),
            ),
            SimpleNamespace(
                event_kind='function_tool_call',
                part=SimpleNamespace(
                    part_kind='tool-call',
                    tool_name='search_trace',
                    args={'query': 'PRIVATE_QUERY'},
                ),
            ),
            SimpleNamespace(
                event_kind='function_tool_result',
                part=SimpleNamespace(
                    part_kind='tool-return',
                    tool_name='search_trace',
                    content='PRIVATE_RESULT',
                ),
            ),
            SimpleNamespace(
                event_kind='part_start',
                part=SimpleNamespace(part_kind='text', content='PRIVATE_DRAFT'),
            ),
            SimpleNamespace(
                event_kind='part_end',
                part=SimpleNamespace(part_kind='text', content='PRIVATE_DRAFT'),
            ),
            SimpleNamespace(event_kind='final_result', part=None),
        ]
        activity = []
        handler = _pydantic_event_stream_handler(activity.append)

        async def stream():
            for event in events:
                yield event

        self.assertIsNotNone(handler)
        asyncio.run(handler(None, stream()))

        serialized_activity = ' '.join(item.message for item in activity)
        self.assertIn('Pydantic AI started tool search_trace.', serialized_activity)
        self.assertIn('Pydantic AI completed tool search_trace.', serialized_activity)
        self.assertNotIn('Response', [item.phase for item in activity])
        self.assertNotIn('PRIVATE_', serialized_activity)

    def test_runs_state_aware_trace_agent_with_read_only_context_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)
            qa = JournalQA(QASettings(api_key='test-key', model='test-model'))

            with mock.patch(
                'agent_trace_studio.qa._qa_model_for_settings',
                return_value=TestModel(call_tools=['inspect_current_context']),
            ):
                response = qa.answer(
                    result,
                    question='Did the tool complete?',
                    scope='turn',
                    session_id='session-1',
                    turn_id='turn-1',
                )

        self.assertEqual(response['harness'], 'Pydantic AI')
        self.assertEqual(response['tools'], ['inspect_current_context'])
        self.assertIn('[turn turn-1, line', response['answer'])
        self.assertGreaterEqual(response['usage']['requests'], 2)
        self.assertNotIn('TOP_SECRET_ENCRYPTED_REASONING', response['answer'])

    def test_trace_tools_stay_within_the_selected_session_and_turn_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.jsonl'
            write_journal(path)
            result = analyze_journals([path], include_trace=True)
            context = TraceQAContext(
                result=result,
                question='What did exec_command return?',
                scope='turn',
                session_id='session-1',
                turn_id='turn-1',
                event_sequence=3,
                view_state={'category': 'tool'},
                max_context_chars=20_000,
            )
            run_context = SimpleNamespace(deps=context)

            current = inspect_current_context(run_context)
            searched = search_trace(run_context, 'exec_command', tool_name='exec_command')
            missing = search_trace(run_context, 'definitely_absent_trace_term')
            turn = read_trace_turn(run_context, 'turn-1')
            outside = read_trace_turn(run_context, 'turn-2')

        self.assertIn('CURRENT DASHBOARD SELECTION', current)
        self.assertIn('tool=exec_command', searched)
        self.assertIn('No trace events matched', missing)
        self.assertIn('[turn turn-1,', turn)
        self.assertNotIn('[turn turn-2,', turn)
        self.assertIn('outside the active turn scope', outside)

    def test_conversation_history_is_passed_as_native_agent_messages(self) -> None:
        messages = _qa_message_history(
            [
                {'question': 'What failed?', 'answer': 'The check failed [turn turn-1, line 8].'},
                {'question': '', 'answer': 'ignored'},
            ],
            model_name='test-model',
        )

        self.assertEqual(len(messages), 2)
        self.assertIsInstance(messages[0], ModelRequest)
        self.assertEqual(messages[0].parts[0].content, 'What failed?')
        self.assertIsInstance(messages[1], ModelResponse)
        self.assertEqual(messages[1].parts, [TextPart('The check failed [turn turn-1, line 8].')])

        complete_history = _qa_message_history(
            [{'question': f'Question {index}', 'answer': f'Answer {index}'} for index in range(10)],
            model_name='test-model',
        )
        self.assertEqual(len(complete_history), 20)
        self.assertEqual(complete_history[0].parts[0].content, 'Question 0')

    def test_builds_provider_native_models_for_trace_agent(self) -> None:
        cases = (
            (
                QASettings(api_key='openai-secret', provider='openai', model='test-openai'),
                OpenAIResponsesModel,
                'https://api.openai.com/v1/',
            ),
            (
                QASettings(api_key='anthropic-secret', provider='anthropic', model='test-anthropic'),
                AnthropicModel,
                'https://api.anthropic.com',
            ),
            (
                QASettings(api_key='google-secret', provider='google', model='models/test-google'),
                GoogleModel,
                'https://generativelanguage.googleapis.com',
            ),
        )

        for settings, expected_type, expected_base_url in cases:
            with self.subTest(provider=settings.provider):
                model = _qa_model_for_settings(settings)
                self.assertIsInstance(model, expected_type)
                self.assertEqual(str(model.provider.base_url), expected_base_url)

    def test_provider_catalog_includes_current_model_presets(self) -> None:
        catalog = {provider['id']: provider for provider in qa_provider_catalog()}

        self.assertIn('gpt-5.6-sol', {model['id'] for model in catalog['openai']['models']})
        self.assertIn('claude-sonnet-5', {model['id'] for model in catalog['anthropic']['models']})
        self.assertIn('gemini-3.6-flash', {model['id'] for model in catalog['google']['models']})

    def test_reads_selected_provider_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                'CODEX_SESSION_DASHBOARD_QA_PROVIDER': 'anthropic',
                'ANTHROPIC_API_KEY': 'anthropic-env-secret',
            },
            clear=True,
        ):
            settings = QASettings.from_environment()

        self.assertEqual(settings.provider, 'anthropic')
        self.assertEqual(settings.model, 'claude-sonnet-5')
        self.assertEqual(settings.base_url, 'https://api.anthropic.com/v1')
        self.assertEqual(settings.api_key, 'anthropic-env-secret')


if __name__ == '__main__':
    unittest.main()
