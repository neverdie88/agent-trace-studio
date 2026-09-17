from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_trace_studio.agent_backend import (
    AgentProgress,
    AgentTransportError,
    InvestigationClaim,
    MemoryCandidate,
    MemoryExtraction,
    ParserAudit,
    ParserAuditIssue,
    SessionCheckpoint,
    SessionCheckpointSummary,
    SessionInvestigation,
    VerificationVerdict,
)
from agent_trace_studio.parser import analyze_journals
from agent_trace_studio.qa import QASettings, TraceQAContext
from agent_trace_studio.repair import (
    DeterministicCheck,
    RepairCoordinator,
    RepairEngine,
    _append_activity,
    _callable_implementation_digest,
    _classify_check_regressions,
    _merge_session_checkpoints,
    _snapshot_digest,
    build_parser_audit_evidence,
)
from agent_trace_studio.workspace import (
    LocalWorkspaceTransaction,
    clone_workspace,
    compare_workspaces,
    restore_workspace_backup,
    snapshot_workspace,
)
from helpers import write_journal


class _LoopAgents:
    label = 'Fake repair agents'

    def __init__(self, actual_parser: Path, *, pass_attempt: int | None) -> None:
        self.actual_parser = actual_parser
        self.pass_attempt = pass_attempt
        self.audit_calls = 0
        self.fix_calls = 0
        self.verify_calls = 0
        self.actual_contents_seen: list[str] = []
        self.workflow_evidence: list[str] = []
        self.workflow_contexts: list[TraceQAContext | None] = []
        self.checkpoint_context = None

    def investigate(
        self,
        evidence: str,
        settings: QASettings,
        *,
        context: TraceQAContext | None = None,
        progress=None,
    ) -> SessionInvestigation:
        self.workflow_evidence.append(evidence)
        self.workflow_contexts.append(context)
        if progress:
            progress(AgentProgress('Model', 'Investigation model is analyzing the evidence.'))
        return SessionInvestigation(
            title='Synthetic session investigation',
            summary='The session exercised the synthetic parser workflow.',
            objective='Verify the selected session behavior.',
            outcome='completed',
            key_actions=['Loaded the trace.'],
            findings=['The selected event was represented.'],
            lessons=['Keep trace evidence bounded.'],
            evidence=[
                InvestigationClaim(
                    claim='The trace was loaded.',
                    evidence_anchors=['[turn turn-1, line 3]'],
                )
            ],
        )

    def extract_memories(
        self,
        evidence: str,
        settings: QASettings,
        *,
        context: TraceQAContext | None = None,
        progress=None,
    ) -> MemoryExtraction:
        self.workflow_evidence.append(evidence)
        self.workflow_contexts.append(context)
        if progress:
            progress(AgentProgress('Model', 'Memory extraction model is analyzing the evidence.'))
        return MemoryExtraction(
            summary='One durable workflow was supported.',
            candidates=[
                MemoryCandidate(
                    category='workflow',
                    title='Bound trace evidence',
                    memory='Use bounded normalized evidence for trace analysis.',
                    why_reusable='It preserves a predictable context boundary.',
                    confidence='high',
                    evidence_anchors=['[turn turn-1, line 3]'],
                )
            ],
        )

    def summarize_checkpoints(
        self,
        evidence: str,
        settings: QASettings,
        *,
        context=None,
        progress=None,
    ) -> SessionCheckpointSummary:
        self.workflow_evidence.append(evidence)
        self.checkpoint_context = context
        if progress:
            progress(AgentProgress('Model', 'Checkpoint model is analyzing the complete session.'))
        return SessionCheckpointSummary(
            title='Synthetic session brief',
            summary='The session completed its synthetic workflow.',
            objective='Exercise session checkpoint extraction.',
            outcome='completed',
            checkpoints=[
                SessionCheckpoint(
                    title='Loaded the selected trace',
                    status='completed',
                    summary='The trace event was normalized.',
                    turn_ids=['turn-1'],
                    start_event_sequence=1,
                    end_event_sequence=3,
                    actions=['Loaded normalized turn evidence.'],
                    achievements=['Confirmed the event was represented.'],
                    blockers=[],
                    artifacts=['Synthetic checkpoint report'],
                    next_steps=[],
                    evidence_anchors=['[turn turn-1, line 3]'],
                )
            ],
        )

    def audit(
        self,
        workspace: Path,
        settings: QASettings,
        *,
        progress=None,
    ) -> ParserAudit:
        self.audit_calls += 1
        if progress:
            progress(AgentProgress('Tool', 'Reading a scoped source file.'))
        return ParserAudit(
            summary='A supported parser omission was found.',
            requires_fix=True,
            confidence='high',
            issues=[
                ParserAuditIssue(
                    severity='medium',
                    title='Synthetic parser omission',
                    evidence='The structural fixture is not represented.',
                    expected_behavior='Normalize the supported fixture.',
                    suggested_test='Add a synthetic parser regression.',
                )
            ],
        )

    def fix(
        self,
        workspace: Path,
        settings: QASettings,
        *,
        attempt: int,
        audit: ParserAudit,
        feedback: dict[str, object] | None,
        progress=None,
    ) -> str:
        self.fix_calls += 1
        if progress:
            progress(AgentProgress('Tool', 'Editing a candidate file in the isolated workspace.'))
        parser = workspace / 'src/agent_trace_studio/parser.py'
        parser.write_text(f'REPAIR_ATTEMPT = {attempt}\n', encoding='utf-8')
        test = workspace / 'tests/test_regression.py'
        test.write_text(f'ATTEMPT = {attempt}\n', encoding='utf-8')
        return f'Changed parser and regression test on attempt {attempt}.'

    def verify(self, workspace: Path, settings: QASettings, *, progress=None) -> VerificationVerdict:
        self.verify_calls += 1
        if progress:
            progress(AgentProgress('Model', 'Verification model is analyzing the evidence.'))
        self.actual_contents_seen.append(self.actual_parser.read_text(encoding='utf-8'))
        if self.verify_calls == self.pass_attempt:
            return VerificationVerdict(
                status='pass',
                summary='Independent verification passed.',
                fixed_items=['Synthetic parser omission'],
            )
        return VerificationVerdict(
            status='fail',
            summary='The regression is not complete.',
            unresolved_issues=['Synthetic omission remains.'],
            required_changes=['Revise the parser and regression test.'],
        )


class _NoProgressAgents(_LoopAgents):
    def fix(
        self,
        workspace: Path,
        settings: QASettings,
        *,
        attempt: int,
        audit: ParserAudit,
        feedback: dict[str, object] | None,
        progress=None,
    ) -> str:
        self.fix_calls += 1
        if progress:
            progress(AgentProgress('Tool', 'Editing a candidate file in the isolated workspace.'))
        (workspace / 'src/agent_trace_studio/parser.py').write_text('UNCHANGED_REPAIR = True\n', encoding='utf-8')
        return 'Repeated the same change.'


class _ControlRemovingAgents(_LoopAgents):
    def __init__(self, actual_parser: Path) -> None:
        super().__init__(actual_parser, pass_attempt=1)
        self.verification_artifacts: dict[str, str] = {}

    def fix(
        self,
        workspace: Path,
        settings: QASettings,
        *,
        attempt: int,
        audit: ParserAudit,
        feedback: dict[str, object] | None,
        progress=None,
    ) -> str:
        summary = super().fix(
            workspace,
            settings,
            attempt=attempt,
            audit=audit,
            feedback=feedback,
            progress=progress,
        )
        shutil.rmtree(workspace / '.agent-trace-studio')
        return summary

    def verify(self, workspace: Path, settings: QASettings, *, progress=None) -> VerificationVerdict:
        control = workspace / '.agent-trace-studio'
        self.verification_artifacts = {
            path.name: path.read_text(encoding='utf-8') for path in sorted(control.iterdir()) if path.is_file()
        }
        return super().verify(workspace, settings, progress=progress)


class _PolicyRetryAgents(_LoopAgents):
    def __init__(self, actual_parser: Path) -> None:
        super().__init__(actual_parser, pass_attempt=1)
        self.feedback_seen: list[dict[str, object] | None] = []

    def fix(
        self,
        workspace: Path,
        settings: QASettings,
        *,
        attempt: int,
        audit: ParserAudit,
        feedback: dict[str, object] | None,
        progress=None,
    ) -> str:
        self.fix_calls += 1
        self.feedback_seen.append(feedback)
        if attempt == 1:
            (workspace / 'src/agent_trace_studio/cli.py').write_text(
                'PROTECTED_CHANGE = True\n',
                encoding='utf-8',
            )
            return 'Changed the protected CLI title surface.'
        (workspace / 'src/agent_trace_studio/parser.py').write_text(
            f'REPAIR_ATTEMPT = {attempt}\n',
            encoding='utf-8',
        )
        (workspace / 'tests/test_regression.py').write_text(f'ATTEMPT = {attempt}\n', encoding='utf-8')
        return 'Moved the change to permitted source and test surfaces.'


class _ResumableFixerSession:
    def __init__(self, owner: _ResumableAgents, workspace: Path, settings: QASettings) -> None:
        self.owner = owner
        self.workspace = workspace
        self.settings = settings

    def fix(
        self,
        *,
        attempt: int,
        audit: ParserAudit,
        feedback: dict[str, object] | None,
        user_instruction: str | None = None,
        resume_pending_turn: bool = False,
        progress=None,
    ) -> str:
        self.owner.fixer_requests.append(
            {
                'attempt': attempt,
                'feedback': feedback,
                'user_instruction': user_instruction,
                'resume_pending_turn': resume_pending_turn,
            }
        )
        if not self.owner.transport_stopped:
            self.owner.transport_stopped = True
            raise AgentTransportError(kind='timed_out', retries=2, role='repair')
        return self.owner.fix(
            self.workspace,
            self.settings,
            attempt=attempt,
            audit=audit,
            feedback=feedback,
            progress=progress,
        )


