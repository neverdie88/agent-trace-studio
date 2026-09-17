from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from agent_trace_studio.agent_backend import (
    AgentTransportError,
    MemoryExtraction,
    ParserAudit,
    PydanticRepairAgents,
    SessionInvestigation,
    VerificationVerdict,
)
from agent_trace_studio.harness_backend import (
    HarnessStatus,
    SelectableRepairAgents,
    _run_codex_source_review,
    _run_opencode_source_review,
)
from agent_trace_studio.qa import QASettings, QAUnavailableError, StudioAgentCancelled, TraceQAAgentResult
from agent_trace_studio.workflow_roles import (
    immutable_review_workspace,
    isolated_review_workspace,
    opencode_review_preflight,
    run_structured_role,
    structured_prompt,
)


def _result(value: str) -> TraceQAAgentResult:
    return TraceQAAgentResult(value, 'synthetic-model', 'synthetic-provider', 'test', {'requests': 1}, [])


def _audit() -> ParserAudit:
    return ParserAudit(summary='Synthetic structural audit.', requires_fix=False, confidence='high')


def _artifacts(workspace: Path) -> None:
    control = workspace / '.agent-trace-studio'
    control.mkdir(parents=True, exist_ok=True)
    for name in ('audit-report.json', 'change.patch', 'verification.json'):
        (control / name).write_text(f'Synthetic host artifact: {name}', encoding='utf-8')


class StructuredRoleTests(unittest.TestCase):
    def test_review_mirror_is_outside_git_and_detects_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.git').mkdir()
            workspace = root / 'workspace'
            _artifacts(workspace)
            (workspace / 'parser.py').write_text('synthetic original', encoding='utf-8')
            with (
                self.assertRaisesRegex(QAUnavailableError, 'changed the workspace'),
                isolated_review_workspace(workspace) as mirror,
            ):
                self.assertFalse(any((parent / '.git').exists() for parent in mirror.parents))
                self.assertEqual((mirror / 'parser.py').read_text(encoding='utf-8'), 'synthetic original')
                (mirror / 'parser.py').write_text('synthetic mutation', encoding='utf-8')
            self.assertFalse(mirror.exists())
            self.assertEqual((workspace / 'parser.py').read_text(encoding='utf-8'), 'synthetic original')

    def test_review_mirror_rejects_file_and_directory_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'file.txt').write_text('synthetic private file', encoding='utf-8')
            for target in (outside, outside / 'file.txt'):
                workspace = root / ('source-' + target.name)
                workspace.mkdir()
                (workspace / 'link').symlink_to(target)
                with (
                    self.subTest(target=target.name),
                    self.assertRaisesRegex(QAUnavailableError, 'symbolic links'),
                    isolated_review_workspace(workspace),
                ):
                    self.fail('Must refuse symlinks before launching the reviewer.')

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO fixture requires POSIX')
    def test_review_mirror_rejects_special_files_without_reading_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            os.mkfifo(workspace / 'synthetic-pipe')
            with (
                self.assertRaisesRegex(QAUnavailableError, 'regular files'),
                isolated_review_workspace(workspace),
            ):
                self.fail('Must refuse special files before reading or copying.')

    def test_correction_preserves_schema_and_evidence_without_echoing_invalid_output(self) -> None:
        prompt = structured_prompt('Audit instructions.', 'SYNTHETIC EVIDENCE', ParserAudit)
        run = mock.Mock(
            side_effect=[_result('{"summary":"PRIVATE_INVALID_TEXT"}'), _result(_audit().model_dump_json())]
        )
        activity = []
        value = run_structured_role(
            prompt=prompt,
            output_type=ParserAudit,
            run_turn=run,
            role='audit',
            backend_label='Codex SDK',
            progress=activity.append,
        )
        self.assertEqual(value, _audit())
        correction = run.call_args_list[1].args[0]
        self.assertIn('SYNTHETIC EVIDENCE', correction)
        self.assertIn('<output_schema>', correction)
        self.assertIn('requires_fix: missing', correction)
        self.assertNotIn('PRIVATE_INVALID_TEXT', correction + str(activity))

    def test_invalid_result_fails_without_leaking_model_text(self) -> None:
        run = mock.Mock(return_value=_result('PRIVATE_INVALID_TEXT'))
        with self.assertRaises(AgentTransportError) as raised:
            run_structured_role(
                prompt='Synthetic request',
                output_type=ParserAudit,
                run_turn=run,
                role='audit',
                backend_label='OpenCode',
            )
        self.assertEqual(raised.exception.kind, 'invalid_response')
        self.assertEqual(run.call_count, 3)
        self.assertNotIn('PRIVATE_INVALID_TEXT', str(raised.exception))

    def test_cancellation_is_not_converted_to_a_recoverable_model_error(self) -> None:
        with self.assertRaises(StudioAgentCancelled):
            run_structured_role(
                prompt='Synthetic request',
                output_type=ParserAudit,
                run_turn=mock.Mock(side_effect=StudioAgentCancelled('Stopped.')),
                role='audit',
                backend_label='Codex SDK',
            )

    def test_read_only_gate_covers_source_and_host_artifacts(self) -> None:
        for relative in ('parser.py', '.agent-trace-studio/verification.json'):
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                _artifacts(workspace)
                target = workspace / relative
                target.write_text('original', encoding='utf-8')
                with (
                    self.assertRaisesRegex(QAUnavailableError, 'changed the workspace'),
                    immutable_review_workspace(workspace),
                ):
                    target.write_text('changed', encoding='utf-8')

    def test_read_only_gate_rejects_artifact_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            _artifacts(workspace)
            (workspace / '.agent-trace-studio/link').symlink_to(workspace / 'parser.py')
            with (
                self.assertRaisesRegex(QAUnavailableError, 'symbolic links'),
                immutable_review_workspace(workspace),
            ):
                self.fail('must reject before running a reviewer')


