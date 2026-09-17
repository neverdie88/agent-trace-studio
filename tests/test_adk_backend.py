from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from google.adk.models._capabilities import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart

from agent_trace_studio.adk_backend import (
    AdkFixerSession,
    AdkRepairAgents,
    AdkResult,
    _model_for_settings,
    _source_tools,
    run_adk,
    run_adk_agent_controller,
    run_adk_trace_qa,
)
from agent_trace_studio.agent_backend import ParserAudit
from agent_trace_studio.harness_backend import SelectableRepairAgents
from agent_trace_studio.parser import analyze_journals
from agent_trace_studio.qa import (
    AgentMessageDecision,
    AgentMessageResult,
    DashboardResourceAccess,
    QASettings,
    QAUnavailableError,
    StudioAgentCancelled,
    TraceQAContext,
)
from helpers import write_journal


class ScriptedModel(BaseLlm):
    model: str = 'synthetic-model'
    responses: list[Any] = Field(default_factory=list)
    requests: list[Any] = Field(default_factory=list)
    delay: float = 0

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(self, llm_request: Any, stream: bool = False):
        self.requests.append(
            llm_request.model_copy(
                update={'contents': [content.model_copy(deep=True) for content in llm_request.contents]}
            )
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        yield response


def response(text: str = '', *, tool: str = '', args: dict | None = None) -> LlmResponse:
    part = types.Part(function_call=types.FunctionCall(name=tool, args=args or {})) if tool else types.Part(text=text)
    return LlmResponse(
        content=types.Content(role='model', parts=[part]),
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10,
            candidates_token_count=3,
            thoughts_token_count=2,
            total_token_count=15,
        ),
    )


def context_in(root: Path, *, resources: DashboardResourceAccess | None = None) -> TraceQAContext:
    journal = root / 'synthetic-session.jsonl'
    write_journal(journal)
    return TraceQAContext(
        result=analyze_journals([journal], include_trace=True),
        question='Explain this selection.',
        scope='journal',
        session_id='session-1',
        turn_id='turn-1',
        event_sequence=3,
        view_state={},
        max_context_chars=20_000,
        dashboard_resources=resources,
    )