class _ResumableAgents(_LoopAgents):
    def __init__(self, actual_parser: Path) -> None:
        super().__init__(actual_parser, pass_attempt=1)
        self.transport_stopped = False
        self.session_ids: list[str] = []
        self.fixer_requests: list[dict[str, object]] = []

    def create_fixer_session(
        self,
        workspace: Path,
        settings: QASettings,
        *,
        run_id: str,
        state_dir: Path,
    ) -> _ResumableFixerSession:
        self.session_ids.append(f'{run_id}:{state_dir.name}')
        return _ResumableFixerSession(self, workspace, settings)


class _VerifierPauseAgents(_LoopAgents):
    def verify(self, workspace: Path, settings: QASettings, *, progress=None) -> VerificationVerdict:
        self.verify_calls += 1
        if self.verify_calls == 1:
            raise AgentTransportError(kind='timed_out', retries=2, role='verify')
        return VerificationVerdict(
            status='pass',
            summary='Independent verification passed.',
            fixed_items=['Synthetic parser omission'],
        )


class _AlreadySatisfiedAgents(_LoopAgents):
    def fix(
        self,
        workspace: Path,
        settings: QASettings,
        *,
        attempt: int,
        audit: ParserAudit,
        feedback: dict[str, object] | None,
        progress=None,
    ) -> str:
        self.fix_calls += 1
        return 'The requested dashboard state is already present; no source edit is needed.'

    def verify(self, workspace: Path, settings: QASettings, *, progress=None) -> VerificationVerdict:
        self.verify_calls += 1
        return VerificationVerdict(
            status='pass',
            summary='The unchanged dashboard already satisfies the explicit request.',
            already_satisfied=True,
            fixed_items=['Requested dashboard state is already present.'],
        )