class SelectedWorkflowTests(unittest.TestCase):
    def test_native_trace_context_is_bounded_and_cancellation_reaches_the_subprocess(self) -> None:
        for backend, function in (
            ('codex-sdk', '_run_codex_source_review'),
            ('opencode', '_run_opencode_source_review'),
        ):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                registry = SelectableRepairAgents(Path(directory), default_harness=backend)
                event = threading.Event()
                context = mock.Mock(cancel_event=event)
                plan = _result('{"context_requests":[{"tool":"inspect_current_context"}]}')
                final = _result(MemoryExtraction(summary='Synthetic memory.').model_dump_json())
                with (
                    mock.patch.object(registry, 'trace_qa_status', return_value={'available': True}),
                    mock.patch(f'agent_trace_studio.harness_backend.{function}', side_effect=[plan, final]) as native,
                    mock.patch(
                        'agent_trace_studio.harness_backend._execute_trace_context_plan',
                        return_value=('anchored synthetic evidence', []),
                    ) as retrieve,
                ):
                    registry.extract_memories(
                        'seed evidence',
                        QASettings(api_key='synthetic-key', max_context_chars=12000),
                        context=context,
                    )
                self.assertEqual(retrieve.call_args.kwargs['max_chars'], 12000 - len('seed evidence'))
                self.assertIn('anchored synthetic evidence', native.call_args.args[0])
                for call in native.call_args_list:
                    self.assertIs(call.kwargs['cancel_event'], event)

    def test_native_audit_and_verifier_use_only_the_selected_backend(self) -> None:
        verdict = VerificationVerdict(status='pass', summary='Synthetic review passed.')
        for backend, function in (
            ('codex-sdk', '_run_codex_source_review'),
            ('opencode', '_run_opencode_source_review'),
        ):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / 'workspace'
                _artifacts(workspace)
                analysis = mock.Mock(spec=PydanticRepairAgents)
                registry = SelectableRepairAgents(root / 'state', default_harness=backend, analysis_backend=analysis)
                with (
                    mock.patch.object(registry, 'trace_qa_status', return_value={'available': True}),
                    mock.patch(
                        f'agent_trace_studio.harness_backend.{function}',
                        side_effect=[_result(_audit().model_dump_json()), _result(verdict.model_dump_json())],
                    ) as native,
                ):
                    self.assertEqual(registry.audit(workspace, QASettings(api_key='')), _audit())
                    self.assertEqual(registry.verify(workspace, QASettings(api_key='')), verdict)
                analysis.audit.assert_not_called()
                analysis.verify.assert_not_called()
                self.assertEqual(native.call_count, 2)
                self.assertIn('audit-evidence.json', native.call_args_list[0].args[0])
                for name in ('audit-report.json', 'change.patch', 'verification.json'):
                    self.assertIn(f'Synthetic host artifact: {name}', native.call_args_list[1].args[0])
                for call in native.call_args_list:
                    self.assertEqual(call.args[1], workspace)
                    self.assertNotIn('thread_id', call.kwargs)
                    self.assertNotIn('session_id', call.kwargs)

    def test_native_investigation_and_memories_never_fall_back_to_pydantic(self) -> None:
        investigation = SessionInvestigation(
            title='Synthetic',
            summary='Synthetic investigation.',
            objective='Test routing.',
            outcome='completed',
        )
        memories = MemoryExtraction(summary='Synthetic memory extraction.')
        for backend, function in (
            ('codex-sdk', '_run_codex_source_review'),
            ('opencode', '_run_opencode_source_review'),
        ):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                analysis = mock.Mock(spec=PydanticRepairAgents)
                registry = SelectableRepairAgents(Path(directory), default_harness=backend, analysis_backend=analysis)
                with (
                    mock.patch.object(registry, 'trace_qa_status', return_value={'available': True}),
                    mock.patch(
                        f'agent_trace_studio.harness_backend.{function}',
                        side_effect=[_result(investigation.model_dump_json()), _result(memories.model_dump_json())],
                    ) as native,
                ):
                    self.assertEqual(registry.investigate('bounded evidence', QASettings(api_key='')), investigation)
                    self.assertEqual(registry.extract_memories('bounded evidence', QASettings(api_key='')), memories)
                analysis.investigate.assert_not_called()
                analysis.extract_memories.assert_not_called()
                for call in native.call_args_list:
                    self.assertIn('bounded evidence', call.args[0])
                    self.assertFalse(call.args[1].exists(), 'ephemeral trace role workspace must be cleaned up')

    def test_pydantic_retains_its_native_roles_and_api_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            _artifacts(workspace)
            analysis = mock.Mock(spec=PydanticRepairAgents)
            registry = SelectableRepairAgents(root / 'state', default_harness='pydantic', analysis_backend=analysis)
            settings = QASettings(api_key='synthetic-key')
            registry.audit(workspace, settings)
            registry.verify(workspace, settings)
            registry.investigate('evidence', settings)
            registry.extract_memories('evidence', settings)
            for name in ('audit', 'verify', 'investigate', 'extract_memories'):
                getattr(analysis, name).assert_called_once()
            self.assertTrue(registry.requires_api_settings)
            self.assertEqual(registry.label, 'Pydantic AI')

    def test_verification_requires_complete_host_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            registry = SelectableRepairAgents(root / 'state', default_harness='codex-sdk')
            with self.assertRaisesRegex(RuntimeError, 'missing verification artifact'):
                registry.verify(workspace, QASettings(api_key=''))

    def test_workflow_status_uses_native_identity_not_spare_api_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = SelectableRepairAgents(Path(directory), default_harness='codex-sdk')
            with mock.patch.object(
                registry,
                'selected_status',
                return_value={
                    'id': 'codex-sdk',
                    'label': 'Codex SDK',
                    'available': True,
                    'model': 'native-model',
                },
            ):
                status = registry.workflow_status(
                    QASettings(api_key='', provider='anthropic', model='unused-api-model')
                )
            self.assertFalse(registry.requires_api_settings)
            self.assertEqual(registry.label, 'Codex SDK')
            self.assertEqual(status['provider'], 'Codex authentication')
            self.assertEqual(status['model'], 'native-model')

    def test_removed_claude_selection_is_not_executable_and_saved_state_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            saved = state / 'agent-harness.json'
            saved.write_text('{"harness":"claude-agent-sdk"}', encoding='utf-8')
            registry = SelectableRepairAgents(state)
            self.assertEqual(registry.harness_id, 'opencode')
            self.assertIn('removed', registry.selection_notice)
            self.assertIn('claude-agent-sdk', saved.read_text(encoding='utf-8'))
            with self.assertRaises(ValueError):
                registry.select_harness('claude-agent-sdk')
            with mock.patch.object(registry, '_probe', side_effect=lambda key: HarnessStatus(key, key, True, 'Test')):
                self.assertEqual(
                    {row['id'] for row in registry.harness_catalog()},
                    {'codex-sdk', 'opencode', 'pydantic', 'google-adk'},
                )
                registry.select_harness('codex-sdk')
            self.assertEqual(registry.selection_notice, '')
            with (
                mock.patch.dict(os.environ, {'AGENT_TRACE_STUDIO_AGENT_TYPE': 'claude-agent-sdk'}),
                self.assertRaises(ValueError),
            ):
                SelectableRepairAgents(state)