class AdkRuntimeTests(unittest.TestCase):
    def test_native_loop_retrieves_bounded_resources_before_structured_action(self) -> None:
        secret = 'synthetic-provider-credential'
        resources = DashboardResourceAccess(
            catalog=({'name': 'audit_rules', 'available': True},),
            reader=lambda *_: {'title': 'Require tests', 'api_key': secret, 'path': '/synthetic/private/path'},
        )
        model = ScriptedModel(
            responses=[
                response(tool='read_dashboard_resource', args={'resource': 'audit_rules'}),
                response('{"action":"answer","answer":"The Require tests rule is saved."}'),
            ]
        )
        activity = []
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                'agent_trace_studio.adk_backend._model_for_settings',
                return_value=model,
            ),
        ):
            result = run_adk_agent_controller(
                prompt='Read the saved rules.',
                context=context_in(Path(directory), resources=resources),
                history=[],
                settings=QASettings(api_key=secret),
                progress=activity.append,
            )
        self.assertEqual(result.harness, 'Google ADK')
        self.assertEqual(result.decision.action, 'answer')
        self.assertIn('read_dashboard_resource', result.tools)
        self.assertEqual(result.usage, {'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30, 'requests': 2})
        second_request = str(model.requests[1].contents)
        self.assertIn('Require tests', second_request)
        self.assertNotIn(secret, second_request)
        self.assertNotIn('/synthetic/private/path', second_request)
        details = [item.details for item in activity if item.details]
        self.assertEqual([item['status'] for item in details], ['running', 'completed'])
        self.assertEqual(details[0]['id'], details[1]['id'])
        names = set(model.requests[0].tools_dict)
        self.assertIn('read_trace_turn', names)
        self.assertIn('load_capability', names)
        self.assertNotIn('write_file', names)
        self.assertNotIn('run_tests', names)

    def test_history_and_latest_selection_are_given_to_native_adk(self) -> None:
        secret = 'synthetic-provider-credential'
        model = ScriptedModel(responses=[response('A remembered answer.')])
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                'agent_trace_studio.adk_backend._model_for_settings',
                return_value=model,
            ),
        ):
            result = run_adk_trace_qa(
                prompt='Current selection is turn-2.',
                context=context_in(Path(directory)),
                history=[],
                settings=QASettings(api_key=secret),
                message_history=[
                    ModelRequest.user_text_prompt('Prior question ' + secret),
                    ModelResponse(parts=[TextPart('Prior answer.')], model_name='prior-model'),
                ],
            )
        request = model.requests[0].model_dump_json()
        self.assertIn('Prior question', request)
        self.assertIn('Prior answer', request)
        self.assertIn('turn-2', request)
        self.assertNotIn(secret, request)
        self.assertEqual(result.answer, 'A remembered answer.')

    def test_invalid_action_and_provider_errors_never_escape_as_actions_or_secrets(self) -> None:
        for value in ('{"action":"execute_shell","answer":"bad"}', RuntimeError('synthetic-secret-header')):
            model = ScriptedModel(responses=[value if isinstance(value, Exception) else response(value)])
            with (
                self.subTest(value=type(value).__name__),
                mock.patch(
                    'agent_trace_studio.adk_backend._model_for_settings',
                    return_value=model,
                ),
                self.assertRaises(QAUnavailableError) as failure,
            ):
                run_adk(
                    settings=QASettings(api_key='synthetic-secret-header'),
                    instructions='Test',
                    prompt='Test',
                    name='test_agent',
                    output_type=AgentMessageDecision,
                )
            self.assertNotIn('synthetic-secret-header', str(failure.exception))

    def test_stop_interrupts_pending_model_call(self) -> None:
        model = ScriptedModel(responses=[response('late')], delay=30)
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                'agent_trace_studio.adk_backend._model_for_settings',
                return_value=model,
            ),
        ):
            context = replace(context_in(Path(directory)), cancel_event=threading.Event())
            timer = threading.Timer(0.1, context.cancel_event.set)
            timer.start()
            try:
                with self.assertRaises(StudioAgentCancelled):
                    run_adk(
                        settings=QASettings(api_key='synthetic-key'),
                        instructions='Test',
                        prompt='Test',
                        name='test_agent',
                        context=context,
                    )
            finally:
                timer.cancel()

    def test_readonly_and_fixer_tools_keep_source_boundaries(self) -> None:
        settings = QASettings(api_key='synthetic-key')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'source'
            workspace.mkdir()
            (workspace / 'example.txt').write_text('original')
            (workspace / '.env').write_text('synthetic-secret')
            (root / 'outside.txt').write_text('outside-secret')
            (workspace / 'escape.txt').symlink_to(root / 'outside.txt')
            (workspace / '.agent-trace-studio').mkdir()
            (workspace / '.agent-trace-studio/audit-report.json').write_text('{}')
            readonly = {tool.name: tool for tool in _source_tools(workspace, settings, None)}
            writable = {tool.name: tool for tool in _source_tools(workspace, settings, None, writable=True)}
            self.assertNotIn('write_file', readonly)
            self.assertNotIn('run_tests', readonly)
            self.assertNotIn('run_command', writable)
            for path in ('../outside.txt', 'escape.txt', '.env'):
                result = asyncio.run(readonly['read_file'].run_async(args={'path': path}, tool_context=None))
                self.assertNotIn('outside-secret', str(result))
                self.assertNotIn('synthetic-secret', str(result))
            for path in ('.agent-trace-studio/audit-report.json', 'src/agent_trace_studio/adk_backend.py'):
                result = asyncio.run(
                    writable['write_file'].run_async(
                        args={'path': path, 'content': 'changed'},
                        tool_context=None,
                    )
                )
                self.assertIn('error', result)
            self.assertEqual((workspace / '.agent-trace-studio/audit-report.json').read_text(), '{}')
            asyncio.run(
                writable['write_file'].run_async(
                    args={'path': 'example.txt', 'content': 'changed', 'expected_hash': 'incorrect'},
                    tool_context=None,
                )
            )
            self.assertEqual((workspace / 'example.txt').read_text(), 'original')
            expected = hashlib.sha256(b'original').hexdigest()[:12]
            asyncio.run(
                writable['write_file'].run_async(
                    args={'path': 'example.txt', 'content': 'changed', 'expected_hash': expected},
                    tool_context=None,
                )
            )
            self.assertEqual((workspace / 'example.txt').read_text(), 'changed')

    def test_fixer_test_command_does_not_inherit_provider_keys(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-secret', 'ANTHROPIC_API_KEY': 'other-secret'}),
            mock.patch('agent_trace_studio.adk_backend.Shell') as shell,
        ):
            shell.return_value.get_toolset.return_value.run_command = mock.AsyncMock(return_value='tests passed')
            tools = {
                tool.name: tool
                for tool in _source_tools(Path(directory), QASettings(api_key='synthetic-key'), None, writable=True)
            }
            result = asyncio.run(tools['run_tests'].run_async(args={}, tool_context=None))
            self.assertEqual(result, 'tests passed')
            environment = shell.call_args.kwargs['env']
            self.assertNotIn('OPENAI_API_KEY', environment)
            self.assertNotIn('ANTHROPIC_API_KEY', environment)
            shell.return_value.get_toolset.return_value.run_command.assert_awaited_once()

    def test_model_configuration_is_provider_specific_and_never_exports_keys(self) -> None:
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'unrelated-env-key'}):
            before = dict(os.environ)
            model = _model_for_settings(QASettings(api_key='synthetic-openai', model='gpt-test'))
            self.assertEqual(model.model, 'openai/responses/gpt-test')
            self.assertEqual(model._additional_args['api_key'], 'synthetic-openai')
            self.assertEqual(model._additional_args['api_base'], 'https://api.openai.com/v1')
            self.assertFalse(model._additional_args['store'])
            self.assertNotIn('synthetic-openai', model.model_dump_json())
            other = _model_for_settings(
                QASettings(api_key='synthetic-anthropic', provider='anthropic', model='claude-test')
            )
            self.assertEqual(other.model, 'anthropic/claude-test')
            self.assertEqual(other._additional_args['api_key'], 'synthetic-anthropic')
            self.assertEqual(other._additional_args['api_base'], 'https://api.anthropic.com')
            self.assertEqual(os.environ.get('OPENAI_API_KEY'), before['OPENAI_API_KEY'])
            self.assertNotIn('synthetic-openai', os.environ.values())
            self.assertNotIn('synthetic-anthropic', os.environ.values())

    def test_registry_persists_adk_conversation_and_routes_every_role_to_adk(self) -> None:
        reply = AgentMessageResult(
            AgentMessageDecision(action='answer', answer='ADK answer'),
            'gpt-test',
            'OpenAI Responses API',
            'Google ADK',
            {'requests': 1},
            [],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = replace(context_in(root), question='First synthetic-key')
            settings = QASettings(api_key='synthetic-key')
            first = SelectableRepairAgents(root / 'state', default_harness='google-adk')
            self.assertTrue(first.requires_api_settings)
            self.assertFalse(first.trace_qa_status(QASettings(api_key=''))['available'])
            with mock.patch('agent_trace_studio.adk_backend.run_adk_agent_controller', return_value=reply):
                first.route_message(
                    prompt='First', context=context, history=[], settings=settings, conversation_id='browser-one'
                )
            restored = SelectableRepairAgents(root / 'state', default_harness='google-adk')
            with mock.patch('agent_trace_studio.adk_backend.run_adk_agent_controller', return_value=reply) as run:
                restored.route_message(
                    prompt='Second', context=context, history=[], settings=settings, conversation_id='browser-one'
                )
            self.assertEqual(len(run.call_args.kwargs['message_history']), 2)
            self.assertNotIn('synthetic-key', str(run.call_args.kwargs['message_history']))
            self.assertEqual(restored._conversation_messages('other-browser', context.session_id), [])
            fake_backend = mock.Mock()
            restored._adk_analysis = fake_backend
            for method in ('investigate', 'extract_memories', 'summarize_checkpoints'):
                getattr(restored, method)('evidence', settings, context=context)
                getattr(fake_backend, method).assert_called_once()
            with mock.patch('agent_trace_studio.harness_backend.immutable_review_workspace'):
                for method in ('audit', 'verify'):
                    getattr(restored, method)(root, settings)
                    getattr(fake_backend, method).assert_called_once()
            restored.create_fixer_session(root, settings, run_id='test-run', state_dir=root / 'fixer')
            fake_backend.create_fixer_session.assert_called_once()
            restored.clear_conversation(conversation_id='browser-one', session_id=context.session_id)
            self.assertEqual(restored._conversation_messages('browser-one', context.session_id), [])

    def test_missing_adk_package_stays_unavailable_with_a_valid_key(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                'agent_trace_studio.harness_backend._package_version',
                return_value='',
            ),
        ):
            registry = SelectableRepairAgents(Path(directory), default_harness='google-adk')
            status = registry.trace_qa_status(QASettings(api_key='synthetic-key'))
            self.assertFalse(status['available'])
            self.assertIn('uv sync', status['detail'])

    def test_fixer_history_survives_new_instances_and_review_is_independent(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                'agent_trace_studio.adk_backend.run_adk',
                return_value=AdkResult('Done', {}, []),
            ) as run,
        ):
            root = Path(directory)
            settings = QASettings(api_key='synthetic-key')
            audit = ParserAudit(summary='Update a synthetic component.', requires_fix=True, confidence='high')
            first = AdkFixerSession(root, settings, run_id='run-1', state_dir=root / 'state')
            first.fix(attempt=1, audit=audit, feedback=None)
            second = AdkFixerSession(root, settings, run_id='run-1', state_dir=root / 'state')
            second.fix(attempt=2, audit=audit, feedback={'needs': 'tests'}, resume_pending_turn=True)
            self.assertIn('interrupted', run.call_args.kwargs['prompt'])
            self.assertGreaterEqual(len(run.call_args.kwargs['history']), 2)
            # Windows uses ACLs; POSIX mode bits do not describe its access control.
            if os.name != 'nt':
                self.assertEqual(second.history_path.stat().st_mode & 0o777, 0o600)
            with mock.patch('agent_trace_studio.adk_backend._verification_artifact_bundle', return_value='{}'):
                AdkRepairAgents().verify(root, settings)
            self.assertNotIn('history', run.call_args.kwargs)
            self.assertNotIn('write_file', [tool.name for tool in run.call_args.kwargs['tools']])