class RepairEngineTest(unittest.TestCase):
    def test_every_native_workflow_uses_native_auth_and_records_native_identity(self) -> None:
        for backend in ('codex-sdk', 'opencode'):
            for kind in ('investigate', 'memories', 'checkpoints', 'audit', 'repair', 'customize'):
                with self.subTest(backend=backend, kind=kind), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = _write_source_checkout(root / 'source')
                    journal = root / 'synthetic.jsonl'
                    write_journal(journal, session_id='native-workflow')
                    result = analyze_journals([journal], include_trace=True)
                    agents = _LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=1)
                    agents.requires_api_settings = False
                    agents.harness_id = backend
                    agents.workflow_status = lambda _settings, selected=backend: {
                        'id': selected,
                        'label': selected,
                        'available': True,
                        'provider': 'native authentication',
                        'model': 'native-model',
                    }
                    coordinator = RepairCoordinator(source_workspace=source, state_dir=root / 'state', agents=agents)
                    dashboard_state = {
                        'source': {'session_id': 'native-workflow'},
                        'selection': {'turn_id': 'turn-1', 'event_sequence': None},
                        'view': {'active_tab': 'trace'},
                    }
                    settings = QASettings(api_key='', provider='anthropic', model='unused-api-model')
                    with mock.patch('agent_trace_studio.repair.threading.Thread'):
                        if kind in {'repair', 'customize'}:
                            with self.assertRaisesRegex(ValueError, 'approved one-time source action'):
                                coordinator.start(
                                    kind,
                                    result=result,
                                    dashboard_state=dashboard_state,
                                    settings=settings,
                                    instruction='Make a synthetic change.',
                                )
                            approval = coordinator.prepare_source_action(
                                'repair_parser' if kind == 'repair' else 'customize_dashboard',
                                result=result,
                                dashboard_state=dashboard_state,
                                instruction='Make a synthetic change.',
                                client_session_nonce='synthetic-client-session-123456',
                            )
                            started = coordinator.approve_source_action(
                                authorization_id=str(approval['id']),
                                token=str(approval['token']),
                                client_session_nonce='synthetic-client-session-123456',
                                result=result,
                                settings=settings,
                            )
                        else:
                            started = coordinator.start(
                                kind,
                                result=result,
                                dashboard_state=dashboard_state,
                                settings=settings,
                            )
                    self.assertEqual(started['harness'], backend)
                    self.assertEqual(started['provider'], 'native authentication')
                    self.assertEqual(started['model'], 'native-model')
                    self.assertFalse(coordinator.status()['requires_api_settings'])
                    coordinator.close()

    def test_pydantic_and_unavailable_native_backends_gate_every_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'synthetic.jsonl'
            write_journal(journal)
            result = analyze_journals([journal], include_trace=True)
            agents = _LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=1)
            coordinator = RepairCoordinator(source_workspace=source, state_dir=root / 'state', agents=agents)
            for requires_api, error in ((True, 'Configure an API'), (False, 'Native login required')):
                agents.requires_api_settings = requires_api
                agents.workflow_status = lambda _settings: {'available': False, 'detail': 'Native login required'}
                for kind in ('investigate', 'memories', 'checkpoints', 'audit', 'repair', 'customize'):
                    with self.subTest(requires_api=requires_api, kind=kind), self.assertRaisesRegex(ValueError, error):
                        coordinator.start(kind, result=result, dashboard_state={}, settings=QASettings(api_key=''))
            coordinator.close()

    def test_removed_backend_run_continues_only_after_approval_with_native_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'synthetic.jsonl'
            write_journal(journal, session_id='legacy-run')
            result = analyze_journals([journal], include_trace=True)
            agents = _LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=1)
            agents.harness_id = 'codex-sdk'
            agents.requires_api_settings = False
            agents.workflow_status = lambda _settings: {
                'available': True,
                'id': 'codex-sdk',
                'label': 'Codex SDK',
                'provider': 'Codex authentication',
                'model': 'native-model',
            }
            coordinator = RepairCoordinator(source_workspace=source, state_dir=root / 'state', agents=agents)
            payload = _run_payload('repair', session_id='legacy-run')
            payload.update(
                {
                    'run_id': 'legacyrun1',
                    'status': 'paused',
                    'pending_model_turn': True,
                    'harness': 'claude-agent-sdk',
                    'provider': 'legacy-provider',
                    'model': 'legacy-model',
                    'dashboard_state': {
                        'source': {'session_id': 'legacy-run'},
                        'selection': {'turn_id': 'turn-1', 'event_sequence': None},
                        'view': {'active_tab': 'trace'},
                    },
                    'recovery': {'can_continue': True, 'workspace_preserved': True},
                }
            )
            coordinator.store.save(payload)
            (root / 'state/workspaces/legacyrun1').mkdir(parents=True)
            (root / 'state/baselines/legacyrun1').mkdir(parents=True)
            approval = coordinator.prepare_run_action(
                'legacyrun1',
                action='continue',
                result=result,
                instruction=None,
                client_session_nonce='synthetic-client-session-123456',
            )
            with mock.patch('agent_trace_studio.repair.threading.Thread'):
                continued = coordinator.approve_source_action(
                    authorization_id=str(approval['id']),
                    token=str(approval['token']),
                    client_session_nonce='synthetic-client-session-123456',
                    result=result,
                    settings=QASettings(api_key=''),
                )
            self.assertEqual(continued['harness'], 'codex-sdk')
            self.assertEqual(continued['model'], 'native-model')
            self.assertFalse(continued['pending_model_turn'])
            self.assertEqual(continued['fallbacks'][0]['from_harness'], 'claude-agent-sdk')
            self.assertEqual(continued['fallbacks'][0]['to_harness'], 'codex-sdk')
            coordinator.close()

    def test_coordinator_starts_session_brief_without_selected_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-no-turn')
            result = analyze_journals([journal], include_trace=True)
            coordinator = RepairCoordinator(
                source_workspace=None,
                state_dir=root / 'state',
                agents=_LoopAgents(root / 'unused-parser.py', pass_attempt=None),
            )
            started = coordinator.start(
                'checkpoints',
                result=result,
                dashboard_state={
                    'source': {'session_id': 'session-no-turn'},
                    'selection': {'turn_id': None, 'event_sequence': None},
                    'view': {'active_tab': 'trace'},
                },
                settings=QASettings(api_key='test-key', model='test-model'),
            )
            deadline = time.monotonic() + 5
            completed = coordinator.get(str(started['run_id']))
            while completed['status'] != 'summarized' and time.monotonic() < deadline:
                time.sleep(0.01)
                completed = coordinator.get(str(started['run_id']))
            coordinator.close()

        self.assertEqual(completed['status'], 'summarized')
        self.assertEqual(completed['result']['scope'], 'session')

    def test_external_harness_can_start_session_brief_without_model_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-harness-auth')
            result = analyze_journals([journal], include_trace=True)
            agents = _LoopAgents(root / 'unused-parser.py', pass_attempt=None)
            agents.checkpoint_requires_api_settings = False
            coordinator = RepairCoordinator(source_workspace=None, state_dir=root / 'state', agents=agents)
            started = coordinator.start(
                'checkpoints',
                result=result,
                dashboard_state={
                    'source': {'session_id': 'session-harness-auth'},
                    'selection': {'turn_id': None, 'event_sequence': None},
                    'view': {'active_tab': 'trace'},
                },
                settings=QASettings(api_key=''),
            )
            deadline = time.monotonic() + 5
            completed = coordinator.get(str(started['run_id']))
            while completed['status'] != 'summarized' and time.monotonic() < deadline:
                time.sleep(0.01)
                completed = coordinator.get(str(started['run_id']))
            coordinator.close()

        self.assertEqual(completed['status'], 'summarized')
        self.assertEqual(completed['result']['scope'], 'session')

    def test_session_brief_persists_and_reports_new_live_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-brief')
            result = analyze_journals([journal], include_trace=True)
            agents = _LoopAgents(root / 'unused-parser.py', pass_attempt=None)
            coordinator = RepairCoordinator(source_workspace=None, state_dir=root / 'state', agents=agents)
            payload = _run_payload('checkpoints', session_id='session-brief')
            payload['run_id'] = 'sessionbriefrun1'
            coordinator.engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=coordinator.store.save,
            )

            current = coordinator.session_brief(result, 'session-brief')
            with journal.open('a', encoding='utf-8') as stream:
                stream.write(
                    json.dumps(
                        {
                            'timestamp': '2026-08-10T01:00:15Z',
                            'type': 'event_msg',
                            'payload': {'type': 'agent_message', 'message': 'A new live result arrived.'},
                        }
                    )
                    + '\n'
                )
            refreshed_result = analyze_journals([journal], include_trace=True)
            stale = coordinator.session_brief(refreshed_result, 'session-brief')
            update_payload = _run_payload('checkpoints', session_id='session-brief')
            update_payload.update(
                {
                    'run_id': 'sessionbriefrun2',
                    'previous_session_brief': payload['result'],
                    'previous_brief_run_id': payload['run_id'],
                    'session_brief_revision': 2,
                }
            )
            coordinator.engine.run(
                update_payload,
                result=refreshed_result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=coordinator.store.save,
            )
            evidence_calls = len(agents.workflow_evidence)
            unchanged_payload = _run_payload('checkpoints', session_id='session-brief')
            unchanged_payload.update(
                {
                    'run_id': 'sessionbriefrun3',
                    'previous_session_brief': update_payload['result'],
                    'previous_brief_run_id': update_payload['run_id'],
                    'session_brief_revision': 3,
                }
            )
            coordinator.engine.run(
                unchanged_payload,
                result=refreshed_result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=coordinator.store.save,
            )
            journal.write_text(
                journal.read_text(encoding='utf-8').replace('Done', 'Rewritten result'), encoding='utf-8'
            )
            rewritten_result = analyze_journals([journal], include_trace=True)
            rewritten = coordinator.session_brief(rewritten_result, 'session-brief')
            coordinator.close()

        self.assertTrue(current['available'])
        self.assertEqual(current['status'], 'current')
        self.assertFalse(current['stale'])
        self.assertEqual(current['result']['scope'], 'session')
        self.assertEqual(stale['status'], 'out_of_date')
        self.assertEqual(stale['new_event_count'], 1)
        self.assertIn('PREVIOUS PERSISTED SESSION BRIEF', agents.workflow_evidence[-1])
        self.assertEqual(update_payload['result']['revision'], 2)
        self.assertEqual(update_payload['result']['appended_event_count'], 1)
        self.assertEqual(len(update_payload['result']['checkpoints']), 1)
        self.assertEqual(len(agents.workflow_evidence), evidence_calls)
        self.assertEqual(unchanged_payload['result']['revision'], 2)
        self.assertTrue(rewritten['stale'])
        self.assertTrue(rewritten['rebuild_required'])
        self.assertEqual(rewritten['new_event_count'], len(rewritten_result.traces[0].events))

    def test_session_checkpoint_merge_preserves_sealed_history_and_replaces_open_work(self) -> None:
        previous = {
            'checkpoints': [
                {
                    'title': 'Prepared the workspace',
                    'status': 'completed',
                    'start_event_sequence': 1,
                    'end_event_sequence': 4,
                },
                {
                    'title': 'Ran verification',
                    'status': 'in_progress',
                    'start_event_sequence': 5,
                    'end_event_sequence': 8,
                },
            ]
        }
        current = {
            'checkpoints': [
                {
                    'title': 'Ran verification',
                    'status': 'completed',
                    'start_event_sequence': 5,
                    'end_event_sequence': 12,
                },
                {
                    'title': 'Delivered the result',
                    'status': 'completed',
                    'start_event_sequence': 13,
                    'end_event_sequence': 15,
                },
            ]
        }

        merged = _merge_session_checkpoints(previous, current)

        self.assertEqual(
            [checkpoint['title'] for checkpoint in merged],
            ['Prepared the workspace', 'Ran verification', 'Delivered the result'],
        )
        self.assertEqual(merged[1]['status'], 'completed')

    def test_source_stale_failure_is_retired_and_all_pending_deployments_are_listed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            coordinator = RepairCoordinator(
                source_workspace=source,
                state_dir=root / 'state',
                agents=_LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=None),
            )
            historical = _run_payload('customize', session_id='historical')
            historical.update(
                {
                    'run_id': 'historicalrun1',
                    'status': 'failed',
                    'source_snapshot_digest': '0' * 64,
                    'updated_at': '2026-08-17T00:00:01+00:00',
                }
            )
            coordinator.store.save(historical)

            status = coordinator.status()
            public = coordinator.public_get('historicalrun1')

            for index in (1, 2):
                pending = _run_payload('repair', session_id=f'pending-{index}')
                pending.update(
                    {
                        'run_id': f'pendingrun{index}',
                        'status': 'passed',
                        'updated_at': f'2026-08-17T00:00:0{index + 1}+00:00',
                        'result': {'restart_required': True, 'change_digest': str(index) * 64},
                    }
                )
                coordinator.store.save(pending)
            pending_ids = [str(item['run_id']) for item in coordinator.pending_deployments()]
            coordinator.close()

        self.assertIsNone(status['latest_run'])
        self.assertEqual(status['latest_historical_run']['run_id'], 'historicalrun1')
        self.assertTrue(public['historical'])
        self.assertIsNone(public['recovery'])
        self.assertEqual(pending_ids, ['pendingrun1', 'pendingrun2'])

    def test_customization_can_finish_when_verifier_confirms_it_is_already_satisfied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-noop-customization')
            result = analyze_journals([journal], include_trace=True)
            agents = _AlreadySatisfiedAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=None)
            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=agents,
                check_runner=_passing_checks,
                probe_runner=_probe,
            )
            payload = _run_payload('customize', session_id='session-noop-customization')
            payload['customization_request'] = 'Keep the dashboard title as Agent Trace Studio.'

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

        self.assertEqual(payload['status'], 'passed')
        self.assertEqual(payload['result']['changed_files'], [])
        self.assertTrue(payload['result']['already_satisfied'])
        self.assertFalse(payload['result']['restart_required'])

    def test_failed_candidate_keeps_same_fixer_session_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            state_dir = root / 'state'
            coordinator = RepairCoordinator(
                source_workspace=source,
                state_dir=state_dir,
                agents=_LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=1),
                deployment_managed=True,
            )
            payload = _run_payload('repair', session_id='session-deployment')
            payload['run_id'] = 'deploymentrun1'
            payload['status'] = 'passed'
            payload['result'] = {'restart_required': True}
            coordinator.store.save(payload)
            (state_dir / 'workspaces/deploymentrun1').mkdir(parents=True)
            (state_dir / 'baselines/deploymentrun1').mkdir(parents=True)

            try:
                updated = coordinator.record_deployment_result(
                    'deploymentrun1',
                    {
                        'status': 'failed',
                        'message': 'Candidate health check failed.',
                        'rollback': {'status': 'restored'},
                    },
                )
            finally:
                coordinator.close()

        self.assertEqual(updated['status'], 'paused')
        self.assertTrue(updated['recovery']['can_continue'])
        self.assertEqual(updated['recovery']['actions'], ['continue', 'restart', 'discard'])
        self.assertEqual(updated['last_feedback']['reason'], 'runtime_activation_failed')

    def test_trace_analysis_workflows_run_without_source_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-insights')
            result = analyze_journals([journal], include_trace=True)
            agents = _LoopAgents(root / 'unused-parser.py', pass_attempt=None)
            engine = RepairEngine(
                source_workspace=None,
                state_dir=root / 'state',
                agents=agents,
                check_runner=_passing_checks,
                probe_runner=_probe,
            )

            investigation = _run_payload('investigate', session_id='session-insights')
            engine.run(
                investigation,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )
            memories = _run_payload('memories', session_id='session-insights')
            engine.run(
                memories,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )
            checkpoints = _run_payload('checkpoints', session_id='session-insights')
            engine.run(
                checkpoints,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            self.assertEqual(investigation['status'], 'investigated')
            self.assertEqual(investigation['result']['workflow'], 'investigation')
            self.assertEqual(investigation['result']['title'], 'Synthetic session investigation')
            self.assertEqual(memories['status'], 'extracted')
            self.assertEqual(memories['result']['workflow'], 'memories')
            self.assertEqual(len(memories['result']['candidates']), 1)
            self.assertEqual(checkpoints['status'], 'summarized')
            self.assertEqual(checkpoints['result']['workflow'], 'checkpoints')
            self.assertEqual(checkpoints['result']['scope'], 'session')
            self.assertEqual(checkpoints['result']['event_count'], result.traces[0].events_total)
            self.assertEqual(checkpoints['result']['revision'], 1)
            self.assertFalse(checkpoints['result']['event_details_sampled'])
            self.assertEqual(len(checkpoints['result']['checkpoints']), 1)
            self.assertIn('CURRENT DASHBOARD SELECTION', agents.workflow_evidence[0])
            self.assertIn('journal_evidence_preloaded: false', agents.workflow_evidence[0])
            self.assertNotIn('[turn turn-1,', agents.workflow_evidence[0])
            self.assertNotIn(str(journal), agents.workflow_evidence[0])
            self.assertEqual([context.scope for context in agents.workflow_contexts if context], ['session', 'session'])
            self.assertIn('SESSION CHECKPOINT EVIDENCE', agents.workflow_evidence[2])
            self.assertIn('[turn turn-1,', agents.workflow_evidence[2])
            self.assertIn('[turn turn-2,', agents.workflow_evidence[2])
            self.assertIsNotNone(agents.checkpoint_context)
            self.assertEqual(agents.checkpoint_context.session_id, 'session-insights')
            self.assertEqual(agents.checkpoint_context.scope, 'session')
            self.assertIs(agents.checkpoint_context.result, result)
            self.assertIn('Session investigation completed.', [item['message'] for item in investigation['activity']])
            self.assertIn('Extracted 1 memory candidate.', [item['message'] for item in memories['activity']])
            self.assertTrue(
                any(
                    item['message'].startswith('Updated the session brief through ') for item in checkpoints['activity']
                )
            )

    def test_dashboard_customization_uses_verified_source_change_loop_without_parser_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-customize')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            agents = _LoopAgents(parser, pass_attempt=1)
            state_dir = root / 'state'
            engine = RepairEngine(
                source_workspace=source,
                state_dir=state_dir,
                agents=agents,
                deployment_managed=True,
                check_runner=_passing_checks,
                probe_runner=_probe,
            )
            payload = _run_payload('customize', session_id='session-customize')
            payload['customization_request'] = 'Make the trace detail panel wider.'

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
                user_instruction='Make the trace detail panel wider.',
            )

            self.assertEqual(payload['status'], 'passed')
            self.assertEqual(agents.audit_calls, 0)
            self.assertEqual(agents.fix_calls, 1)
            self.assertTrue(payload['result']['change_digest'])
            self.assertEqual(payload['result']['source_delta']['modified'], ['src/agent_trace_studio/parser.py'])
            self.assertIn('src/agent_trace_studio/parser.py', payload['result']['applied_records'])
            self.assertTrue((state_dir / 'workspaces/run-session-customize').is_dir())
            self.assertIn('Make the trace detail panel wider.', payload['audit']['summary'])
            self.assertIn(
                'Prepared the explicit customization task for the coding agent.',
                [item['message'] for item in payload['activity']],
            )

    def test_workspace_policy_rejection_returns_feedback_to_same_fixer_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            protected_cli = source / 'src/agent_trace_studio/cli.py'
            protected_cli.write_text('TRUSTED = True\n', encoding='utf-8')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-policy-retry')
            result = analyze_journals([journal], include_trace=True)
            agents = _PolicyRetryAgents(source / 'src/agent_trace_studio/parser.py')
            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=agents,
                max_attempts=3,
                check_runner=_passing_checks,
                probe_runner=_probe,
            )
            payload = _run_payload('customize', session_id='session-policy-retry')
            payload['customization_request'] = 'Always use Agent Trace Studio as the product title.'

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
                user_instruction='Always use Agent Trace Studio as the product title.',
            )

            self.assertEqual(payload['status'], 'passed')
            self.assertEqual(payload['attempt'], 2)
            self.assertEqual(agents.fix_calls, 2)
            self.assertEqual(agents.verify_calls, 1)
            self.assertIsNone(agents.feedback_seen[0])
            self.assertEqual(agents.feedback_seen[1]['reason'], 'workspace_policy_rejected')
            self.assertEqual(protected_cli.read_text(encoding='utf-8'), 'TRUSTED = True\n')
            self.assertEqual(len(payload['attempts']), 2)
            rejected = payload['attempts'][0]
            self.assertEqual(rejected['verifier']['status'], 'skipped')
            self.assertEqual(rejected['policy_violation']['category'], 'protected_runtime_file')
            self.assertEqual(
                rejected['policy_violation']['blocked_files'],
                ['src/agent_trace_studio/cli.py'],
            )
            self.assertEqual(rejected['checks'][0]['name'], 'Workspace policy')
            self.assertFalse(rejected['deterministic_gates_passed'])
            self.assertIn(
                'Returning feedback to the fixer.',
                next(item['message'] for item in payload['activity'] if item['phase'] == 'Policy'),
            )

    def test_continue_classifies_preserved_policy_violation_before_resuming_fixer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            protected_cli = source / 'src/agent_trace_studio/cli.py'
            protected_cli.write_text('TRUSTED = True\n', encoding='utf-8')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-policy-continue')
            result = analyze_journals([journal], include_trace=True)
            state_dir = root / 'state'
            run_id = 'preserved-policy-run'
            baseline = state_dir / 'baselines' / run_id
            shadow = state_dir / 'workspaces' / run_id
            clone_workspace(source, baseline)
            clone_workspace(baseline, shadow)
            (state_dir / 'runs' / run_id).mkdir(parents=True)
            (shadow / 'src/agent_trace_studio/cli.py').write_text(
                'PROTECTED_CHANGE = True\n',
                encoding='utf-8',
            )
            agents = _PolicyRetryAgents(source / 'src/agent_trace_studio/parser.py')
            engine = RepairEngine(
                source_workspace=source,
                state_dir=state_dir,
                agents=agents,
                max_attempts=3,
                check_runner=_passing_checks,
                probe_runner=_probe,
            )
            payload = _run_payload('customize', session_id='session-policy-continue')
            payload.update(
                {
                    'run_id': run_id,
                    'status': 'failed',
                    'attempt': 1,
                    'resume_from': 'repair',
                    'pending_model_turn': False,
                    'customization_request': 'Always use Agent Trace Studio as the product title.',
                }
            )

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
                resume=True,
            )

            self.assertEqual(payload['status'], 'passed')
            self.assertEqual(payload['attempt'], 2)
            self.assertEqual(agents.fix_calls, 1)
            self.assertEqual(agents.feedback_seen[0]['reason'], 'workspace_policy_rejected')
            self.assertEqual(payload['attempts'][0]['attempt'], 1)
            self.assertEqual(payload['attempts'][0]['verifier']['status'], 'skipped')
            self.assertEqual(protected_cli.read_text(encoding='utf-8'), 'TRUSTED = True\n')

    def test_failed_clone_removes_partial_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            state_dir = root / 'state'
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-clone-failure')
            result = analyze_journals([journal], include_trace=True)
            engine = RepairEngine(
                source_workspace=source,
                state_dir=state_dir,
                agents=_LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=None),
                check_runner=_passing_checks,
                probe_runner=_probe,
            )
            payload = _run_payload('audit', session_id='session-clone-failure')

            def fail_clone(_source: Path, destination: Path) -> None:
                destination.mkdir(parents=True)
                (destination / 'partial.txt').write_text('partial\n', encoding='utf-8')
                raise OSError('synthetic copy failure')

            with (
                mock.patch('agent_trace_studio.repair.clone_workspace', side_effect=fail_clone),
                self.assertRaisesRegex(OSError, 'synthetic copy failure'),
            ):
                engine.run(
                    payload,
                    result=result,
                    source_path=journal,
                    settings=QASettings(api_key='test-key', model='test-model'),
                    cancel_event=threading.Event(),
                    update=lambda _payload: None,
                )

            self.assertFalse((state_dir / 'workspaces/run-session-clone-failure').exists())

    def test_audit_ignores_state_directory_nested_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            state_dir = source / '.dashboard-agent-state'
            stale = state_dir / 'workspaces/stale/.dashboard-agent-state/workspaces/stale'
            stale.mkdir(parents=True)
            (stale / 'runtime.txt').write_text('generated runtime data\n', encoding='utf-8')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-audit-state')
            result = analyze_journals([journal], include_trace=True)
            agents = _LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=None)
            engine = RepairEngine(
                source_workspace=source,
                state_dir=state_dir,
                agents=agents,
                check_runner=_passing_checks,
                probe_runner=_probe,
            )
            payload = _run_payload('audit', session_id='session-audit-state')

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            self.assertEqual(payload['status'], 'audited')
            messages = [item['message'] for item in payload['activity']]
            self.assertIn('Capturing the local source snapshot.', messages)
            self.assertIn('Reading a scoped source file.', messages)
            self.assertEqual(messages[-1], 'Audit found 1 supported issue.')
            self.assertFalse((state_dir / 'workspaces/run-session-audit-state').exists())

    def test_repair_reuses_completed_audit_for_unchanged_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-seeded-audit')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            agents = _LoopAgents(parser, pass_attempt=1)
            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=agents,
                check_runner=_passing_checks,
                probe_runner=_probe,
            )
            settings = QASettings(api_key='test-key', model='test-model')
            audit_payload = _run_payload('audit', session_id='session-seeded-audit')
            audit_payload['run_id'] = 'auditseed1'
            engine.run(
                audit_payload,
                result=result,
                source_path=journal,
                settings=settings,
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )
            repair_payload = _run_payload('repair', session_id='session-seeded-audit')
            repair_payload.update(
                {
                    'run_id': 'repairseed1',
                    'audit': audit_payload['audit'],
                    'seed_audit_run_id': audit_payload['run_id'],
                    'seed_audit_source_digest': audit_payload['source_snapshot_digest'],
                }
            )

            engine.run(
                repair_payload,
                result=result,
                source_path=journal,
                settings=settings,
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            self.assertEqual(agents.audit_calls, 1)
            self.assertEqual(repair_payload['status'], 'passed')
            self.assertIn(
                'Using the completed audit for the unchanged source snapshot.',
                [item['message'] for item in repair_payload['activity']],
            )

    def test_repair_reaudits_when_source_changed_after_completed_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-stale-audit')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            agents = _LoopAgents(parser, pass_attempt=1)
            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=agents,
                check_runner=_passing_checks,
                probe_runner=_probe,
            )
            settings = QASettings(api_key='test-key', model='test-model')
            audit_payload = _run_payload('audit', session_id='session-stale-audit')
            audit_payload['run_id'] = 'auditstale1'
            engine.run(
                audit_payload,
                result=result,
                source_path=journal,
                settings=settings,
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )
            (source / 'tests/test_existing.py').write_text('USER_CHANGE = True\n', encoding='utf-8')
            repair_payload = _run_payload('repair', session_id='session-stale-audit')
            repair_payload.update(
                {
                    'run_id': 'repairstale1',
                    'audit': audit_payload['audit'],
                    'seed_audit_run_id': audit_payload['run_id'],
                    'seed_audit_source_digest': audit_payload['source_snapshot_digest'],
                }
            )

            engine.run(
                repair_payload,
                result=result,
                source_path=journal,
                settings=settings,
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            self.assertEqual(agents.audit_calls, 2)
            self.assertEqual(repair_payload['status'], 'passed')
            self.assertIn(
                'The source changed after the completed audit; running a fresh audit.',
                [item['message'] for item in repair_payload['activity']],
            )

    def test_retries_until_verifier_passes_then_applies_local_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-repair')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            original = parser.read_text(encoding='utf-8')
            agents = _LoopAgents(parser, pass_attempt=2)
            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=agents,
                max_attempts=5,
                check_runner=_passing_checks,
                probe_runner=_probe,
            )
            payload = _run_payload('repair', session_id='session-repair')

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            self.assertEqual(payload['status'], 'passed')
            self.assertEqual(payload['attempt'], 2)
            self.assertEqual(agents.fix_calls, 2)
            self.assertEqual(agents.verify_calls, 2)
            self.assertEqual(agents.actual_contents_seen, [original, original])
            self.assertEqual(parser.read_text(encoding='utf-8'), 'REPAIR_ATTEMPT = 2\n')
            self.assertEqual(
                payload['repair_summary']['fixed_items'],
                [{'attempt': 2, 'item': 'Synthetic parser omission'}],
            )
            self.assertNotIn('latest_repair', payload['repair_summary'])
            self.assertNotIn('latest_verification', payload['repair_summary'])
            self.assertNotIn('verification_failures', payload['repair_summary'])
            self.assertTrue(payload['result']['restart_required'])
            self.assertTrue(Path(payload['result']['backup_path']).is_dir())

    def test_host_restores_complete_verification_artifacts_after_fixer_removes_control_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-artifacts')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            agents = _ControlRemovingAgents(parser)
            state_dir = root / 'state'
            engine = RepairEngine(
                source_workspace=source,
                state_dir=state_dir,
                agents=agents,
                check_runner=_passing_checks,
                probe_runner=_probe,
            )
            payload = _run_payload('repair', session_id='session-artifacts')

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            self.assertEqual(payload['status'], 'passed')
            self.assertEqual(
                set(agents.verification_artifacts),
                {'audit-evidence.json', 'audit-report.json', 'change.patch', 'verification.json'},
            )
            verification = json.loads(agents.verification_artifacts['verification.json'])
            self.assertTrue(verification['deterministic_gates_passed'])
            self.assertEqual(verification['checks'][0]['status'], 'passed')
            self.assertEqual(verification['parser_after']['session_id'], 'session-artifacts')
            self.assertIn('tests/test_regression.py', agents.verification_artifacts['change.patch'])

            retained = json.loads(
                (state_dir / 'runs/run-session-artifacts/attempt-1-verification.json').read_text(encoding='utf-8')
            )
            self.assertEqual(retained['verifier']['status'], 'pass')
            self.assertTrue((state_dir / 'runs/run-session-artifacts/audit-report.json').is_file())
            self.assertTrue((state_dir / 'runs/run-session-artifacts/attempt-1.patch').is_file())

    def test_transport_stop_preserves_checkpoint_and_continue_resumes_same_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-resume')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            agents = _ResumableAgents(parser)
            state_dir = root / 'state'
            engine = RepairEngine(
                source_workspace=source,
                state_dir=state_dir,
                agents=agents,
                check_runner=_passing_checks,
                probe_runner=_probe,
            )
            payload = _run_payload('repair', session_id='session-resume')
            settings = QASettings(api_key='test-key', model='test-model')

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=settings,
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            self.assertEqual(payload['status'], 'paused')
            self.assertEqual(payload['resume_from'], 'repair')
            self.assertTrue(payload['pending_model_turn'])
            self.assertEqual(payload['recovery']['actions'], ['continue', 'restart', 'discard'])
            self.assertIn('transport retries', payload['recovery']['reason'])
            self.assertTrue((state_dir / 'workspaces/run-session-resume').is_dir())
            self.assertTrue((state_dir / 'baselines/run-session-resume').is_dir())

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=settings,
                cancel_event=threading.Event(),
                update=lambda _payload: None,
                resume=True,
                user_instruction='Continue from the provider timeout.',
            )

            self.assertEqual(payload['status'], 'passed')
            self.assertEqual(payload['attempt'], 1)
            self.assertEqual(agents.session_ids, ['run-session-resume:run-session-resume'] * 2)
            self.assertEqual([item['attempt'] for item in agents.fixer_requests], [1, 1])
            self.assertEqual(
                [item['resume_pending_turn'] for item in agents.fixer_requests],
                [False, True],
            )
            self.assertEqual(
                agents.fixer_requests[-1]['user_instruction'],
                'Continue from the provider timeout.',
            )
            self.assertEqual(parser.read_text(encoding='utf-8'), 'REPAIR_ATTEMPT = 1\n')
            self.assertFalse((state_dir / 'workspaces/run-session-resume').exists())
            self.assertFalse((state_dir / 'baselines/run-session-resume').exists())

    def test_continue_retries_verified_apply_after_transient_post_apply_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-apply-resume')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            original = parser.read_text(encoding='utf-8')
            agents = _LoopAgents(parser, pass_attempt=1)
            check_calls = 0

            def checks(workspace: Path, source_path: Path, session_id: str) -> list[DeterministicCheck]:
                nonlocal check_calls
                check_calls += 1
                if check_calls == 3:
                    raise OSError('synthetic post-apply service interruption')
                return _passing_checks(workspace, source_path, session_id)

            state_dir = root / 'state'
            engine = RepairEngine(
                source_workspace=source,
                state_dir=state_dir,
                agents=agents,
                check_runner=checks,
                probe_runner=_probe,
            )
            payload = _run_payload('repair', session_id='session-apply-resume')
            settings = QASettings(api_key='test-key', model='test-model')

            with self.assertRaisesRegex(OSError, 'post-apply service interruption'):
                engine.run(
                    payload,
                    result=result,
                    source_path=journal,
                    settings=settings,
                    cancel_event=threading.Event(),
                    update=lambda _payload: None,
                )

            self.assertEqual(payload['resume_from'], 'apply')
            self.assertEqual(parser.read_text(encoding='utf-8'), original)
            self.assertTrue((state_dir / 'workspaces/run-session-apply-resume').is_dir())

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=settings,
                cancel_event=threading.Event(),
                update=lambda _payload: None,
                resume=True,
            )

            self.assertEqual(payload['status'], 'passed')
            self.assertEqual(payload['attempt'], 1)
            self.assertEqual(agents.fix_calls, 1)
            self.assertEqual(agents.verify_calls, 1)
            self.assertEqual(check_calls, 4)
            self.assertEqual(parser.read_text(encoding='utf-8'), 'REPAIR_ATTEMPT = 1\n')
            self.assertIn('attempt-1-retry-1', payload['result']['backup_path'])

    def test_provider_fallback_continues_history_with_a_fresh_model_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-fallback')
            result = analyze_journals([journal], include_trace=True)
            state_dir = root / 'state'
            coordinator = RepairCoordinator(
                source_workspace=source,
                state_dir=state_dir,
                agents=_LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=1),
            )
            payload = _run_payload('repair', session_id='session-fallback')
            payload.update(
                {
                    'run_id': 'fallbackrun1',
                    'status': 'paused',
                    'message': 'Provider timed out.',
                    'dashboard_state': {
                        'source': {'session_id': 'session-fallback'},
                        'selection': {'turn_id': 'turn-1', 'event_sequence': None},
                        'view': {'active_tab': 'trace'},
                    },
                    'provider': 'openai',
                    'model': 'old-model',
                    'pending_model_turn': True,
                    'recovery': {
                        'reason': 'Provider timed out.',
                        'can_continue': True,
                        'workspace_preserved': True,
                        'actions': ['continue', 'restart', 'discard'],
                    },
                }
            )
            coordinator.store.save(payload)
            (state_dir / 'workspaces/fallbackrun1').mkdir(parents=True)
            (state_dir / 'baselines/fallbackrun1').mkdir(parents=True)

            authorization = coordinator.prepare_run_action(
                'fallbackrun1',
                action='continue',
                result=result,
                instruction=None,
                client_session_nonce='client-session-nonce-000001',
            )
            with mock.patch('agent_trace_studio.repair.threading.Thread') as thread:
                continued = coordinator.approve_source_action(
                    authorization_id=str(authorization['id']),
                    token=str(authorization['token']),
                    client_session_nonce='client-session-nonce-000001',
                    result=result,
                    settings=QASettings(
                        api_key='test-key',
                        provider='anthropic',
                        model='new-model',
                    ),
                )

            worker_args = thread.call_args.kwargs['args']
            self.assertEqual(continued['status'], 'queued')
            self.assertFalse(continued['pending_model_turn'])
            self.assertEqual(continued['fallbacks'][0]['from_provider'], 'openai')
            self.assertEqual(continued['fallbacks'][0]['to_provider'], 'anthropic')
            self.assertIn('provider or model change', worker_args[-1])
            thread.return_value.start.assert_called_once()
            coordinator.close()

    def test_continue_falls_back_to_turn_when_saved_event_was_renumbered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-stale-event')
            result = analyze_journals([journal], include_trace=True)
            state_dir = root / 'state'
            coordinator = RepairCoordinator(
                source_workspace=source,
                state_dir=state_dir,
                agents=_LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=1),
            )
            payload = _run_payload('repair', session_id='session-stale-event')
            payload.update(
                {
                    'run_id': 'staleeventrun1',
                    'status': 'interrupted',
                    'dashboard_state': {
                        'source': {'session_id': 'session-stale-event'},
                        'selection': {'turn_id': 'turn-1', 'event_sequence': 999_999},
                        'view': {'active_tab': 'trace'},
                    },
                    'provider': 'openai',
                    'model': 'test-model',
                    'recovery': {
                        'reason': 'Server interrupted.',
                        'can_continue': True,
                        'workspace_preserved': True,
                        'actions': ['continue', 'restart', 'discard'],
                    },
                }
            )
            coordinator.store.save(payload)
            (state_dir / 'workspaces/staleeventrun1').mkdir(parents=True)
            (state_dir / 'baselines/staleeventrun1').mkdir(parents=True)

            authorization = coordinator.prepare_run_action(
                'staleeventrun1',
                action='continue',
                result=result,
                instruction=None,
                client_session_nonce='client-session-nonce-000001',
            )
            with mock.patch('agent_trace_studio.repair.threading.Thread') as thread:
                continued = coordinator.approve_source_action(
                    authorization_id=str(authorization['id']),
                    token=str(authorization['token']),
                    client_session_nonce='client-session-nonce-000001',
                    result=result,
                    settings=QASettings(api_key='test-key', model='test-model'),
                )

            self.assertEqual(continued['status'], 'queued')
            self.assertEqual(continued['dashboard_state']['selection']['turn_id'], 'turn-1')
            self.assertIsNone(continued['dashboard_state']['selection']['event_sequence'])
            self.assertTrue(any('continuing with turn context' in item['message'] for item in continued['activity']))
            thread.return_value.start.assert_called_once()
            coordinator.close()

    def test_coordinator_starts_repair_from_matching_completed_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-fix-it')
            result = analyze_journals([journal], include_trace=True)
            coordinator = RepairCoordinator(
                source_workspace=source,
                state_dir=root / 'state',
                agents=_LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=1),
            )
            dashboard_state = {
                'source': {'session_id': 'session-fix-it'},
                'selection': {'turn_id': 'turn-1', 'event_sequence': None},
                'view': {'active_tab': 'trace'},
            }
            audit = _run_payload('audit', session_id='session-fix-it')
            audit.update(
                {
                    'run_id': 'auditfixit1',
                    'status': 'audited',
                    'event_sequence': None,
                    'dashboard_state': dashboard_state,
                    'source_workspace': str(source.resolve()),
                    'source_snapshot_digest': 'source-digest',
                    'audit': {
                        'summary': 'A supported parser issue was found.',
                        'requires_fix': True,
                        'confidence': 'high',
                        'issues': [],
                    },
                    'repair_summary': {'fixing': ['Supported parser issue']},
                }
            )
            audit['source_snapshot_digest'] = _snapshot_digest(snapshot_workspace(source))
            coordinator.store.save(audit)

            with self.assertRaisesRegex(ValueError, 'approved one-time source action'):
                coordinator.start(
                    'repair',
                    result=result,
                    dashboard_state=dashboard_state,
                    settings=QASettings(api_key='test-key', model='test-model'),
                    audit_run_id='auditfixit1',
                    instruction='Fix this parser.',
                )
            authorization = coordinator.prepare_source_action(
                'repair_parser',
                result=result,
                dashboard_state=dashboard_state,
                instruction='Fix this parser.',
                client_session_nonce='client-session-nonce-000001',
                audit_run_id='auditfixit1',
            )
            with mock.patch('agent_trace_studio.repair.threading.Thread') as thread:
                repair = coordinator.approve_source_action(
                    authorization_id=str(authorization['id']),
                    token=str(authorization['token']),
                    client_session_nonce='client-session-nonce-000001',
                    result=result,
                    settings=QASettings(api_key='test-key', model='test-model'),
                )

            self.assertEqual(repair['audit'], audit['audit'])
            self.assertEqual(repair['repair_summary'], audit['repair_summary'])
            self.assertEqual(repair['seed_audit_run_id'], 'auditfixit1')
            self.assertEqual(repair['seed_audit_source_digest'], audit['source_snapshot_digest'])
            self.assertEqual(repair['user_instruction'], 'Fix this parser.')
            self.assertEqual(repair['source_authorization']['activation_mode'], 'activate_verified_runtime')
            self.assertEqual(thread.call_args.kwargs['args'][-1], 'Fix this parser.')
            thread.return_value.start.assert_called_once()
            coordinator.close()

    def test_coordinator_requires_and_forwards_dashboard_customization_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-customize-start')
            result = analyze_journals([journal], include_trace=True)
            coordinator = RepairCoordinator(
                source_workspace=source,
                state_dir=root / 'state',
                agents=_LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=1),
            )
            dashboard_state = {
                'source': {'session_id': 'session-customize-start'},
                'selection': {'turn_id': 'turn-1', 'event_sequence': None},
                'view': {'active_tab': 'trace'},
            }
            settings = QASettings(api_key='test-key', model='test-model')

            with self.assertRaisesRegex(ValueError, 'describe the dashboard customization'):
                coordinator.start(
                    'customize',
                    result=result,
                    dashboard_state=dashboard_state,
                    settings=settings,
                )
            with self.assertRaisesRegex(ValueError, 'approved one-time source action'):
                coordinator.start(
                    'customize',
                    result=result,
                    dashboard_state=dashboard_state,
                    settings=settings,
                    instruction='Make the trace detail panel wider.',
                )
            authorization = coordinator.prepare_source_action(
                'customize_dashboard',
                result=result,
                dashboard_state=dashboard_state,
                instruction='Make the trace detail panel wider.',
                client_session_nonce='client-session-nonce-000001',
            )
            with mock.patch('agent_trace_studio.repair.threading.Thread') as thread:
                customization = coordinator.approve_source_action(
                    authorization_id=str(authorization['id']),
                    token=str(authorization['token']),
                    client_session_nonce='client-session-nonce-000001',
                    result=result,
                    settings=settings,
                )

            self.assertEqual(customization['kind'], 'customize')
            self.assertEqual(customization['customization_request'], 'Make the trace detail panel wider.')
            self.assertEqual(customization['source_authorization']['activation_mode'], 'activate_verified_runtime')
            self.assertEqual(thread.call_args.kwargs['args'][-1], 'Make the trace detail panel wider.')
            thread.return_value.start.assert_called_once()
            coordinator.close()

    def test_coordinator_activation_approval_is_bound_to_pending_verified_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-activation')
            result = analyze_journals([journal], include_trace=True)
            coordinator = RepairCoordinator(
                source_workspace=source,
                state_dir=root / 'state',
                agents=_LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=1),
            )
            dashboard_state = {
                'source': {'session_id': 'session-activation'},
                'selection': {'turn_id': 'turn-1', 'event_sequence': None},
                'view': {'active_tab': 'trace'},
            }
            pending = _run_payload('customize', session_id='session-activation')
            pending.update(
                {
                    'run_id': 'activationrun1',
                    'status': 'passed',
                    'event_sequence': None,
                    'dashboard_state': dashboard_state,
                    'source_workspace': str(source.resolve()),
                    'result': {'restart_required': True, 'change_digest': 'a' * 64},
                }
            )
            coordinator.store.save(pending)

            authorization = coordinator.prepare_run_action(
                'activationrun1',
                action='activate',
                result=result,
                instruction=None,
                client_session_nonce='client-session-nonce-000001',
            )
            approved = coordinator.approve_source_action(
                authorization_id=str(authorization['id']),
                token=str(authorization['token']),
                client_session_nonce='client-session-nonce-000001',
                result=result,
                settings=QASettings(api_key='test-key', model='test-model'),
            )
            coordinator.close()

        self.assertEqual(authorization['action'], 'activate_run')
        self.assertEqual(authorization['change_digest'], 'a' * 64)
        self.assertEqual(approved['source_authorization']['action'], 'activate_run')
        self.assertEqual(approved['source_authorization']['change_digest'], 'a' * 64)

    def test_verifier_pass_cannot_override_failed_deterministic_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-gate')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            original = parser.read_text(encoding='utf-8')
            agents = _LoopAgents(parser, pass_attempt=1)

            def candidate_only_failure(
                workspace: Path,
                _source_path: Path,
                _session_id: str,
            ) -> list[DeterministicCheck]:
                candidate = workspace / 'src/agent_trace_studio/parser.py'
                if 'REPAIR_ATTEMPT' in candidate.read_text(encoding='utf-8'):
                    return [DeterministicCheck('Synthetic checks', 'failed', 'test', 1, 'candidate failed')]
                return _passing_checks(workspace, _source_path, _session_id)

            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=agents,
                max_attempts=2,
                check_runner=candidate_only_failure,
                probe_runner=_probe,
            )
            payload = _run_payload('repair', session_id='session-gate')

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            self.assertEqual(payload['status'], 'blocked')
            self.assertEqual(len(payload['attempts']), 2)
            self.assertEqual(parser.read_text(encoding='utf-8'), original)

    def test_unchanged_baseline_failure_is_visible_but_does_not_block_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-inherited-gate')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            agents = _LoopAgents(parser, pass_attempt=1)

            def inherited_failure(
                _workspace: Path,
                _source_path: Path,
                _session_id: str,
            ) -> list[DeterministicCheck]:
                return [
                    DeterministicCheck(
                        'Ruff format',
                        'failed',
                        'ruff format --check .',
                        1,
                        'Would reformat: src/agent_trace_studio/agent_control.py',
                    )
                ]

            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=agents,
                max_attempts=1,
                check_runner=inherited_failure,
                probe_runner=_probe,
            )
            payload = _run_payload('repair', session_id='session-inherited-gate')

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            self.assertEqual(payload['status'], 'passed')
            self.assertEqual(parser.read_text(encoding='utf-8'), 'REPAIR_ATTEMPT = 1\n')
            attempt = payload['attempts'][0]
            self.assertTrue(attempt['deterministic_gates_passed'])
            self.assertEqual(attempt['checks'][0]['status'], 'skipped')
            self.assertEqual(attempt['raw_checks'][0]['status'], 'failed')
            self.assertEqual(attempt['baseline_checks'][0]['status'], 'failed')
            self.assertEqual(attempt['inherited_failed_checks'][0]['name'], 'Ruff format')
            self.assertEqual(attempt['new_failed_checks'], [])
            self.assertEqual(payload['result']['post_apply_checks'][0]['status'], 'skipped')

    def test_expanded_baseline_failure_remains_a_candidate_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-expanded-gate')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            agents = _LoopAgents(parser, pass_attempt=1)

            def expanded_failure(
                workspace: Path,
                _source_path: Path,
                _session_id: str,
            ) -> list[DeterministicCheck]:
                output = 'Would reformat: src/agent_trace_studio/agent_control.py'
                candidate = workspace / 'src/agent_trace_studio/parser.py'
                if 'REPAIR_ATTEMPT' in candidate.read_text(encoding='utf-8'):
                    output += '\nWould reformat: src/agent_trace_studio/parser.py'
                return [DeterministicCheck('Ruff format', 'failed', 'ruff format --check .', 1, output)]

            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=agents,
                max_attempts=1,
                check_runner=expanded_failure,
                probe_runner=_probe,
            )
            payload = _run_payload('repair', session_id='session-expanded-gate')

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            self.assertEqual(payload['status'], 'blocked')
            attempt = payload['attempts'][0]
            self.assertFalse(attempt['deterministic_gates_passed'])
            self.assertEqual(attempt['checks'][0]['status'], 'failed')
            self.assertEqual(attempt['inherited_failed_checks'], [])
            self.assertEqual(attempt['new_failed_checks'][0]['name'], 'Ruff format')
            self.assertNotEqual(parser.read_text(encoding='utf-8'), 'REPAIR_ATTEMPT = 1\n')

    def test_candidate_cannot_remove_a_baseline_check_from_the_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-missing-check')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            original = parser.read_text(encoding='utf-8')

            def missing_check(
                workspace: Path,
                _source_path: Path,
                _session_id: str,
            ) -> list[DeterministicCheck]:
                checks = [DeterministicCheck('Synthetic checks', 'passed', 'test', 0, 'ok')]
                candidate = workspace / 'src/agent_trace_studio/parser.py'
                if 'REPAIR_ATTEMPT' not in candidate.read_text(encoding='utf-8'):
                    checks.append(DeterministicCheck('Dashboard syntax', 'passed', 'node --check dashboard.js', 0, ''))
                return checks

            payload = _run_payload('repair', session_id='session-missing-check')
            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=_LoopAgents(parser, pass_attempt=1),
                max_attempts=1,
                check_runner=missing_check,
                probe_runner=_probe,
            )
            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            self.assertEqual(payload['status'], 'blocked')
            self.assertEqual(parser.read_text(encoding='utf-8'), original)
            missing = payload['attempts'][0]['new_failed_checks'][0]
            self.assertEqual(missing['name'], 'Dashboard syntax')
            self.assertIn('did not return this baseline check', missing['output'])

    def test_failure_for_a_changed_file_is_not_treated_as_inherited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-touched-failure')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'

            def touched_failure(
                _workspace: Path,
                _source_path: Path,
                _session_id: str,
            ) -> list[DeterministicCheck]:
                return [
                    DeterministicCheck(
                        'Ruff format',
                        'failed',
                        'ruff format --check .',
                        1,
                        'Would reformat: src/agent_trace_studio/parser.py',
                    )
                ]

            payload = _run_payload('repair', session_id='session-touched-failure')
            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=_LoopAgents(parser, pass_attempt=1),
                max_attempts=1,
                check_runner=touched_failure,
                probe_runner=_probe,
            )
            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            attempt = payload['attempts'][0]
            self.assertEqual(payload['status'], 'blocked')
            self.assertEqual(attempt['inherited_failed_checks'], [])
            self.assertEqual(attempt['new_failed_checks'][0]['name'], 'Ruff format')

    def test_identical_unit_test_failure_remains_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-unit-failure')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'

            def unit_failure(
                _workspace: Path,
                _source_path: Path,
                _session_id: str,
            ) -> list[DeterministicCheck]:
                return [DeterministicCheck('Unit tests', 'failed', 'python -m unittest', 1, 'same failure')]

            payload = _run_payload('repair', session_id='session-unit-failure')
            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=_LoopAgents(parser, pass_attempt=1),
                max_attempts=1,
                check_runner=unit_failure,
                probe_runner=_probe,
            )
            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            attempt = payload['attempts'][0]
            self.assertEqual(payload['status'], 'blocked')
            self.assertEqual(attempt['inherited_failed_checks'], [])
            self.assertEqual(attempt['new_failed_checks'][0]['name'], 'Unit tests')

    def test_duplicate_failures_are_matched_to_baseline_one_for_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / 'baseline'
            candidate = root / 'candidate'
            baseline.mkdir()
            candidate.mkdir()
            failure = DeterministicCheck('Ruff format', 'failed', 'ruff format --check .', 1, 'old.py')

            effective, inherited, new = _classify_check_regressions(
                [failure],
                [failure, failure],
                baseline_workspace=baseline,
                candidate_workspace=candidate,
                changed_files=('new.py',),
            )

            self.assertEqual([check.status for check in effective], ['skipped', 'failed'])
            self.assertEqual(len(inherited), 1)
            self.assertEqual(len(new), 1)

    def test_ruff_configuration_change_cannot_inherit_a_baseline_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / 'baseline'
            candidate = root / 'candidate'
            baseline.mkdir()
            candidate.mkdir()
            failure = DeterministicCheck(
                'Ruff format',
                'failed',
                'ruff format --check .',
                1,
                'Would reformat: src/agent_trace_studio/agent_control.py',
            )

            effective, inherited, new = _classify_check_regressions(
                [failure],
                [failure],
                baseline_workspace=baseline,
                candidate_workspace=candidate,
                changed_files=('pyproject.toml',),
            )

            self.assertEqual(effective[0].status, 'failed')
            self.assertEqual(inherited, [])
            self.assertEqual(new, [failure])

    def test_abbreviated_changed_path_cannot_inherit_a_baseline_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / 'baseline'
            candidate = root / 'candidate'
            baseline.mkdir()
            candidate.mkdir()
            failure = DeterministicCheck(
                'Ruff format',
                'failed',
                'ruff format --check .',
                1,
                'Would reformat: parser.py',
            )

            effective, inherited, new = _classify_check_regressions(
                [failure],
                [failure],
                baseline_workspace=baseline,
                candidate_workspace=candidate,
                changed_files=('src/agent_trace_studio/parser.py',),
            )

            self.assertEqual(effective[0].status, 'failed')
            self.assertEqual(inherited, [])
            self.assertEqual(new, [failure])

    def test_baseline_check_cache_is_bound_to_snapshot_and_strictly_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            baseline = root / 'baseline'
            clone_workspace(source, baseline)
            artifact_dir = root / 'artifacts'
            artifact_dir.mkdir()
            calls = 0

            def baseline_checks(
                _workspace: Path,
                _source_path: Path,
                _session_id: str,
            ) -> list[DeterministicCheck]:
                nonlocal calls
                calls += 1
                return [DeterministicCheck('Current check', 'passed', 'current', 0, 'ok')]

            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=_LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=1),
                check_runner=baseline_checks,
                probe_runner=_probe,
            )
            digest = _snapshot_digest(snapshot_workspace(baseline))
            payload: dict[str, object] = {
                'source_snapshot_digest': digest,
                'baseline_checks': [
                    {'name': 'Stale check', 'status': 'failed', 'command': 'old', 'exit_code': 1, 'output': 'old'}
                ],
                'baseline_checks_source_digest': 'wrong-digest',
                'baseline_checks_cache_version': 1,
            }

            loaded = engine._load_or_run_baseline_checks(
                payload,
                artifact_dir=artifact_dir,
                baseline_workspace=baseline,
                source_path=root / 'session.jsonl',
                session_id='session-cache',
            )
            self.assertEqual(calls, 1)
            self.assertEqual(loaded[0].name, 'Current check')

            (artifact_dir / 'baseline-checks.json').write_text(
                json.dumps(
                    {
                        'schema_version': 1,
                        'source_snapshot_digest': digest,
                        'checks': [{'name': 'Malformed', 'status': 'failed'}],
                    }
                ),
                encoding='utf-8',
            )
            loaded = engine._load_or_run_baseline_checks(
                {'source_snapshot_digest': digest},
                artifact_dir=artifact_dir,
                baseline_workspace=baseline,
                source_path=root / 'session.jsonl',
                session_id='session-cache',
            )
            self.assertEqual(calls, 2)
            self.assertEqual(loaded[0].name, 'Current check')

    def test_baseline_check_cache_is_invalidated_when_the_toolchain_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            baseline = root / 'baseline'
            clone_workspace(source, baseline)
            artifact_dir = root / 'artifacts'
            artifact_dir.mkdir()
            calls = 0

            def baseline_checks(
                _workspace: Path,
                _source_path: Path,
                _session_id: str,
            ) -> list[DeterministicCheck]:
                nonlocal calls
                calls += 1
                return [DeterministicCheck('Current check', 'passed', 'current', 0, f'run {calls}')]

            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=_LoopAgents(source / 'src/agent_trace_studio/parser.py', pass_attempt=1),
                check_runner=baseline_checks,
                probe_runner=_probe,
            )
            payload: dict[str, object] = {}
            with mock.patch(
                'agent_trace_studio.repair._check_environment_fingerprint',
                side_effect=['toolchain-one', 'toolchain-two'],
            ):
                first = engine._load_or_run_baseline_checks(
                    payload,
                    artifact_dir=artifact_dir,
                    baseline_workspace=baseline,
                    source_path=root / 'session.jsonl',
                    session_id='session-cache-toolchain',
                )
                second = engine._load_or_run_baseline_checks(
                    payload,
                    artifact_dir=artifact_dir,
                    baseline_workspace=baseline,
                    source_path=root / 'session.jsonl',
                    session_id='session-cache-toolchain',
                )

            self.assertEqual(calls, 2)
            self.assertEqual(first[0].output, 'run 1')
            self.assertEqual(second[0].output, 'run 2')
            self.assertEqual(payload['baseline_checks_checker_fingerprint'], 'toolchain-two')

    def test_check_runner_implementation_digest_distinguishes_same_named_code(self) -> None:
        def first_runner(_workspace: Path, _source_path: Path, _session_id: str) -> list[DeterministicCheck]:
            return []

        def second_runner(_workspace: Path, _source_path: Path, _session_id: str) -> list[DeterministicCheck]:
            return [DeterministicCheck('Changed', 'passed', 'changed', 0, 'changed')]

        second_runner.__qualname__ = first_runner.__qualname__

        self.assertNotEqual(
            _callable_implementation_digest(first_runner),
            _callable_implementation_digest(second_runner),
        )

    def test_post_apply_new_failure_rolls_back_after_candidate_inherited_baseline_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-post-apply-regression')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            original = parser.read_text(encoding='utf-8')
            check_calls = 0

            def post_apply_failure(
                _workspace: Path,
                _source_path: Path,
                _session_id: str,
            ) -> list[DeterministicCheck]:
                nonlocal check_calls
                check_calls += 1
                output = 'Would reformat: src/agent_trace_studio/agent_control.py'
                if check_calls == 3:
                    output += '\nWould reformat: src/agent_trace_studio/parser.py'
                return [DeterministicCheck('Ruff format', 'failed', 'ruff format --check .', 1, output)]

            payload = _run_payload('repair', session_id='session-post-apply-regression')
            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=_LoopAgents(parser, pass_attempt=1),
                max_attempts=1,
                check_runner=post_apply_failure,
                probe_runner=_probe,
            )
            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            self.assertEqual(check_calls, 3)
            self.assertEqual(payload['status'], 'blocked')
            self.assertEqual(parser.read_text(encoding='utf-8'), original)
            self.assertEqual(payload['last_feedback']['reason'], 'post_apply_verification_failed')
            self.assertEqual(payload['last_feedback']['new_failed_checks'][0]['name'], 'Ruff format')

    def test_resumed_verification_rechecks_the_current_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-resume-checks')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            agents = _VerifierPauseAgents(parser, pass_attempt=1)
            candidate_runs = 0

            def changing_checks(
                workspace: Path,
                _source_path: Path,
                _session_id: str,
            ) -> list[DeterministicCheck]:
                nonlocal candidate_runs
                if 'baselines' not in workspace.parts:
                    candidate_runs += 1
                if 'baselines' not in workspace.parts and candidate_runs > 1:
                    return [DeterministicCheck('Synthetic checks', 'failed', 'test', 1, 'new failure')]
                return [DeterministicCheck('Synthetic checks', 'passed', 'test', 0, 'ok')]

            payload = _run_payload('repair', session_id='session-resume-checks')
            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=agents,
                max_attempts=1,
                check_runner=changing_checks,
                probe_runner=_probe,
            )
            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )
            self.assertEqual(payload['status'], 'paused')

            verification_path = root / 'state/runs/run-session-resume-checks/attempt-1-verification.json'
            persisted = json.loads(verification_path.read_text(encoding='utf-8'))
            persisted['deterministic_gates_passed'] = True
            persisted['checks'] = []
            verification_path.write_text(json.dumps(persisted), encoding='utf-8')

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
                resume=True,
            )

            self.assertEqual(payload['status'], 'blocked')
            self.assertEqual(candidate_runs, 2)
            self.assertFalse(payload['attempts'][0]['deterministic_gates_passed'])
            self.assertEqual(payload['attempts'][0]['new_failed_checks'][0]['name'], 'Synthetic checks')

    def test_stops_after_identical_patch_and_verification_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='session-stalled')
            result = analyze_journals([journal], include_trace=True)
            parser = source / 'src/agent_trace_studio/parser.py'
            original = parser.read_text(encoding='utf-8')
            agents = _NoProgressAgents(parser, pass_attempt=None)
            engine = RepairEngine(
                source_workspace=source,
                state_dir=root / 'state',
                agents=agents,
                max_attempts=5,
                check_runner=_passing_checks,
                probe_runner=_probe,
            )
            payload = _run_payload('repair', session_id='session-stalled')

            engine.run(
                payload,
                result=result,
                source_path=journal,
                settings=QASettings(api_key='test-key', model='test-model'),
                cancel_event=threading.Event(),
                update=lambda _payload: None,
            )

            self.assertEqual(payload['status'], 'blocked')
            self.assertEqual(payload['attempt'], 2)
            self.assertIn('identical patches', payload['message'])
            self.assertEqual(parser.read_text(encoding='utf-8'), original)

    def test_audit_evidence_contains_shapes_not_trace_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'session.jsonl'
            write_journal(journal, session_id='session-evidence')
            result = analyze_journals([journal], include_trace=True)

            evidence = build_parser_audit_evidence(
                result,
                session_id='session-evidence',
                turn_id='turn-1',
                event_sequence=1,
            )
            serialized = json.dumps(evidence)

        self.assertNotIn('TOP_SECRET_TOOL_INPUT', serialized)
        self.assertNotIn('TOP_SECRET_TOOL_OUTPUT', serialized)
        self.assertFalse(evidence['content_policy']['raw_text_included'])
        self.assertTrue(evidence['record_shapes'])

    def test_activity_history_is_bounded_and_collapses_adjacent_duplicates(self) -> None:
        payload: dict[str, object] = {'activity': []}
        for index in range(55):
            _append_activity(
                payload,
                phase='Tool',
                message=f'Safe activity {index}',
                timestamp=f'2026-08-17T00:00:{index:02d}+00:00',
            )
        _append_activity(
            payload,
            phase='Tool',
            message='Safe activity 54',
            timestamp='2026-08-17T00:01:00+00:00',
        )

        activity = payload['activity']
        self.assertEqual(len(activity), 50)
        self.assertEqual(activity[-1]['repeat_count'], 2)
        self.assertEqual(activity[-1]['sequence'], 55)