class OpenCodeReviewPreflightTests(unittest.TestCase):
    def test_standard_native_credentials_and_empty_account_metadata_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / 'data'
            data.mkdir()
            (data / 'auth.json').write_text(
                json.dumps(
                    {
                        'provider-a': {'type': 'api', 'key': 'SYNTHETIC_PRIVATE_KEY'},
                        'provider-b': {'type': 'oauth', 'access': 'SYNTHETIC_ACCESS', 'refresh': 'SYNTHETIC_REFRESH'},
                    }
                ),
                encoding='utf-8',
            )
            database = data / 'opencode.db'
            # A connection's own context manager commits but does not close it.
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute('CREATE TABLE account (id TEXT, private_token TEXT)')
                connection.execute('CREATE TABLE account_state (active_account_id TEXT, active_org_id TEXT)')
            before = database.read_bytes()
            opencode_review_preflight(native_home=root / 'home', data_dir=data, managed_paths=[])
            self.assertEqual(database.read_bytes(), before)

    def test_remote_and_unknown_auth_configuration_is_refused_without_echoing_secrets(self) -> None:
        for value in (
            {'provider': {'type': 'wellknown', 'key': 'PRIVATE_KEY', 'token': 'PRIVATE_TOKEN'}},
            {'provider': {'type': 'unknown', 'secret': 'PRIVATE_TOKEN'}},
            {'provider': 'PRIVATE_TOKEN'},
            [],
        ):
            with self.subTest(value_type=type(value).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'auth.json').write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(QAUnavailableError) as raised:
                    opencode_review_preflight(native_home=root / 'home', data_dir=root, managed_paths=[])
                self.assertNotIn('PRIVATE', str(raised.exception))

    def test_accounts_and_unknown_database_schema_are_refused_using_presence_only(self) -> None:
        for table in ('account', 'control_account', 'unknown'):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with closing(sqlite3.connect(root / 'opencode.db')) as connection, connection:
                    connection.execute(f'CREATE TABLE {table} (private_token TEXT)')
                    connection.execute(f'INSERT INTO {table} VALUES (?)', ('PRIVATE_TOKEN',))
                with self.assertRaises(QAUnavailableError) as raised:
                    opencode_review_preflight(native_home=root / 'home', data_dir=root, managed_paths=[])
                self.assertNotIn('PRIVATE_TOKEN', str(raised.exception))

    def test_managed_policy_presence_is_refused_without_reading_or_changing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / 'managed.json'
            policy.write_text('PRIVATE_POLICY', encoding='utf-8')
            with self.assertRaisesRegex(QAUnavailableError, 'managed configuration'):
                opencode_review_preflight(native_home=root / 'home', data_dir=root / 'data', managed_paths=[policy])
            self.assertEqual(policy.read_text(encoding='utf-8'), 'PRIVATE_POLICY')

    def test_auth_and_database_symlinks_are_refused(self) -> None:
        for filename in ('auth.json', 'opencode.db'):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / 'synthetic-private'
                target.write_text('PRIVATE_CONTENT', encoding='utf-8')
                (root / filename).symlink_to(target)
                with self.assertRaises(QAUnavailableError) as raised:
                    opencode_review_preflight(native_home=root / 'home', data_dir=root, managed_paths=[])
                self.assertNotIn('PRIVATE_CONTENT', str(raised.exception))


class NativeReviewLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        native_directory = tempfile.TemporaryDirectory(prefix='synthetic-native-home-')
        self.addCleanup(native_directory.cleanup)
        native_home = Path(native_directory.name)
        home = mock.patch('agent_trace_studio.harness_backend.Path.home', return_value=native_home)
        home.start()
        self.addCleanup(home.stop)
        guard = mock.patch('agent_trace_studio.harness_backend.opencode_review_preflight')
        guard.start()
        self.addCleanup(guard.stop)

    def test_opencode_review_refuses_unisolated_home_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'workspace'
            workspace.mkdir()
            native_home = root / 'synthetic-home'
            (native_home / '.opencode/tools').mkdir(parents=True)
            with (
                self.assertRaisesRegex(QAUnavailableError, 'home-directory custom configuration'),
            ):
                opencode_review_preflight(native_home=native_home, data_dir=root / 'data', managed_paths=[])

    def test_opencode_review_rejects_managed_configuration_that_widens_access(self) -> None:
        for override in (
            {'lsp': {}},
            {'instructions': ['PRIVATE_MANAGED_INSTRUCTIONS']},
            {'mcp': {'private': {'enabled': True, 'token': 'PRIVATE_MANAGED_TOKEN'}}},
            {'permission': {'*': 'allow'}},
            {'agent': 'invalid'},
        ):
            with self.subTest(override=list(override)), tempfile.TemporaryDirectory() as directory:
                calls = []

                def run(command, _calls=calls, _override=override, **kwargs):
                    _calls.append(command)
                    if '--version' in command:
                        return subprocess.CompletedProcess(command, 0, '1.18.18', '')
                    config = json.loads(kwargs['env']['OPENCODE_CONFIG_CONTENT'])
                    config.update(_override)
                    return subprocess.CompletedProcess(command, 0, json.dumps(config), '')

                with (
                    mock.patch('agent_trace_studio.harness_backend._run_studio_subprocess', side_effect=run),
                    self.assertRaises(QAUnavailableError) as raised,
                ):
                    _run_opencode_source_review('synthetic request', Path(directory), QASettings(api_key=''))
                self.assertNotIn('PRIVATE_MANAGED', str(raised.exception))
                self.assertEqual(len(calls), 2)
                self.assertFalse(any('run' in command for command in calls))

    def test_codex_review_is_fresh_confined_and_receives_no_provider_key(self) -> None:
        captured = []

        def run(command, **kwargs):
            captured.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, '{"finalResponse":"synthetic result"}', '')

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                'agent_trace_studio.harness_backend._run_studio_subprocess',
                side_effect=run,
            ),
            mock.patch.dict(
                os.environ, {'OPENAI_API_KEY': 'DO_NOT_FORWARD', 'AGENT_TRACE_STUDIO_CODEX_MODEL': 'native'}
            ),
        ):
            _run_codex_source_review('synthetic prompt', Path(directory))
        command, kwargs = captured[0]
        request = json.loads(kwargs['input_text'])
        self.assertTrue(request['confinedReview'])
        self.assertTrue(request['readOnly'])
        self.assertNotIn('threadId', request)
        self.assertNotIn('OPENAI_API_KEY', kwargs['env'])
        self.assertNotIn('DO_NOT_FORWARD', str(captured))
        self.assertTrue(command[1].endswith('codex_turn.mjs'))

    def test_opencode_review_has_a_fresh_deny_by_default_agent_and_external_attachment(self) -> None:
        captured = []

        def run(command, **kwargs):
            if '--version' in command:
                return subprocess.CompletedProcess(command, 0, '1.18.18', '')
            if 'debug' in command:
                return subprocess.CompletedProcess(command, 0, kwargs['env']['OPENCODE_CONFIG_CONTENT'], '')
            attachment = Path(command[command.index('--file') + 1])
            self.assertEqual(attachment.read_text(encoding='utf-8'), 'synthetic prompt')
            if os.name != 'nt':
                self.assertEqual(attachment.stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(attachment.parent, kwargs['cwd'])
            captured.append((command, kwargs, attachment))
            return subprocess.CompletedProcess(command, 0, '{"type":"text","part":{"text":"result"}}', '')

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                'agent_trace_studio.harness_backend._run_studio_subprocess',
                side_effect=run,
            ),
            mock.patch('agent_trace_studio.harness_backend._opencode_binary', return_value=Path('/synthetic/opencode')),
        ):
            for _ in range(2):
                _run_opencode_source_review('synthetic prompt', Path(directory), QASettings(api_key='DO_NOT_FORWARD'))
        names = []
        for command, kwargs, attachment in captured:
            self.assertIn('--pure', command)
            self.assertNotIn('--session', command)
            name = command[command.index('--agent') + 1]
            names.append(name)
            config = json.loads(kwargs['env']['OPENCODE_CONFIG_CONTENT'])
            permissions = config['agent'][name]['permission']
            self.assertFalse(config['lsp'])
            self.assertEqual(kwargs['env']['OPENCODE_DISABLE_PROJECT_CONFIG'], 'true')
            self.assertEqual(kwargs['env']['OPENCODE_DISABLE_EXTERNAL_SKILLS'], 'true')
            self.assertEqual(kwargs['env']['OPENCODE_DISABLE_CLAUDE_CODE'], 'true')
            self.assertEqual(permissions['*'], 'deny')
            for permission in ('bash', 'edit', 'external_directory', 'skill', 'task', 'webfetch', 'websearch'):
                self.assertEqual(permissions[permission], 'deny')
            self.assertEqual(permissions['read']['*.env'], 'deny')
            self.assertFalse(attachment.exists())
            self.assertNotIn('DO_NOT_FORWARD', str(kwargs))
        self.assertNotEqual(*names)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for SDK review isolation tests')
    def test_codex_review_isolation_contract(self) -> None:
        completed = subprocess.run(
            [shutil.which('node') or 'node', str(Path(__file__).with_name('codex_review.test.cjs'))],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