class AdkProviderHTTPTests(unittest.TestCase):
    def test_native_google_and_anthropic_have_explicit_provider_transports(self) -> None:
        cases = [
            (
                'google',
                'gemini-2.5-flash',
                '/v1beta',
                '/v1beta/models/gemini-2.5-flash:generateContent',
                'x-goog-api-key',
                {
                    'candidates': [
                        {
                            'content': {'role': 'model', 'parts': [{'text': 'Synthetic success.'}]},
                            'finishReason': 'STOP',
                        }
                    ],
                    'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 4, 'totalTokenCount': 16},
                    'modelVersion': 'gemini-2.5-flash',
                },
            ),
            (
                'anthropic',
                'claude-sonnet-4-20250514',
                '/v1',
                '/v1/messages',
                'x-api-key',
                {
                    'id': 'msg_synthetic',
                    'type': 'message',
                    'role': 'assistant',
                    'model': 'claude-sonnet-4-20250514',
                    'content': [{'type': 'text', 'text': 'Synthetic success.'}],
                    'stop_reason': 'end_turn',
                    'stop_sequence': None,
                    'usage': {'input_tokens': 12, 'output_tokens': 4},
                },
            ),
        ]
        for provider, model, base, endpoint, header, fixture in cases:
            with self.subTest(provider=provider), mock.patch.dict(os.environ, {'GOOGLE_GENAI_USE_VERTEXAI': 'true'}):
                requests = []

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_):
                        pass

                    def do_POST(self, requests=requests, fixture=fixture):
                        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        requests.append(
                            (self.path, {key.lower(): value for key, value in self.headers.items()}, payload)
                        )
                        body = json.dumps(fixture).encode()
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    result = run_adk(
                        settings=QASettings(
                            provider=provider,
                            api_key='synthetic-key',
                            model=model,
                            base_url=f'http://127.0.0.1:{server.server_port}{base}',
                        ),
                        instructions='Return a short answer.',
                        prompt='Synthetic request.',
                        name='provider_test',
                    )
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()
                self.assertEqual(result.output, 'Synthetic success.')
                self.assertEqual(requests[0][0], endpoint)
                self.assertEqual(requests[0][1][header], 'synthetic-key')
                self.assertNotIn('synthetic-key', json.dumps(requests[0][2]))
                self.assertEqual(result.usage['input_tokens'], 12)
                self.assertEqual(result.usage['output_tokens'], 4)

    def test_openai_uses_responses_endpoint_and_explicit_key(self) -> None:
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, dict(self.headers), request))
                body = json.dumps(
                    {
                        'id': 'resp_synthetic',
                        'object': 'response',
                        'created_at': 1,
                        'status': 'completed',
                        'model': 'gpt-4.1-mini',
                        'output': [
                            {
                                'id': 'msg_synthetic',
                                'type': 'message',
                                'role': 'assistant',
                                'status': 'completed',
                                'content': [{'type': 'output_text', 'text': 'Synthetic success.', 'annotations': []}],
                            }
                        ],
                        'usage': {'input_tokens': 12, 'output_tokens': 4, 'total_tokens': 16},
                    }
                ).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = run_adk(
                settings=QASettings(
                    api_key='synthetic-openai-key',
                    model='gpt-4.1-mini',
                    base_url=f'http://127.0.0.1:{server.server_port}/v1',
                ),
                instructions='Return a short answer.',
                prompt='Synthetic request.',
                name='http_test',
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(result.output, 'Synthetic success.')
        self.assertEqual(requests[0][0], '/v1/responses')
        headers = {key.lower(): value for key, value in requests[0][1].items()}
        self.assertEqual(headers['authorization'], 'Bearer synthetic-openai-key')
        self.assertNotIn('synthetic-openai-key', json.dumps(requests[0][2]))
        self.assertEqual(requests[0][2]['model'], 'gpt-4.1-mini')
        self.assertFalse(requests[0][2]['store'])
        self.assertEqual(result.usage['input_tokens'], 12)
        self.assertEqual(result.usage['output_tokens'], 4)


if __name__ == '__main__':
    unittest.main()