class WorkspaceTransactionTest(unittest.TestCase):
    def test_clone_and_snapshot_ignore_generated_runtime_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            runtime = source / '.custom-agent-state/runs/previous'
            runtime.mkdir(parents=True)
            (runtime / 'result.json').write_text('{}\n', encoding='utf-8')
            egg_info = source / 'src/agent_trace_studio.egg-info'
            egg_info.mkdir()
            (egg_info / 'PKG-INFO').write_text('generated\n', encoding='utf-8')
            shadow = root / 'shadow'

            clone_workspace(source, shadow)
            snapshot = snapshot_workspace(source)

            self.assertFalse((shadow / '.custom-agent-state').exists())
            self.assertFalse((shadow / 'src/agent_trace_studio.egg-info').exists())
            self.assertFalse(any('-agent-state/' in path for path in snapshot.files))
            self.assertFalse(any('.egg-info/' in path for path in snapshot.files))

    def test_conflict_blocks_apply_and_rollback_restores_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            shadow = root / 'shadow'
            clone_workspace(source, shadow)
            baseline = snapshot_workspace(source)
            parser = shadow / 'src/agent_trace_studio/parser.py'
            parser.write_text('VERIFIED = True\n', encoding='utf-8')
            candidate = snapshot_workspace(shadow)
            delta = compare_workspaces(baseline, candidate)

            source_parser = source / 'src/agent_trace_studio/parser.py'
            source_parser.write_text('USER_EDIT = True\n', encoding='utf-8')
            transaction = LocalWorkspaceTransaction(
                target=source,
                candidate=candidate,
                baseline=baseline,
                backup_dir=root / 'backup-conflict',
            )
            with self.assertRaisesRegex(RuntimeError, 'changed during verification'):
                transaction.apply(delta)
            self.assertEqual(source_parser.read_text(encoding='utf-8'), 'USER_EDIT = True\n')

            source_parser.write_text('ORIGINAL = True\n', encoding='utf-8')
            baseline = snapshot_workspace(source)
            shadow_parser = shadow / 'src/agent_trace_studio/parser.py'
            shadow_parser.write_text('VERIFIED = True\n', encoding='utf-8')
            candidate = snapshot_workspace(shadow)
            delta = compare_workspaces(baseline, candidate)
            transaction = LocalWorkspaceTransaction(
                target=source,
                candidate=candidate,
                baseline=baseline,
                backup_dir=root / 'backup-rollback',
            )
            transaction.apply(delta)
            self.assertEqual(source_parser.read_text(encoding='utf-8'), 'VERIFIED = True\n')
            transaction.rollback()
            self.assertEqual(source_parser.read_text(encoding='utf-8'), 'ORIGINAL = True\n')

    def test_committed_change_can_be_restored_from_verified_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            shadow = root / 'shadow'
            clone_workspace(source, shadow)
            baseline = snapshot_workspace(source)
            parser = shadow / 'src/agent_trace_studio/parser.py'
            parser.write_text('VERIFIED = True\n', encoding='utf-8')
            candidate = snapshot_workspace(shadow)
            delta = compare_workspaces(baseline, candidate)
            backup = root / 'backup'
            transaction = LocalWorkspaceTransaction(
                target=source,
                candidate=candidate,
                baseline=baseline,
                backup_dir=backup,
            )
            transaction.apply(delta)
            transaction.commit()

            restore_workspace_backup(
                target=source,
                backup_dir=backup,
                added=delta.added,
                modified=delta.modified,
                deleted=delta.deleted,
                applied_records={key: candidate.files[key] for key in (*delta.added, *delta.modified)},
            )

            self.assertEqual(
                (source / 'src/agent_trace_studio/parser.py').read_text(encoding='utf-8'),
                'ORIGINAL = True\n',
            )

    def test_agent_cannot_change_trust_kernel_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _write_source_checkout(root / 'source')
            for relative in (
                'src/agent_trace_studio/supervisor.py',
                'src/agent_trace_studio/agent_control.py',
            ):
                protected = source / relative
                protected.parent.mkdir(parents=True, exist_ok=True)
                protected.write_text('TRUSTED = True\n', encoding='utf-8')
                shadow = root / f'shadow-{protected.stem}'
                clone_workspace(source, shadow)
                baseline = snapshot_workspace(source)
                (shadow / relative).write_text('TRUSTED = False\n', encoding='utf-8')

                with self.assertRaisesRegex(ValueError, 'protected runtime file'):
                    compare_workspaces(baseline, snapshot_workspace(shadow))


def _write_source_checkout(root: Path) -> Path:
    (root / 'src/agent_trace_studio').mkdir(parents=True)
    (root / 'tests').mkdir()
    (root / 'pyproject.toml').write_text('[project]\nname = "agent-trace-studio-test"\n', encoding='utf-8')
    (root / 'AGENTS.md').write_text('# Test repository\n', encoding='utf-8')
    (root / 'src/agent_trace_studio/parser.py').write_text('ORIGINAL = True\n', encoding='utf-8')
    (root / 'tests/test_existing.py').write_text('EXISTING = True\n', encoding='utf-8')
    return root


def _run_payload(kind: str, *, session_id: str) -> dict[str, object]:
    return {
        'run_id': f'run-{session_id}',
        'kind': kind,
        'status': 'queued',
        'message': 'queued',
        'created_at': '2026-08-17T00:00:00+00:00',
        'updated_at': '2026-08-17T00:00:00+00:00',
        'completed_at': None,
        'session_id': session_id,
        'turn_id': 'turn-1',
        'event_sequence': 1,
        'dashboard_state': {},
        'source_workspace': '',
        'max_attempts': 5,
        'attempt': 0,
        'attempts': [],
        'activity': [],
        'audit': None,
        'result': None,
    }


def _passing_checks(_workspace: Path, _source_path: Path, _session_id: str) -> list[DeterministicCheck]:
    return [DeterministicCheck('Synthetic checks', 'passed', 'test', 0, 'ok')]


def _failing_checks(_workspace: Path, _source_path: Path, _session_id: str) -> list[DeterministicCheck]:
    return [DeterministicCheck('Synthetic checks', 'failed', 'test', 1, 'failed')]


def _probe(workspace: Path, _source_path: Path, session_id: str) -> dict[str, object]:
    parser = workspace / 'src/agent_trace_studio/parser.py'
    return {'session_id': session_id, 'parser_digest': parser.read_text(encoding='utf-8')}


if __name__ == '__main__':
    unittest.main()
