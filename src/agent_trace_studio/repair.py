"""Bounded self-repair loop for the local Agent Trace Studio source."""

from __future__ import annotations

import hashlib
import json
import marshal
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from agent_trace_studio.agent_backend import (
    AgentProgress,
    AgentTransportError,
    FixerSession,
    ParserAudit,
    ParserAuditIssue,
    RepairAgentBackend,
    VerificationVerdict,
)
from agent_trace_studio.agent_control import (
    VERIFIED_RUNTIME_ACTIVATION_MODE,
    ApprovedSourceAction,
    SourceActionAuthorizer,
    SourceMutationAction,
)
from agent_trace_studio.harness_backend import SelectableRepairAgents
from agent_trace_studio.models import AnalysisResult, SessionTrace, TraceEvent
from agent_trace_studio.parser import _read_journal_rows
from agent_trace_studio.qa import (
    QASettings,
    TraceQAContext,
    build_session_checkpoint_context,
    build_studio_state_context,
)
from agent_trace_studio.workspace import (
    LocalWorkspaceTransaction,
    WorkspaceDelta,
    WorkspacePolicyViolation,
    WorkspaceSnapshot,
    clone_workspace,
    compare_workspaces,
    render_unified_diff,
    snapshot_workspace,
    validate_source_workspace,
)

RepairKind = Literal['investigate', 'memories', 'checkpoints', 'audit', 'repair', 'customize']
_SOURCE_CHANGE_KINDS = frozenset({'repair', 'customize'})
_SELECTED_HARNESS_KINDS = frozenset({'investigate', 'memories', 'checkpoints', 'audit', *_SOURCE_CHANGE_KINDS})
_ACTIVE_STATUSES = frozenset(
    {
        'queued',
        'investigating',
        'extracting',
        'summarizing',
        'auditing',
        'repairing',
        'retrying',
        'checking',
        'verifying',
        'applying',
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        'investigated',
        'extracted',
        'summarized',
        'audited',
        'passed',
        'paused',
        'blocked',
        'failed',
        'cancelled',
        'interrupted',
        'discarded',
    }
)
_PRESERVE_REPAIR_STATUSES = frozenset({'paused', 'blocked', 'failed', 'cancelled', 'interrupted'})
_PRESERVED_SHAPE_KEYS = frozenset({'type', 'role', 'phase', 'status', 'kind', 'name'})
_SECRET_ENV_MARKERS = ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL')
_MAX_EVIDENCE_SIGNATURES = 120
_MAX_ACTIVITY_ENTRIES = 50
_MAX_ACTIVITY_MESSAGE_CHARS = 220
_BASELINE_CHECK_CACHE_VERSION = 2
_INHERITABLE_BASELINE_FAILURES = frozenset({'Ruff lint', 'Ruff format', 'Dashboard syntax'})
_RUFF_CONFIGURATION_NAMES = frozenset({'pyproject.toml', 'ruff.toml', '.ruff.toml'})


@dataclass(frozen=True)
class DeterministicCheck:
    name: str
    status: Literal['passed', 'failed', 'skipped']
    command: str
    exit_code: int | None
    output: str


class RepairRunStore:
    """Small SQLite history store; API keys are never part of its payloads."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute(
                'CREATE TABLE IF NOT EXISTS repair_runs ('
                'run_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)'
            )
        self.mark_interrupted()

    def save(self, payload: dict[str, object]) -> None:
        serialized = json.dumps(payload, ensure_ascii=True, separators=(',', ':'), sort_keys=True)
        with self._lock, self._connect() as connection:
            connection.execute(
                'INSERT INTO repair_runs(run_id, updated_at, status, payload) VALUES(?, ?, ?, ?) '
                'ON CONFLICT(run_id) DO UPDATE SET updated_at=excluded.updated_at, '
                'status=excluded.status, payload=excluded.payload',
                (payload['run_id'], payload['updated_at'], payload['status'], serialized),
            )

    def get(self, run_id: str) -> dict[str, object] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute('SELECT payload FROM repair_runs WHERE run_id = ?', (run_id,)).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        return cast('dict[str, object]', value)

    def latest(self) -> dict[str, object] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute('SELECT payload FROM repair_runs ORDER BY updated_at DESC LIMIT 1').fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        return cast('dict[str, object]', value)

    def latest_session_brief(self, session_id: str) -> dict[str, object] | None:
        """Return the newest completed session-scoped checkpoint summary."""

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                'SELECT payload FROM repair_runs WHERE status = ? ORDER BY updated_at DESC',
                ('summarized',),
            ).fetchall()
        for row in rows:
            value = json.loads(row[0])
            if not isinstance(value, dict) or value.get('kind') != 'checkpoints':
                continue
            result = value.get('result')
            if value.get('session_id') == session_id and isinstance(result, dict) and result.get('scope') == 'session':
                return cast('dict[str, object]', value)
        return None

    def pending_deployments(self, *, limit: int = 50) -> list[dict[str, object]]:
        """Return verified source runs that still require runtime activation."""

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                'SELECT payload FROM repair_runs WHERE status = ? ORDER BY updated_at ASC',
                ('passed',),
            ).fetchall()
        maximum = max(1, min(limit, 200))
        pending: list[dict[str, object]] = []
        for row in rows:
            value = json.loads(row[0])
            if not isinstance(value, dict):
                continue
            result = value.get('result')
            if (
                value.get('kind') in _SOURCE_CHANGE_KINDS
                and isinstance(result, dict)
                and result.get('restart_required')
            ):
                pending.append(cast('dict[str, object]', value))
                if len(pending) >= maximum:
                    break
        return pending

    def mark_interrupted(self) -> None:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                'SELECT payload FROM repair_runs WHERE status IN '
                "('queued','investigating','extracting','summarizing','auditing','repairing','checking','verifying','applying')"
            ).fetchall()
            for row in rows:
                payload = cast('dict[str, object]', json.loads(row[0]))
                payload.update(
                    {
                        'status': 'interrupted',
                        'message': 'The local server stopped before this run completed.',
                        'updated_at': _now(),
                    }
                )
                run_id = str(payload['run_id'])
                can_continue = (
                    payload.get('kind') in _SOURCE_CHANGE_KINDS
                    and (self.path.parent / 'workspaces' / run_id).is_dir()
                    and (self.path.parent / 'baselines' / run_id).is_dir()
                )
                payload['recovery'] = _recovery_options(
                    reason='The local server stopped before this run completed.',
                    can_continue=can_continue,
                    workspace_preserved=can_continue,
                    category='server_interrupted',
                )
                _append_activity(
                    payload,
                    phase='Run',
                    message='The local server stopped before this run completed.',
                    timestamp=str(payload['updated_at']),
                )
                serialized = json.dumps(payload, ensure_ascii=True, separators=(',', ':'), sort_keys=True)
                connection.execute(
                    'UPDATE repair_runs SET updated_at = ?, status = ?, payload = ? WHERE run_id = ?',
                    (payload['updated_at'], payload['status'], serialized, payload['run_id']),
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()


class RepairEngine:
    """Deterministic orchestration around independent auditor, fixer, and verifier agents."""

    def __init__(
        self,
        *,
        source_workspace: Path | None,
        state_dir: Path,
        agents: RepairAgentBackend,
        max_attempts: int = 5,
        deployment_managed: bool = False,
        check_runner: Callable[[Path, Path, str], list[DeterministicCheck]] | None = None,
        probe_runner: Callable[[Path, Path, str], dict[str, object]] | None = None,
    ) -> None:
        if not 1 <= max_attempts <= 10:
            raise ValueError('max_attempts must be between 1 and 10')
        self.source_workspace = validate_source_workspace(source_workspace) if source_workspace is not None else None
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.agents = agents
        self.max_attempts = max_attempts
        self.deployment_managed = deployment_managed
        self._check_runner = check_runner or run_deterministic_checks
        self._check_cache_runtime_nonce = uuid.uuid4().hex
        self._probe_runner = probe_runner or run_parser_probe

    def run(
        self,
        payload: dict[str, object],
        *,
        result: AnalysisResult,
        source_path: Path,
        settings: QASettings,
        cancel_event: threading.Event,
        update: Callable[[dict[str, object]], None],
        resume: bool = False,
        user_instruction: str | None = None,
    ) -> None:
        run_id = str(payload['run_id'])
        if not resume:
            payload['max_attempts'] = self.max_attempts

        def report_progress(progress: AgentProgress) -> None:
            _record_activity(
                payload,
                update,
                phase=progress.phase,
                message=progress.message,
            )

        if payload['kind'] in {'investigate', 'memories', 'checkpoints'}:
            self._run_trace_workflow(
                payload,
                result=result,
                settings=settings,
                cancel_event=cancel_event,
                update=update,
                report_progress=report_progress,
            )
            return
        source_workspace = self.source_workspace
        if source_workspace is None:
            raise ValueError('parser audit and local source repair require an Agent Trace Studio source workspace')
        shadow = self.state_dir / 'workspaces' / run_id
        baseline_dir = self.state_dir / 'baselines' / run_id
        artifact_dir = self.state_dir / 'runs' / run_id
        workspace_ready = False
        preserve_on_error = False
        try:
            if resume:
                if not shadow.is_dir() or not baseline_dir.is_dir() or not artifact_dir.is_dir():
                    raise RuntimeError('The preserved repair workspace is unavailable; start a new repair run.')
                _record_activity(
                    payload,
                    update,
                    phase='Resume',
                    message='Reopening the preserved fixer workspace and conversation.',
                )
            else:
                artifact_dir.mkdir(parents=True, exist_ok=False)
                _record_activity(
                    payload,
                    update,
                    phase='Workspace',
                    message='Capturing the local source snapshot.',
                )
                clone_workspace(source_workspace, baseline_dir)
                _record_activity(
                    payload,
                    update,
                    phase='Workspace',
                    message='Creating an isolated audit workspace.',
                )
                clone_workspace(baseline_dir, shadow)
            baseline = snapshot_workspace(baseline_dir)
            baseline_digest = _snapshot_digest(baseline)
            payload['source_snapshot_digest'] = baseline_digest
            authorized_digest = _optional_string(payload.get('authorized_source_digest'))
            if authorized_digest is not None and authorized_digest != baseline_digest:
                raise RuntimeError('local source changed before the approved source snapshot was captured')
            workspace_ready = True
            shadow_control = shadow / '.agent-trace-studio'
            shadow_control.mkdir(exist_ok=True)
            evidence_path = artifact_dir / 'audit-evidence.json'
            audit_path = artifact_dir / 'audit-report.json'
            if evidence_path.is_file():
                evidence = cast('dict[str, object]', json.loads(evidence_path.read_text(encoding='utf-8')))
            else:
                if payload['kind'] == 'customize':
                    _record_activity(
                        payload,
                        update,
                        phase='Evidence',
                        message='Recording the explicit dashboard customization request.',
                    )
                    evidence = {
                        'workflow': 'dashboard_customization',
                        'request': str(payload.get('customization_request') or ''),
                        'dashboard_state': payload.get('dashboard_state'),
                        'content_policy': {
                            'raw_journal_rows_included': False,
                            'encrypted_reasoning_included': False,
                        },
                    }
                else:
                    _record_activity(
                        payload,
                        update,
                        phase='Evidence',
                        message='Building structural journal evidence.',
                    )
                    evidence = build_parser_audit_evidence(
                        result,
                        session_id=str(payload['session_id']),
                        turn_id=_optional_string(payload.get('turn_id')),
                        event_sequence=_optional_int(payload.get('event_sequence')),
                    )
                _write_json(evidence_path, evidence)
            _write_json(shadow_control / 'audit-evidence.json', evidence)
            if audit_path.is_file():
                audit_payload = cast('dict[str, object]', json.loads(audit_path.read_text(encoding='utf-8')))
                audit = ParserAudit.model_validate(audit_payload)
                payload['audit'] = audit_payload
                _write_json(shadow_control / 'audit-report.json', audit_payload)
            elif (
                payload['kind'] == 'repair'
                and payload.get('seed_audit_source_digest') == baseline_digest
                and isinstance(payload.get('audit'), dict)
            ):
                audit_payload = cast('dict[str, object]', payload['audit'])
                audit = ParserAudit.model_validate(audit_payload)
                _record_activity(
                    payload,
                    update,
                    phase='Audit',
                    message='Using the completed audit for the unchanged source snapshot.',
                )
                _write_json(shadow_control / 'audit-report.json', audit_payload)
                _write_json(audit_path, audit_payload)
            elif payload['kind'] == 'customize':
                request = str(payload.get('customization_request') or '').strip()
                audit = ParserAudit(
                    summary=f'Implement the requested dashboard customization: {request}',
                    requires_fix=True,
                    confidence='high',
                    issues=[
                        ParserAuditIssue(
                            severity='medium',
                            title=f'Dashboard customization: {request[:160]}',
                            evidence='The user explicitly requested this local dashboard change.',
                            expected_behavior=request,
                            suggested_test='Add or update focused coverage for the requested behavior and layout.',
                        )
                    ],
                )
                audit_payload = audit.model_dump(mode='json')
                payload['audit'] = audit_payload
                _record_activity(
                    payload,
                    update,
                    phase='Customize',
                    message='Prepared the explicit customization task for the coding agent.',
                )
                _write_json(shadow_control / 'audit-report.json', audit_payload)
                _write_json(audit_path, audit_payload)
            else:
                if payload.get('seed_audit_run_id'):
                    payload['audit'] = None
                    _record_activity(
                        payload,
                        update,
                        phase='Audit',
                        message='The source changed after the completed audit; running a fresh audit.',
                    )
                payload['resume_from'] = 'audit'
                _set_run(
                    payload,
                    update,
                    status='auditing',
                    message='Read-only audit agent is inspecting parser coverage.',
                )
                try:
                    audit = self.agents.audit(shadow, settings, progress=report_progress)
                except AgentTransportError as exc:
                    self._pause_for_transport(payload, update, exc, stage='audit')
                    preserve_on_error = True
                    return
                audit_payload = audit.model_dump(mode='json')
                payload['audit'] = audit_payload
                _write_json(shadow_control / 'audit-report.json', audit_payload)
                _write_json(audit_path, audit_payload)
            _update_repair_summary(payload, audit=audit)
            if payload['kind'] == 'audit':
                _set_run(
                    payload,
                    update,
                    status='audited',
                    message=_audit_message(audit),
                    completed_at=_now(),
                )
                return
            if _cancelled(payload, cancel_event, update):
                return

            if payload.get('parser_before') is None:
                _record_activity(
                    payload,
                    update,
                    phase='Replay',
                    message='Capturing the parser baseline for comparison.',
                )
                payload['parser_before'] = self._probe_runner(
                    baseline_dir,
                    source_path,
                    str(payload['session_id']),
                )

            feedback_value = payload.get('last_feedback')
            feedback = cast('dict[str, object]', feedback_value) if isinstance(feedback_value, dict) else None
            attempts = cast('list[object]', payload['attempts'])
            previous_cycle = _last_failure_cycle(attempts, feedback)
            max_attempts = _payload_max_attempts(payload, self.max_attempts)
            fixer = _create_fixer_session(
                self.agents,
                shadow,
                settings,
                run_id=run_id,
                state_dir=artifact_dir,
            )
            pending_turn = bool(payload.get('pending_model_turn'))
            resume_from = str(payload.get('resume_from') or '')
            incomplete_attempt = int(payload.get('attempt') or 0)
            if incomplete_attempt > len(attempts) and resume_from in {'repair', 'verify'}:
                attempt_number = incomplete_attempt
            else:
                attempt_number = len(attempts) + 1

            if resume_from == 'apply' and attempts:
                last_attempt = attempts[-1]
                if isinstance(last_attempt, dict) and isinstance(last_attempt.get('verifier'), dict):
                    attempt_number = int(last_attempt.get('attempt') or len(attempts))
                    candidate = snapshot_workspace(shadow)
                    delta = compare_workspaces(baseline, candidate)
                    verdict = VerificationVerdict.model_validate(last_attempt['verifier'])
                    _record_activity(
                        payload,
                        update,
                        phase='Resume',
                        message=f'Retrying transactional apply for verified attempt {attempt_number}.',
                    )
                    if self._apply_verified_delta(
                        payload,
                        update,
                        baseline=baseline,
                        candidate=candidate,
                        delta=delta,
                        source_path=source_path,
                        session_id=str(payload['session_id']),
                        attempt_number=attempt_number,
                        verdict=verdict,
                    ):
                        return
                    feedback_value = payload.get('last_feedback')
                    feedback = cast('dict[str, object]', feedback_value) if isinstance(feedback_value, dict) else None
                    previous_cycle = _last_failure_cycle(attempts, feedback)
                    attempt_number = len(attempts) + 1
                    resume_from = ''

            while attempt_number <= max_attempts:
                if _cancelled(payload, cancel_event, update):
                    return
                payload['attempt'] = attempt_number
                verification_path = artifact_dir / f'attempt-{attempt_number}-verification.json'
                resume_verification = (
                    resume_from == 'verify'
                    and verification_path.is_file()
                    and not any(isinstance(item, dict) and item.get('attempt') == attempt_number for item in attempts)
                )
                resume_unclassified_candidate = (
                    resume
                    and resume_from == 'repair'
                    and not pending_turn
                    and attempt_number == incomplete_attempt
                    and not any(isinstance(item, dict) and item.get('attempt') == attempt_number for item in attempts)
                )
                if resume_unclassified_candidate:
                    candidate = snapshot_workspace(shadow)
                    try:
                        compare_workspaces(baseline, candidate)
                    except WorkspacePolicyViolation as exc:
                        feedback, candidate_digest = self._reject_workspace_policy_candidate(
                            payload,
                            update,
                            audit=audit,
                            baseline=baseline,
                            baseline_dir=baseline_dir,
                            candidate=candidate,
                            shadow=shadow,
                            shadow_control=shadow_control,
                            artifact_dir=artifact_dir,
                            evidence=evidence,
                            audit_payload=audit_payload,
                            attempt_number=attempt_number,
                            fixer_summary='Recovered the completed candidate from the interrupted attempt.',
                            violation=exc,
                        )
                        cycle = (candidate_digest, _failure_signature(feedback))
                        if cycle == previous_cycle:
                            self._stop_repair(
                                payload,
                                update,
                                status='blocked',
                                message='Repair stopped after two identical candidates violated workspace policy.',
                                category='repeated_policy_violation',
                            )
                            return
                        previous_cycle = cycle
                        resume_from = ''
                        attempt_number += 1
                        continue
                if resume_verification:
                    try:
                        saved_verification = json.loads(verification_path.read_text(encoding='utf-8'))
                    except (OSError, json.JSONDecodeError):
                        saved_verification = {}
                    if not isinstance(saved_verification, dict):
                        saved_verification = {}
                    candidate = snapshot_workspace(shadow)
                    try:
                        delta = compare_workspaces(baseline, candidate)
                    except WorkspacePolicyViolation as exc:
                        feedback, candidate_digest = self._reject_workspace_policy_candidate(
                            payload,
                            update,
                            audit=audit,
                            baseline=baseline,
                            baseline_dir=baseline_dir,
                            candidate=candidate,
                            shadow=shadow,
                            shadow_control=shadow_control,
                            artifact_dir=artifact_dir,
                            evidence=evidence,
                            audit_payload=audit_payload,
                            attempt_number=attempt_number,
                            fixer_summary=str(saved_verification.get('fixer_summary') or 'Resumed fixer attempt.'),
                            violation=exc,
                        )
                        cycle = (candidate_digest, _failure_signature(feedback))
                        if cycle == previous_cycle:
                            self._stop_repair(
                                payload,
                                update,
                                status='blocked',
                                message='Repair stopped after two identical candidates violated workspace policy.',
                                category='repeated_policy_violation',
                            )
                            return
                        previous_cycle = cycle
                        resume_from = ''
                        attempt_number += 1
                        continue
                    _restore_control_artifacts(
                        shadow_control,
                        evidence=evidence,
                        audit=audit_payload,
                        feedback=feedback,
                    )
                    patch = render_unified_diff(baseline, candidate, delta)
                    (shadow_control / 'change.patch').write_text(patch, encoding='utf-8')
                    (artifact_dir / f'attempt-{attempt_number}.patch').write_text(patch, encoding='utf-8')
                    _set_run(
                        payload,
                        update,
                        status='checking',
                        message=f'Revalidating deterministic checks for resumed attempt {attempt_number}.',
                    )
                    verification_package = self._build_verification_package(
                        payload,
                        attempt_number=attempt_number,
                        audit_payload=audit_payload,
                        fixer_summary=str(saved_verification.get('fixer_summary') or 'Resumed fixer attempt.'),
                        baseline_workspace=baseline_dir,
                        candidate_workspace=shadow,
                        artifact_dir=artifact_dir,
                        source_path=source_path,
                        session_id=str(payload['session_id']),
                        delta=delta,
                    )
                    _write_json(shadow_control / 'verification.json', verification_package)
                    _write_json(verification_path, verification_package)
                    _update_repair_summary(payload, audit=audit, attempt=verification_package)
                else:
                    payload['resume_from'] = 'repair'
                    _set_run(
                        payload,
                        update,
                        status='repairing',
                        message=f'Fix agent is working on attempt {attempt_number} of {max_attempts}.',
                    )
                    try:
                        fixer_summary = fixer.fix(
                            attempt=attempt_number,
                            audit=audit,
                            feedback=feedback,
                            user_instruction=user_instruction,
                            resume_pending_turn=pending_turn,
                            progress=report_progress,
                        )
                    except AgentTransportError as exc:
                        payload['pending_model_turn'] = True
                        self._pause_for_transport(payload, update, exc, stage='repair')
                        preserve_on_error = True
                        return
                    pending_turn = False
                    payload['pending_model_turn'] = False
                    user_instruction = None
                    if _cancelled(payload, cancel_event, update):
                        return
                    _restore_control_artifacts(
                        shadow_control,
                        evidence=evidence,
                        audit=audit_payload,
                        feedback=feedback,
                    )
                    candidate = snapshot_workspace(shadow)
                    try:
                        delta = compare_workspaces(baseline, candidate)
                    except WorkspacePolicyViolation as exc:
                        feedback, candidate_digest = self._reject_workspace_policy_candidate(
                            payload,
                            update,
                            audit=audit,
                            baseline=baseline,
                            baseline_dir=baseline_dir,
                            candidate=candidate,
                            shadow=shadow,
                            shadow_control=shadow_control,
                            artifact_dir=artifact_dir,
                            evidence=evidence,
                            audit_payload=audit_payload,
                            attempt_number=attempt_number,
                            fixer_summary=fixer_summary,
                            violation=exc,
                        )
                        cycle = (candidate_digest, _failure_signature(feedback))
                        if cycle == previous_cycle:
                            self._stop_repair(
                                payload,
                                update,
                                status='blocked',
                                message='Repair stopped after two identical candidates violated workspace policy.',
                                category='repeated_policy_violation',
                            )
                            return
                        previous_cycle = cycle
                        resume_from = ''
                        attempt_number += 1
                        continue
                    patch = render_unified_diff(baseline, candidate, delta)
                    (shadow_control / 'change.patch').write_text(patch, encoding='utf-8')
                    (artifact_dir / f'attempt-{attempt_number}.patch').write_text(patch, encoding='utf-8')
                    _set_run(
                        payload,
                        update,
                        status='checking',
                        message=f'Running deterministic checks for attempt {attempt_number}.',
                    )
                    verification_package = self._build_verification_package(
                        payload,
                        attempt_number=attempt_number,
                        audit_payload=audit_payload,
                        fixer_summary=fixer_summary,
                        baseline_workspace=baseline_dir,
                        candidate_workspace=shadow,
                        artifact_dir=artifact_dir,
                        source_path=source_path,
                        session_id=str(payload['session_id']),
                        delta=delta,
                    )
                    _write_json(shadow_control / 'verification.json', verification_package)
                    _write_json(verification_path, verification_package)
                    _update_repair_summary(payload, audit=audit, attempt=verification_package)

                finished, feedback = self._complete_attempt(
                    payload,
                    update,
                    audit=audit,
                    baseline=baseline,
                    candidate=candidate,
                    delta=delta,
                    shadow=shadow,
                    shadow_control=shadow_control,
                    artifact_dir=artifact_dir,
                    source_path=source_path,
                    settings=settings,
                    verification_package=verification_package,
                    report_progress=report_progress,
                )
                if finished:
                    return
                resume_from = ''
                failure_signature = _failure_signature(feedback)
                cycle = (delta.digest, failure_signature)
                if cycle == previous_cycle:
                    self._stop_repair(
                        payload,
                        update,
                        status='blocked',
                        message='Repair stopped after two identical patches produced the same verification failure.',
                        category='repeated_failure',
                    )
                    return
                previous_cycle = cycle
                attempt_number += 1

            self._stop_repair(
                payload,
                update,
                status='blocked',
                message=f'Repair did not pass verification after {max_attempts} attempts.',
                category='attempt_limit',
            )
        except BaseException:
            preserve_on_error = workspace_ready and payload.get('kind') in _SOURCE_CHANGE_KINDS
            raise
        finally:
            status = str(payload.get('status') or '')
            result_payload = payload.get('result')
            awaiting_deployment = (
                self.deployment_managed
                and status == 'passed'
                and isinstance(result_payload, dict)
                and bool(result_payload.get('restart_required'))
            )
            preserve = payload.get('kind') in _SOURCE_CHANGE_KINDS and (
                preserve_on_error or status in _PRESERVE_REPAIR_STATUSES or awaiting_deployment
            )
            if not preserve:
                self._cleanup_workspace(run_id, remove_session_state=True)

    def _build_verification_package(
        self,
        payload: dict[str, object],
        *,
        attempt_number: int,
        audit_payload: dict[str, object],
        fixer_summary: str,
        baseline_workspace: Path,
        candidate_workspace: Path,
        artifact_dir: Path,
        source_path: Path,
        session_id: str,
        delta: WorkspaceDelta,
    ) -> dict[str, object]:
        parser_after = _probe_or_error(self._probe_runner, candidate_workspace, source_path, session_id)
        raw_checks = self._check_runner(candidate_workspace, source_path, session_id)
        checks, baseline_checks, inherited_failures, new_failures = self._classify_candidate_checks(
            payload,
            artifact_dir=artifact_dir,
            baseline_workspace=baseline_workspace,
            candidate_workspace=candidate_workspace,
            source_path=source_path,
            session_id=session_id,
            candidate_checks=raw_checks,
            changed_files=delta.changed_files,
        )
        return {
            'attempt': attempt_number,
            'audit': audit_payload,
            'changed_files': list(delta.changed_files),
            'change_digest': delta.digest,
            'changed_bytes': delta.changed_bytes,
            'fixer_summary': fixer_summary,
            'parser_before': payload.get('parser_before'),
            'parser_after': parser_after,
            'deterministic_gates_passed': _checks_passed(checks) and 'error' not in parser_after,
            'checks': [asdict(check) for check in checks],
            'raw_checks': [asdict(check) for check in raw_checks],
            'baseline_checks': [asdict(check) for check in baseline_checks],
            'baseline_checks_source_digest': payload.get('baseline_checks_source_digest'),
            'inherited_failed_checks': [asdict(check) for check in inherited_failures],
            'new_failed_checks': [asdict(check) for check in new_failures],
        }

    def _classify_candidate_checks(
        self,
        payload: dict[str, object],
        *,
        artifact_dir: Path,
        baseline_workspace: Path,
        candidate_workspace: Path,
        source_path: Path,
        session_id: str,
        candidate_checks: list[DeterministicCheck],
        changed_files: tuple[str, ...],
    ) -> tuple[
        list[DeterministicCheck],
        list[DeterministicCheck],
        list[DeterministicCheck],
        list[DeterministicCheck],
    ]:
        baseline_checks = self._load_or_run_baseline_checks(
            payload,
            artifact_dir=artifact_dir,
            baseline_workspace=baseline_workspace,
            source_path=source_path,
            session_id=session_id,
        )
        effective, inherited, new = _classify_check_regressions(
            baseline_checks,
            candidate_checks,
            baseline_workspace=baseline_workspace,
            candidate_workspace=candidate_workspace,
            changed_files=changed_files,
        )
        return effective, baseline_checks, inherited, new

    def _load_or_run_baseline_checks(
        self,
        payload: dict[str, object],
        *,
        artifact_dir: Path,
        baseline_workspace: Path,
        source_path: Path,
        session_id: str,
    ) -> list[DeterministicCheck]:
        baseline_digest = _snapshot_digest(snapshot_workspace(baseline_workspace))
        checker_fingerprint = _check_environment_fingerprint(
            self._check_runner,
            runtime_nonce=self._check_cache_runtime_nonce,
        )
        stored = payload.get('baseline_checks')
        stored_digest = payload.get('baseline_checks_source_digest')
        stored_version = payload.get('baseline_checks_cache_version')
        stored_checker = payload.get('baseline_checks_checker_fingerprint')
        if (
            isinstance(stored, list)
            and stored_digest == baseline_digest
            and stored_version == _BASELINE_CHECK_CACHE_VERSION
            and stored_checker == checker_fingerprint
        ):
            checks = _checks_from_records(stored)
            if checks is not None:
                return checks
        baseline_path = artifact_dir / 'baseline-checks.json'
        if baseline_path.is_file():
            try:
                loaded = json.loads(baseline_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                loaded = None
            if (
                isinstance(loaded, dict)
                and loaded.get('schema_version') == _BASELINE_CHECK_CACHE_VERSION
                and loaded.get('source_snapshot_digest') == baseline_digest
                and loaded.get('checker_fingerprint') == checker_fingerprint
                and isinstance(loaded.get('checks'), list)
            ):
                checks = _checks_from_records(cast('list[object]', loaded['checks']))
            else:
                checks = None
            if checks is not None:
                payload['baseline_checks'] = [asdict(check) for check in checks]
                payload['baseline_checks_source_digest'] = baseline_digest
                payload['baseline_checks_cache_version'] = _BASELINE_CHECK_CACHE_VERSION
                payload['baseline_checks_checker_fingerprint'] = checker_fingerprint
                return checks
        checks = self._check_runner(baseline_workspace, source_path, session_id)
        records = [asdict(check) for check in checks]
        payload['baseline_checks'] = records
        payload['baseline_checks_source_digest'] = baseline_digest
        payload['baseline_checks_cache_version'] = _BASELINE_CHECK_CACHE_VERSION
        payload['baseline_checks_checker_fingerprint'] = checker_fingerprint
        _write_json(
            baseline_path,
            {
                'schema_version': _BASELINE_CHECK_CACHE_VERSION,
                'source_snapshot_digest': baseline_digest,
                'checker_fingerprint': checker_fingerprint,
                'checks': records,
            },
        )
        return checks

    def _complete_attempt(
        self,
        payload: dict[str, object],
        update: Callable[[dict[str, object]], None],
        *,
        audit: ParserAudit,
        baseline: WorkspaceSnapshot,
        candidate: WorkspaceSnapshot,
        delta: WorkspaceDelta,
        shadow: Path,
        shadow_control: Path,
        artifact_dir: Path,
        source_path: Path,
        settings: QASettings,
        verification_package: dict[str, object],
        report_progress: Callable[[AgentProgress], None],
    ) -> tuple[bool, dict[str, object] | None]:
        attempt_number = int(verification_package['attempt'])
        payload['resume_from'] = 'verify'
        _set_run(
            payload,
            update,
            status='verifying',
            message=f'Independent verification agent is reviewing attempt {attempt_number}.',
        )
        try:
            verdict = self.agents.verify(shadow, settings, progress=report_progress)
        except AgentTransportError as exc:
            self._pause_for_transport(payload, update, exc, stage='verify')
            return True, None
        attempt_payload = {
            **verification_package,
            'verifier': verdict.model_dump(mode='json'),
        }
        _write_json(shadow_control / 'verification.json', attempt_payload)
        _write_json(artifact_dir / f'attempt-{attempt_number}-verification.json', attempt_payload)
        attempts = cast('list[object]', payload['attempts'])
        attempts[:] = [item for item in attempts if not isinstance(item, dict) or item.get('attempt') != attempt_number]
        attempts.append(attempt_payload)
        _update_repair_summary(payload, audit=audit, attempt=attempt_payload)
        update(payload)
        checks = [
            DeterministicCheck(
                name=str(item.get('name') or 'Check'),
                status=cast('Literal["passed", "failed", "skipped"]', item.get('status')),
                command=str(item.get('command') or ''),
                exit_code=item.get('exit_code') if isinstance(item.get('exit_code'), int) else None,
                output=str(item.get('output') or ''),
            )
            for item in cast('list[dict[str, object]]', verification_package.get('checks') or [])
            if isinstance(item, dict) and item.get('status') in {'passed', 'failed', 'skipped'}
        ]
        parser_after = cast('dict[str, object]', verification_package.get('parser_after') or {})
        gates_passed = bool(verification_package.get('deterministic_gates_passed'))
        if _accepted(
            audit,
            delta,
            gates_passed,
            verdict,
            allow_already_satisfied=payload.get('kind') == 'customize',
        ):
            if delta.empty:
                _set_run(
                    payload,
                    update,
                    status='passed',
                    message='Audit and verification passed; no source change was required.',
                    completed_at=_now(),
                    result={
                        'changed_files': [],
                        'restart_required': False,
                        'already_satisfied': bool(verdict.already_satisfied),
                        'verifier': verdict.model_dump(mode='json'),
                    },
                )
                return True, None
            payload['resume_from'] = 'apply'
            if self._apply_verified_delta(
                payload,
                update,
                baseline=baseline,
                candidate=candidate,
                delta=delta,
                source_path=source_path,
                session_id=str(payload['session_id']),
                attempt_number=attempt_number,
                verdict=verdict,
            ):
                return True, None
            feedback = cast('dict[str, object]', payload.get('last_feedback'))
        else:
            feedback = _verification_feedback(audit, delta, gates_passed, checks, parser_after, verdict)
            payload['last_feedback'] = feedback
        if feedback is not None:
            _write_json(shadow_control / 'verification-feedback.json', feedback)
            _write_json(artifact_dir / f'attempt-{attempt_number}-feedback.json', feedback)
        payload['resume_from'] = 'repair'
        return False, feedback

    def _reject_workspace_policy_candidate(
        self,
        payload: dict[str, object],
        update: Callable[[dict[str, object]], None],
        *,
        audit: ParserAudit,
        baseline: WorkspaceSnapshot,
        baseline_dir: Path,
        candidate: WorkspaceSnapshot,
        shadow: Path,
        shadow_control: Path,
        artifact_dir: Path,
        evidence: dict[str, object],
        audit_payload: dict[str, object],
        attempt_number: int,
        fixer_summary: str,
        violation: WorkspacePolicyViolation,
    ) -> tuple[dict[str, object], str]:
        """Record a host policy rejection, reset the candidate, and return fixer feedback."""

        changed_files = list(violation.changed_files)
        blocked_files = list(violation.blocked_files)
        candidate_digest = _rejected_candidate_digest(candidate, violation.changed_files, violation.category)
        policy_check = {
            'name': 'Workspace policy',
            'status': 'failed',
            'command': 'host candidate policy',
            'exit_code': 1,
            'output': str(violation),
        }
        required_change = (
            'Reimplement the requested change without modifying protected runtime files. '
            'Use the presentation, parser, or test surfaces that are outside the protected control plane.'
            if violation.category == 'protected_runtime_file'
            else 'Reduce the candidate to satisfy the host workspace policy before verification.'
        )
        verifier = {
            'status': 'skipped',
            'summary': 'Independent review was not run because the host rejected the candidate first.',
            'fixed_items': [],
            'unresolved_issues': [str(violation)],
            'regressions': [],
            'required_changes': [required_change],
        }
        attempt_payload: dict[str, object] = {
            'attempt': attempt_number,
            'fixer_summary': fixer_summary,
            'changed_files': changed_files,
            'change_digest': candidate_digest,
            'source_delta': _workspace_change_summary(baseline, candidate),
            'checks': [policy_check],
            'inherited_failed_checks': [],
            'new_failed_checks': [policy_check],
            'deterministic_gates_passed': False,
            'parser_before': payload.get('parser_before'),
            'parser_after': {},
            'verifier': verifier,
            'policy_violation': {
                'category': violation.category,
                'message': str(violation),
                'blocked_files': blocked_files,
            },
        }
        feedback: dict[str, object] = {
            'reason': 'workspace_policy_rejected',
            'deterministic_gates_passed': False,
            'failed_checks': [policy_check],
            'parser_probe_error': None,
            'verifier': verifier,
            'policy_violation': attempt_payload['policy_violation'],
        }
        attempts = cast('list[object]', payload['attempts'])
        attempts[:] = [item for item in attempts if not isinstance(item, dict) or item.get('attempt') != attempt_number]
        attempts.append(attempt_payload)
        payload['last_feedback'] = feedback
        payload['resume_from'] = 'repair'
        _write_json(artifact_dir / f'attempt-{attempt_number}-verification.json', attempt_payload)
        _write_json(artifact_dir / f'attempt-{attempt_number}-feedback.json', feedback)
        (artifact_dir / f'attempt-{attempt_number}.patch').write_text(
            '# Candidate rejected by host workspace policy before patch publication.\n'
            + ''.join(f'# Changed: {path}\n' for path in changed_files),
            encoding='utf-8',
        )
        _update_repair_summary(payload, audit=audit, attempt=attempt_payload)
        _record_activity(
            payload,
            update,
            phase='Policy',
            message=f'Attempt {attempt_number} was rejected: {violation}. Returning feedback to the fixer.',
        )

        shutil.rmtree(shadow)
        clone_workspace(baseline_dir, shadow)
        shadow_control.mkdir(exist_ok=True)
        _restore_control_artifacts(
            shadow_control,
            evidence=evidence,
            audit=audit_payload,
            feedback=feedback,
        )
        return feedback, candidate_digest

    def _pause_for_transport(
        self,
        payload: dict[str, object],
        update: Callable[[dict[str, object]], None],
        exc: AgentTransportError,
        *,
        stage: str,
    ) -> None:
        message = f'{exc} The repair checkpoint was preserved.'
        payload['resume_from'] = stage
        payload['recovery'] = _recovery_options(
            reason=message,
            can_continue=True,
            workspace_preserved=True,
            category=exc.kind,
        )
        _set_run(payload, update, status='paused', message=message, completed_at=_now())

    def _stop_repair(
        self,
        payload: dict[str, object],
        update: Callable[[dict[str, object]], None],
        *,
        status: str,
        message: str,
        category: str,
    ) -> None:
        payload['recovery'] = _recovery_options(
            reason=message,
            can_continue=True,
            workspace_preserved=True,
            category=category,
        )
        _set_run(payload, update, status=status, message=message, completed_at=_now())

    def can_resume(self, run_id: str) -> bool:
        return (self.state_dir / 'workspaces' / run_id).is_dir() and (self.state_dir / 'baselines' / run_id).is_dir()

    def discard(self, run_id: str) -> None:
        self._cleanup_workspace(run_id, remove_session_state=True)

    def _cleanup_workspace(self, run_id: str, *, remove_session_state: bool) -> None:
        shutil.rmtree(self.state_dir / 'workspaces' / run_id, ignore_errors=True)
        shutil.rmtree(self.state_dir / 'baselines' / run_id, ignore_errors=True)
        if remove_session_state:
            artifact_dir = self.state_dir / 'runs' / run_id
            for name in (
                'fixer-messages.json',
                'adk-fixer-messages.json',
                'fixer-plan.sqlite3',
                'opencode-session.json',
                'codex-sdk-session.json',
                'claude-agent-session.json',
            ):
                (artifact_dir / name).unlink(missing_ok=True)

    def _run_trace_workflow(
        self,
        payload: dict[str, object],
        *,
        result: AnalysisResult,
        settings: QASettings,
        cancel_event: threading.Event,
        update: Callable[[dict[str, object]], None],
        report_progress: Callable[[AgentProgress], None],
    ) -> None:
        _record_activity(
            payload,
            update,
            phase='Evidence',
            message=(
                'Selecting bounded chronological evidence for the complete session.'
                if payload['kind'] == 'checkpoints'
                else 'Preparing Studio state for agent-directed trace retrieval.'
            ),
        )
        retrieval_context: TraceQAContext | None = None
        if payload['kind'] == 'checkpoints':
            trace = next((item for item in result.traces if item.session_id == payload['session_id']), None)
            if trace is None or not trace.events:
                raise ValueError('the selected session has no trace events to summarize')
            events = sorted(trace.events, key=lambda event: (event.sequence, event.line_number))
            latest_sequence = max(event.sequence for event in events)
            previous_value = payload.get('previous_session_brief')
            previous = cast('dict[str, object]', previous_value) if isinstance(previous_value, dict) else None
            previous_cursor = _optional_int(previous.get('through_event_sequence')) if previous is not None else None
            previous_digest = _optional_string(previous.get('coverage_digest')) if previous is not None else None
            incremental = bool(
                previous is not None
                and previous_cursor is not None
                and previous_digest is not None
                and _trace_coverage_digest(trace, through_sequence=previous_cursor) == previous_digest
            )
            if not incremental:
                previous = None
                previous_cursor = None
            if previous is not None and previous_cursor is not None and latest_sequence <= previous_cursor:
                current = dict(previous)
                current.update(
                    {
                        'workflow': 'checkpoints',
                        'scope': 'session',
                        'event_count': len(events),
                        'turn_count': len({event.turn_id for event in events}),
                        'through_event_sequence': latest_sequence,
                        'coverage_digest': _trace_coverage_digest(trace, through_sequence=latest_sequence),
                    }
                )
                _set_run(
                    payload,
                    update,
                    status='summarized',
                    message=f'Session brief is current through all {len(events)} events.',
                    completed_at=_now(),
                    result=current,
                )
                return
            evidence = build_session_checkpoint_context(
                result,
                session_id=str(payload['session_id']),
                turn_id=_optional_string(payload.get('turn_id')),
                event_sequence=_optional_int(payload.get('event_sequence')),
                # Reserve part of the configured evidence budget for trace
                # retrieval directed by the selected summarizer.
                max_chars=max(4_000, min(32_000, settings.max_context_chars // 2)),
                previous_summary=previous,
                after_sequence=previous_cursor,
            )
            event_details_sampled = (
                bool(previous and previous.get('event_details_sampled'))
                or '[session event details sampled or truncated by context limit' in evidence
            )
        else:
            evidence = build_studio_state_context(
                result,
                session_id=str(payload['session_id']),
                turn_id=_optional_string(payload.get('turn_id')),
                event_sequence=_optional_int(payload.get('event_sequence')),
            )
            retrieval_context = TraceQAContext(
                result=result,
                question=(
                    'Investigate the selected session.'
                    if payload['kind'] == 'investigate'
                    else 'Extract durable memories from the selected session.'
                ),
                scope='session',
                session_id=str(payload['session_id']),
                turn_id=_optional_string(payload.get('turn_id')),
                event_sequence=_optional_int(payload.get('event_sequence')),
                view_state={},
                max_context_chars=settings.max_context_chars,
                cancel_event=cancel_event,
            )
        if _cancelled(payload, cancel_event, update):
            return
        if payload['kind'] == 'checkpoints':
            _set_run(
                payload,
                update,
                status='summarizing',
                message=(
                    'The selected agent is appending new events to the persisted session brief.'
                    if previous is not None
                    else 'The selected agent is reconstructing checkpoints across the complete session.'
                ),
            )
            retrieval_context = TraceQAContext(
                result=result,
                question='Update the complete session checkpoint brief.',
                scope='session',
                session_id=str(payload['session_id']),
                turn_id=_optional_string(payload.get('turn_id')),
                event_sequence=_optional_int(payload.get('event_sequence')),
                view_state={},
                max_context_chars=settings.max_context_chars,
                cancel_event=cancel_event,
            )
            checkpoint_summary = self.agents.summarize_checkpoints(
                evidence,
                settings,
                context=retrieval_context,
                progress=report_progress,
            )
            if _cancelled(payload, cancel_event, update):
                return
            checkpoint_payload = checkpoint_summary.model_dump(mode='json')
            if previous is not None:
                checkpoint_payload['checkpoints'] = _merge_session_checkpoints(previous, checkpoint_payload)
            previous_revision = _optional_int(previous_value.get('revision')) if isinstance(previous_value, dict) else 0
            revision = _optional_int(payload.get('session_brief_revision')) or (previous_revision or 0) + 1
            checkpoint_payload.update(
                {
                    'workflow': 'checkpoints',
                    'scope': 'session',
                    'revision': revision,
                    'event_count': len(events),
                    'turn_count': len({event.turn_id for event in events}),
                    'through_event_sequence': latest_sequence,
                    'coverage_digest': _trace_coverage_digest(trace, through_sequence=latest_sequence),
                    'event_details_sampled': event_details_sampled,
                    'appended_event_count': len(
                        [event for event in events if previous_cursor is None or event.sequence > previous_cursor]
                    ),
                    'generated_at': _now(),
                }
            )
            checkpoint_count = len(checkpoint_payload['checkpoints'])
            _set_run(
                payload,
                update,
                status='summarized',
                message=(
                    f'Updated the session brief through {len(events)} events with '
                    f'{checkpoint_count} checkpoint{"s" if checkpoint_count != 1 else ""}.'
                ),
                completed_at=_now(),
                result=checkpoint_payload,
            )
            return
        if payload['kind'] == 'investigate':
            _set_run(
                payload,
                update,
                status='investigating',
                message='Investigation agent is analyzing the selected session context.',
            )
            investigation = self.agents.investigate(
                evidence,
                settings,
                context=retrieval_context,
                progress=report_progress,
            )
            if _cancelled(payload, cancel_event, update):
                return
            investigation_payload = investigation.model_dump(mode='json')
            _set_run(
                payload,
                update,
                status='investigated',
                message='Session investigation completed.',
                completed_at=_now(),
                result={'workflow': 'investigation', **investigation_payload},
            )
            return
        _set_run(
            payload,
            update,
            status='extracting',
            message='Memory agent is extracting durable learning from the selected session.',
        )
        extraction = self.agents.extract_memories(
            evidence,
            settings,
            context=retrieval_context,
            progress=report_progress,
        )
        if _cancelled(payload, cancel_event, update):
            return
        extraction_payload = extraction.model_dump(mode='json')
        candidate_count = len(extraction.candidates)
        _set_run(
            payload,
            update,
            status='extracted',
            message=f'Extracted {candidate_count} memory candidate{"s" if candidate_count != 1 else ""}.',
            completed_at=_now(),
            result={'workflow': 'memories', **extraction_payload},
        )

    def _apply_verified_delta(
        self,
        payload: dict[str, object],
        update: Callable[[dict[str, object]], None],
        *,
        baseline: WorkspaceSnapshot,
        candidate: WorkspaceSnapshot,
        delta: WorkspaceDelta,
        source_path: Path,
        session_id: str,
        attempt_number: int,
        verdict: VerificationVerdict,
    ) -> bool:
        if self.source_workspace is None:
            raise RuntimeError('local source workspace is unavailable')
        backup_dir = _next_backup_dir(
            self.state_dir / 'backups' / str(payload['run_id']),
            attempt_number,
        )
        transaction = LocalWorkspaceTransaction(
            target=self.source_workspace,
            candidate=candidate,
            baseline=baseline,
            backup_dir=backup_dir,
        )
        _set_run(
            payload,
            update,
            status='applying',
            message='Applying the verified patch to the local source transactionally.',
        )
        try:
            transaction.apply(delta)
        except RuntimeError as exc:
            payload['recovery'] = _recovery_options(
                reason=str(exc),
                can_continue=False,
                workspace_preserved=True,
                category='source_conflict',
            )
            _set_run(
                payload,
                update,
                status='blocked',
                message=str(exc),
                completed_at=_now(),
            )
            return True
        try:
            raw_post_checks = self._check_runner(self.source_workspace, source_path, session_id)
            post_checks, baseline_checks, inherited_failures, new_failures = self._classify_candidate_checks(
                payload,
                artifact_dir=self.state_dir / 'runs' / str(payload['run_id']),
                baseline_workspace=baseline.root,
                candidate_workspace=self.source_workspace,
                source_path=source_path,
                session_id=session_id,
                candidate_checks=raw_post_checks,
                changed_files=delta.changed_files,
            )
            post_probe = _probe_or_error(self._probe_runner, self.source_workspace, source_path, session_id)
        except BaseException:
            transaction.rollback()
            raise
        if not _checks_passed(post_checks) or 'error' in post_probe:
            transaction.rollback()
            feedback = {
                'reason': 'post_apply_verification_failed',
                'checks': [asdict(check) for check in post_checks],
                'raw_checks': [asdict(check) for check in raw_post_checks],
                'baseline_checks': [asdict(check) for check in baseline_checks],
                'inherited_failed_checks': [asdict(check) for check in inherited_failures],
                'new_failed_checks': [asdict(check) for check in new_failures],
                'parser_probe': post_probe,
            }
            payload['last_feedback'] = feedback
            _set_run(
                payload,
                update,
                status='repairing',
                message='Post-apply verification failed; local source was rolled back before the next attempt.',
            )
            return False
        transaction.commit()
        _set_run(
            payload,
            update,
            status='passed',
            message=f'Local source passed verification on attempt {attempt_number}. Finalizing the runtime update.',
            completed_at=_now(),
            result={
                'changed_files': list(delta.changed_files),
                'backup_path': str(backup_dir),
                'change_digest': delta.digest,
                'source_delta': {
                    'added': list(delta.added),
                    'modified': list(delta.modified),
                    'deleted': list(delta.deleted),
                },
                'applied_records': {key: asdict(candidate.files[key]) for key in (*delta.added, *delta.modified)},
                'restart_required': True,
                'post_apply_checks': [asdict(check) for check in post_checks],
                'post_apply_raw_checks': [asdict(check) for check in raw_post_checks],
                'post_apply_inherited_failed_checks': [asdict(check) for check in inherited_failures],
                'parser_after': post_probe,
                'verifier': verdict.model_dump(mode='json'),
            },
        )
        return True


class _LegacyFixerSession:
    """Compatibility adapter for deterministic test and third-party backends."""

    def __init__(self, agents: RepairAgentBackend, workspace: Path, settings: QASettings) -> None:
        self.agents = agents
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
        del user_instruction, resume_pending_turn
        return self.agents.fix(
            self.workspace,
            self.settings,
            attempt=attempt,
            audit=audit,
            feedback=feedback,
            progress=progress,
        )


def _create_fixer_session(
    agents: RepairAgentBackend,
    workspace: Path,
    settings: QASettings,
    *,
    run_id: str,
    state_dir: Path,
) -> FixerSession:
    factory = getattr(agents, 'create_fixer_session', None)
    if callable(factory):
        return cast('FixerSession', factory(workspace, settings, run_id=run_id, state_dir=state_dir))
    return _LegacyFixerSession(agents, workspace, settings)


def _restore_control_artifacts(
    control: Path,
    *,
    evidence: dict[str, object],
    audit: dict[str, object],
    feedback: dict[str, object] | None,
) -> None:
    control.mkdir(parents=True, exist_ok=True)
    _write_json(control / 'audit-evidence.json', evidence)
    _write_json(control / 'audit-report.json', audit)
    if feedback is not None:
        _write_json(control / 'verification-feedback.json', feedback)


def _payload_max_attempts(payload: dict[str, object], fallback: int) -> int:
    value = payload.get('max_attempts')
    return min(max(value if isinstance(value, int) else fallback, 1), 10)


def _snapshot_digest(snapshot: WorkspaceSnapshot) -> str:
    digest = hashlib.sha256()
    for path, record in sorted(snapshot.files.items()):
        digest.update(f'{path}\0{record.kind}\0{record.digest}\0{record.mode}\n'.encode())
    return digest.hexdigest()


def _next_backup_dir(root: Path, attempt: int) -> Path:
    candidate = root / f'attempt-{attempt}'
    retry = 1
    while candidate.exists():
        candidate = root / f'attempt-{attempt}-retry-{retry}'
        retry += 1
    return candidate


def _last_failure_cycle(
    attempts: list[object],
    feedback: dict[str, object] | None,
) -> tuple[str, str] | None:
    if not attempts or feedback is None:
        return None
    last = attempts[-1]
    if not isinstance(last, dict):
        return None
    digest = last.get('change_digest')
    return (str(digest), _failure_signature(feedback)) if digest else None


def _workspace_change_summary(
    baseline: WorkspaceSnapshot,
    candidate: WorkspaceSnapshot,
) -> dict[str, list[str]]:
    baseline_files = set(baseline.files)
    candidate_files = set(candidate.files)
    return {
        'added': sorted(candidate_files - baseline_files),
        'modified': sorted(
            key for key in baseline_files & candidate_files if baseline.files[key] != candidate.files[key]
        ),
        'deleted': sorted(baseline_files - candidate_files),
    }


def _rejected_candidate_digest(
    candidate: WorkspaceSnapshot,
    changed_files: tuple[str, ...],
    category: str,
) -> str:
    records = [category]
    for key in sorted(changed_files):
        record = candidate.files.get(key)
        records.append(f'{key}:{record.digest if record is not None else "deleted"}')
    return hashlib.sha256('\n'.join(records).encode('utf-8')).hexdigest()


def _update_repair_summary(
    payload: dict[str, object],
    *,
    audit: ParserAudit,
    attempt: dict[str, object] | None = None,
) -> None:
    current = payload.get('repair_summary')
    summary = dict(current) if isinstance(current, dict) else {}
    summary['fixing'] = [issue.title for issue in audit.issues] or [audit.summary]
    for redundant_key in ('latest_repair', 'latest_verification', 'verification_failures', 'verification_status'):
        summary.pop(redundant_key, None)
    if attempt is not None:
        summary['changed_files'] = list(attempt.get('changed_files') or [])
    attempts_by_number: dict[int, dict[str, object]] = {}
    for index, item in enumerate(payload.get('attempts') or [], start=1):
        if isinstance(item, dict):
            number = item.get('attempt') if isinstance(item.get('attempt'), int) else index
            attempts_by_number[number] = item
    if attempt is not None and isinstance(attempt.get('verifier'), dict):
        number = attempt.get('attempt')
        attempts_by_number[number if isinstance(number, int) else len(attempts_by_number) + 1] = attempt
    fixed_items: list[dict[str, object]] = []
    for number, item in sorted(attempts_by_number.items()):
        verifier = item.get('verifier')
        verifier_payload = verifier if isinstance(verifier, dict) else {}
        values = verifier_payload.get('fixed_items')
        resolved = [str(value).strip() for value in values] if isinstance(values, list) else []
        resolved = [value for value in resolved if value]
        if not resolved and verifier_payload.get('status') == 'pass':
            resolved = [issue.title for issue in audit.issues] or [audit.summary]
        fixed_items.extend({'attempt': number, 'item': value} for value in resolved)
    summary['fixed_items'] = fixed_items[:24]
    payload['repair_summary'] = summary


def _recovery_options(
    *,
    reason: str,
    can_continue: bool,
    workspace_preserved: bool,
    category: str,
) -> dict[str, object]:
    actions = ['restart']
    if can_continue:
        actions.insert(0, 'continue')
    if workspace_preserved:
        actions.append('discard')
    return {
        'reason': reason,
        'category': category,
        'can_continue': can_continue,
        'workspace_preserved': workspace_preserved,
        'actions': actions,
    }


class RepairCoordinator:
    """Threaded, button-triggered job facade used by the loopback server."""

    def __init__(
        self,
        *,
        source_workspace: Path | None,
        state_dir: Path,
        max_attempts: int = 5,
        agents: RepairAgentBackend | None = None,
        deployment_managed: bool = False,
        source_authorizer: SourceActionAuthorizer | None = None,
    ) -> None:
        backend = agents or SelectableRepairAgents(state_dir)
        self.engine = RepairEngine(
            source_workspace=source_workspace,
            state_dir=state_dir,
            agents=backend,
            max_attempts=max_attempts,
            deployment_managed=deployment_managed,
        )
        self.store = RepairRunStore(state_dir / 'repair-runs.sqlite3')
        self.source_authorizer = source_authorizer or SourceActionAuthorizer()
        self._lock = threading.RLock()
        self._active_run_id: str | None = None
        self._cancel_events: dict[str, threading.Event] = {}
        self._startup_source_digest = (
            self._current_source_digest() if self.engine.source_workspace is not None else None
        )

    def status(self) -> dict[str, object]:
        with self._lock:
            active_run_id = self._active_run_id
        latest = self.store.latest()
        latest_is_historical = self._is_historical_source_run(latest)
        source_workspace = self.engine.source_workspace
        harnesses = self._harness_catalog()
        harness_id = self._harness_id()
        selected = next((item for item in harnesses if item.get('id') == harness_id), harnesses[0])
        coding_ready = bool(selected.get('available'))
        return {
            'available': True,
            'backend': self.engine.agents.label,
            'harness_id': harness_id,
            'harness_label': selected.get('label'),
            'harness_available': coding_ready,
            'harness_detail': selected.get('detail'),
            'harnesses': harnesses,
            'requires_api_settings': self._workflow_requires_api('audit'),
            'selection_notice': getattr(self.engine.agents, 'selection_notice', ''),
            'source_workspace': str(source_workspace) if source_workspace is not None else None,
            'workflows': {
                'investigate': True,
                'memories': True,
                'checkpoints': True,
                'audit': source_workspace is not None,
                'repair': source_workspace is not None and coding_ready,
                'customize': source_workspace is not None and coding_ready,
            },
            'max_attempts': self.engine.max_attempts,
            'active_run_id': active_run_id,
            'latest_run': None if latest_is_historical else _run_summary(latest),
            'latest_historical_run': _run_summary(latest) if latest_is_historical else None,
        }

    def configure_harness(self, harness: str) -> dict[str, object]:
        with self._lock:
            if self._active_run_id is not None:
                raise ValueError('the agent type cannot change while an agent workflow is running')
        select = getattr(self.engine.agents, 'select_harness', None)
        if not callable(select):
            if harness != self._harness_id():
                raise ValueError('this agent backend does not support changing agent types')
            return self.status()
        select(harness)
        return self.status()

    def prepare_source_action(
        self,
        action: SourceMutationAction,
        *,
        result: AnalysisResult,
        dashboard_state: dict[str, object],
        instruction: str,
        client_session_nonce: str,
        audit_run_id: str | None = None,
    ) -> dict[str, object]:
        """Prepare, but do not execute, a request-bound source-changing action."""

        if action not in {'repair_parser', 'customize_dashboard'}:
            raise ValueError('only repair or customization can be prepared from dashboard state')
        if self.engine.source_workspace is None:
            raise ValueError('source changes require an Agent Trace Studio source workspace')
        if action == 'customize_dashboard' and audit_run_id is not None:
            raise ValueError('completed audits may only seed parser repair approvals')
        with self._lock:
            if self._active_run_id is not None:
                raise ValueError('another agent workflow is already running')
        selection = _validated_selection(result, dashboard_state)
        baseline_digest = self._current_source_digest()
        if audit_run_id is not None:
            self._validate_actionable_audit(
                audit_run_id,
                selection=selection,
                baseline_digest=baseline_digest,
            )
        return self.source_authorizer.issue(
            action=action,
            message=instruction,
            dashboard_state=cast('dict[str, object]', selection['dashboard_state']),
            source_workspace=str(self.engine.source_workspace),
            baseline_digest=baseline_digest,
            activation_mode=VERIFIED_RUNTIME_ACTIVATION_MODE,
            client_session_nonce=client_session_nonce,
            audit_run_id=audit_run_id,
        )

    def prepare_run_action(
        self,
        run_id: str,
        *,
        action: str,
        result: AnalysisResult,
        instruction: str | None,
        client_session_nonce: str,
    ) -> dict[str, object]:
        """Prepare approval for controlling or activating a preserved source run."""

        action_name: SourceMutationAction
        if action == 'continue':
            action_name = 'continue_run'
        elif action == 'restart':
            action_name = 'restart_run'
        elif action == 'activate':
            action_name = 'activate_run'
        else:
            raise ValueError('only continue, restart, or activation requires source-action approval')
        payload = self.get(run_id)
        if payload.get('kind') not in _SOURCE_CHANGE_KINDS:
            raise ValueError('only source-changing workflows use source-action approval')
        dashboard_state = payload.get('dashboard_state')
        if not isinstance(dashboard_state, dict):
            raise ValueError('the original dashboard selection is unavailable')
        selection, _ = _validated_recovery_selection(result, dashboard_state)
        result_payload = payload.get('result')
        change_digest = (
            _optional_string(result_payload.get('change_digest')) if isinstance(result_payload, dict) else None
        )
        if action == 'activate':
            if payload.get('status') != 'passed' or not isinstance(result_payload, dict):
                raise ValueError('only a verified source run can be activated')
            if not result_payload.get('restart_required') or change_digest is None:
                raise ValueError('this run does not have a pending verified runtime activation')
        message = instruction or (
            'Continue the preserved source-change workflow.'
            if action == 'continue'
            else (
                'Start the source-change workflow over.'
                if action == 'restart'
                else 'Activate this exact verified source revision.'
            )
        )
        return self.source_authorizer.issue(
            action=action_name,
            message=message,
            dashboard_state=cast('dict[str, object]', selection['dashboard_state']),
            source_workspace=str(self.engine.source_workspace),
            baseline_digest=self._current_source_digest(),
            activation_mode=VERIFIED_RUNTIME_ACTIVATION_MODE,
            client_session_nonce=client_session_nonce,
            run_id=run_id,
            change_digest=change_digest if action == 'activate' else None,
        )

    def approve_source_action(
        self,
        *,
        authorization_id: str,
        token: str,
        client_session_nonce: str,
        result: AnalysisResult,
        settings: QASettings,
    ) -> dict[str, object]:
        """Consume one approval and dispatch its immutable bound operation."""

        authorization = self.source_authorizer.consume(
            authorization_id=authorization_id,
            token=token,
            client_session_nonce=client_session_nonce,
        )
        if authorization.source_workspace != str(self.engine.source_workspace):
            raise ValueError('approved source workspace no longer matches')
        if authorization.action in {'repair_parser', 'customize_dashboard'}:
            kind: RepairKind = 'repair' if authorization.action == 'repair_parser' else 'customize'
            return self.start(
                kind,
                result=result,
                dashboard_state=authorization.dashboard_state(),
                settings=settings,
                audit_run_id=authorization.audit_run_id,
                instruction=authorization.message,
                authorization=authorization,
            )
        if authorization.action == 'activate_run':
            return self._authorize_pending_activation(authorization)
        if authorization.run_id is None:
            raise ValueError('approved run action is missing its run ID')
        return self.act(
            authorization.run_id,
            action='continue' if authorization.action == 'continue_run' else 'restart',
            result=result,
            settings=settings,
            instruction=authorization.message,
            authorization=authorization,
        )

    def cancel_source_action(
        self,
        *,
        authorization_id: str,
        token: str,
        client_session_nonce: str,
    ) -> None:
        self.source_authorizer.cancel(
            authorization_id=authorization_id,
            token=token,
            client_session_nonce=client_session_nonce,
        )

    def start(
        self,
        kind: RepairKind,
        *,
        result: AnalysisResult,
        dashboard_state: dict[str, object],
        settings: QASettings,
        audit_run_id: str | None = None,
        instruction: str | None = None,
        authorization: ApprovedSourceAction | None = None,
    ) -> dict[str, object]:
        if kind not in {'investigate', 'memories', 'checkpoints', 'audit', 'repair', 'customize'}:
            raise ValueError('unknown agent workflow')
        if not settings.configured and self._workflow_requires_api(kind):
            raise ValueError('Configure an API provider before starting an agent workflow')
        selected_harness = self._workflow_status(settings)
        if kind in _SELECTED_HARNESS_KINDS and not selected_harness.get('available'):
            raise ValueError(str(selected_harness.get('detail') or 'the selected coding harness is unavailable'))
        if kind in {'audit', 'repair', 'customize'} and self.engine.source_workspace is None:
            raise ValueError('source audit, repair, and customization require an Agent Trace Studio source workspace')
        workflow_instruction = _optional_string(instruction)
        customization_request = workflow_instruction if kind == 'customize' else None
        if kind == 'customize' and customization_request is None:
            raise ValueError('describe the dashboard customization before starting the agent')
        if kind in _SOURCE_CHANGE_KINDS and workflow_instruction is not None:
            if len(workflow_instruction) > 4_000:
                raise ValueError('source-change instruction must be 4000 characters or fewer')
        elif instruction is not None:
            raise ValueError('an initial instruction is only supported for source-changing workflows')
        selection = _validated_selection(result, dashboard_state)
        if kind in _SOURCE_CHANGE_KINDS:
            self._validate_start_authorization(
                kind=kind,
                selection=selection,
                instruction=workflow_instruction,
                audit_run_id=audit_run_id,
                authorization=authorization,
            )
        seed_audit: dict[str, object] | None = None
        if audit_run_id is not None:
            if kind != 'repair':
                raise ValueError('a completed audit may only seed a repair run')
            audit_run = self.get(audit_run_id)
            audit_value = audit_run.get('audit')
            if (
                audit_run.get('kind') != 'audit'
                or audit_run.get('status') != 'audited'
                or not isinstance(audit_value, dict)
                or not audit_value.get('requires_fix')
            ):
                raise ValueError('Fix it requires a completed audit with supported issues')
            for key in ('session_id', 'turn_id', 'event_sequence'):
                if audit_run.get(key) != selection[key]:
                    raise ValueError('the completed audit does not match the selected dashboard context')
            if audit_run.get('source_workspace') != str(self.engine.source_workspace):
                raise ValueError('the completed audit belongs to a different source workspace')
            seed_audit = audit_run
        previous_brief_run = (
            self.store.latest_session_brief(str(selection['session_id'])) if kind == 'checkpoints' else None
        )
        previous_brief_value = previous_brief_run.get('result') if previous_brief_run is not None else None
        previous_brief = (
            cast('dict[str, object]', previous_brief_value) if isinstance(previous_brief_value, dict) else None
        )
        previous_revision = _optional_int(previous_brief.get('revision')) if previous_brief is not None else 0
        with self._lock:
            if self._active_run_id is not None:
                raise ValueError('another agent workflow is already running')
            run_id = uuid.uuid4().hex
            created_at = _now()
            payload: dict[str, object] = {
                'run_id': run_id,
                'kind': kind,
                'status': 'queued',
                'message': 'Agent workflow queued.',
                'created_at': created_at,
                'updated_at': created_at,
                'completed_at': None,
                'session_id': selection['session_id'],
                'turn_id': selection['turn_id'],
                'event_sequence': selection['event_sequence'],
                'dashboard_state': selection['dashboard_state'],
                'source_workspace': (
                    str(self.engine.source_workspace) if self.engine.source_workspace is not None else None
                ),
                'max_attempts': self.engine.max_attempts,
                'attempt': 0,
                'attempts': [],
                'activity': [],
                'audit': seed_audit.get('audit') if seed_audit is not None else None,
                'result': None,
                'repair_summary': seed_audit.get('repair_summary') if seed_audit is not None else None,
                'recovery': None,
                'resume_from': 'setup',
                'pending_model_turn': False,
                'provider': selected_harness.get('provider', settings.provider),
                'model': selected_harness.get('model', settings.model),
                'harness': self._harness_id(),
                'harness_label': selected_harness.get('label'),
                'customization_request': customization_request,
                'user_instruction': workflow_instruction,
                'seed_audit_run_id': audit_run_id,
                'seed_audit_source_digest': (
                    seed_audit.get('source_snapshot_digest') if seed_audit is not None else None
                ),
                'previous_session_brief': previous_brief,
                'previous_brief_run_id': previous_brief_run.get('run_id') if previous_brief_run is not None else None,
                'session_brief_revision': (previous_revision or 0) + 1 if kind == 'checkpoints' else None,
                'source_authorization': (
                    {
                        'id': authorization.authorization_id,
                        'action': authorization.action,
                        'request_hash': authorization.request_hash,
                        'selection_hash': authorization.selection_hash,
                        'source_workspace': authorization.source_workspace,
                        'baseline_digest': authorization.baseline_digest,
                        'activation_mode': authorization.activation_mode,
                        'issued_at': authorization.issued_at,
                        'expires_at': authorization.expires_at,
                    }
                    if authorization is not None
                    else None
                ),
                'authorized_source_digest': authorization.baseline_digest if authorization is not None else None,
            }
            _append_activity(
                payload,
                phase='Queue',
                message='Agent workflow queued.',
                timestamp=created_at,
            )
            self.store.save(payload)
            cancel_event = threading.Event()
            self._cancel_events[run_id] = cancel_event
            self._active_run_id = run_id
            thread = threading.Thread(
                target=self._run_worker,
                args=(
                    payload,
                    result,
                    selection['source_path'],
                    settings,
                    cancel_event,
                    False,
                    workflow_instruction,
                ),
                name=f'agent-trace-{kind}-{run_id[:8]}',
                daemon=True,
            )
            thread.start()
            return payload

    def get(self, run_id: str) -> dict[str, object]:
        if not run_id or len(run_id) > 64 or not run_id.isalnum():
            raise ValueError('invalid agent run ID')
        payload = self.store.get(run_id)
        if payload is None:
            raise ValueError('agent run not found')
        return payload

    def public_get(self, run_id: str) -> dict[str, object]:
        """Return a run with source-stale terminal state clearly retired."""

        payload = self.get(run_id)
        if not self._is_historical_source_run(payload):
            return payload
        response = dict(payload)
        response['historical'] = True
        response['historical_reason'] = (
            'This run belongs to an older source snapshot and no longer describes the running dashboard.'
        )
        response['original_message'] = payload.get('message')
        response['message'] = str(response['historical_reason'])
        response['recovery'] = None
        return response

    def pending_deployments(self) -> list[dict[str, object]]:
        return self.store.pending_deployments()

    def session_brief(self, result: AnalysisResult, session_id: str) -> dict[str, object]:
        """Return the durable session brief plus its current live-coverage state."""

        trace = next((item for item in result.traces if item.session_id == session_id), None)
        if trace is None:
            raise ValueError('the selected trace is no longer loaded')
        events = sorted(trace.events, key=lambda event: (event.sequence, event.line_number))
        latest_sequence = max((event.sequence for event in events), default=0)
        latest = self.store.latest_session_brief(session_id)
        with self._lock:
            active_run_id = self._active_run_id
        active = self.store.get(active_run_id) if active_run_id is not None else None
        updating = bool(active and active.get('kind') == 'checkpoints' and active.get('session_id') == session_id)
        if latest is None:
            return {
                'available': False,
                'session_id': session_id,
                'status': 'updating' if updating else 'missing',
                'run_id': None,
                'active_run_id': active_run_id if updating else None,
                'result': None,
                'event_count': len(events),
                'through_event_sequence': 0,
                'latest_event_sequence': latest_sequence,
                'new_event_count': len(events),
                'stale': bool(events),
            }
        brief_value = latest.get('result')
        brief = cast('dict[str, object]', brief_value) if isinstance(brief_value, dict) else {}
        cursor = _optional_int(brief.get('through_event_sequence')) or 0
        coverage_digest = _optional_string(brief.get('coverage_digest'))
        prefix_matches = bool(
            coverage_digest and _trace_coverage_digest(trace, through_sequence=cursor) == coverage_digest
        )
        new_event_count = len([event for event in events if event.sequence > cursor]) if prefix_matches else len(events)
        stale = not prefix_matches or bool(new_event_count)
        return {
            'available': True,
            'session_id': session_id,
            'status': 'updating' if updating else ('out_of_date' if stale else 'current'),
            'run_id': latest.get('run_id'),
            'active_run_id': active_run_id if updating else None,
            'updated_at': latest.get('completed_at') or latest.get('updated_at'),
            'revision': brief.get('revision'),
            'result': brief,
            'event_count': len(events),
            'through_event_sequence': cursor,
            'latest_event_sequence': latest_sequence,
            'new_event_count': new_event_count,
            'stale': stale,
            'rebuild_required': not prefix_matches,
        }

    def _is_historical_source_run(self, payload: dict[str, object] | None) -> bool:
        if payload is None or payload.get('kind') not in _SOURCE_CHANGE_KINDS:
            return False
        if payload.get('status') not in _PRESERVE_REPAIR_STATUSES:
            return False
        digest = _optional_string(payload.get('source_snapshot_digest'))
        return digest is not None and self._startup_source_digest is not None and digest != self._startup_source_digest

    def _authorize_pending_activation(self, authorization: ApprovedSourceAction) -> dict[str, object]:
        run_id = authorization.run_id
        if run_id is None:
            raise ValueError('runtime activation approval is missing its run ID')
        payload = self.get(run_id)
        result = payload.get('result')
        if payload.get('status') != 'passed' or not isinstance(result, dict) or not result.get('restart_required'):
            raise ValueError('the verified runtime activation is no longer pending')
        if authorization.source_workspace != str(self.engine.source_workspace):
            raise ValueError('runtime activation approval belongs to a different workspace')
        if authorization.baseline_digest != self._current_source_digest():
            raise ValueError('local source changed after activation approval was requested')
        if authorization.change_digest != result.get('change_digest'):
            raise ValueError('runtime activation approval does not match the verified change')
        if authorization.dashboard_state() != payload.get('dashboard_state'):
            raise ValueError('runtime activation approval does not match the original dashboard selection')
        payload['source_authorization'] = {
            'id': authorization.authorization_id,
            'action': authorization.action,
            'request_hash': authorization.request_hash,
            'selection_hash': authorization.selection_hash,
            'source_workspace': authorization.source_workspace,
            'baseline_digest': authorization.baseline_digest,
            'change_digest': authorization.change_digest,
            'activation_mode': authorization.activation_mode,
            'issued_at': authorization.issued_at,
            'expires_at': authorization.expires_at,
        }
        timestamp = _now()
        _append_activity(
            payload,
            phase='Deployment',
            message='User approved activation of the exact verified source revision.',
            timestamp=timestamp,
        )
        payload['updated_at'] = timestamp
        self.store.save(payload)
        return payload

    def record_deployment_result(
        self,
        run_id: str,
        deployment: dict[str, object],
    ) -> dict[str, object]:
        """Record candidate promotion or rollback against a preserved fixer run."""

        payload = self.get(run_id)
        status = str(deployment.get('status') or '')
        if status not in {'promoted', 'failed', 'rolled_back'}:
            raise ValueError('deployment status must be promoted, failed, or rolled_back')
        result_value = payload.get('result')
        result = dict(result_value) if isinstance(result_value, dict) else {}
        result['deployment'] = deployment
        payload['result'] = result
        payload['deployment'] = deployment
        _append_activity(
            payload,
            phase='Deployment',
            message=str(deployment.get('message') or f'Runtime deployment {status}.'),
            timestamp=_now(),
        )
        if status == 'promoted':
            result['restart_required'] = False
            result['runtime_restarted'] = True
            payload['recovery'] = None
            _set_run(
                payload,
                self.store.save,
                status='passed',
                message='The verified source was activated and passed runtime health checks.',
                completed_at=_now(),
            )
            return payload

        rollback = deployment.get('rollback')
        rollback_payload = rollback if isinstance(rollback, dict) else {}
        source_restored = rollback_payload.get('status') == 'restored'
        can_continue = source_restored and self.engine.can_resume(run_id)
        result['restart_required'] = False
        result['runtime_restarted'] = False
        payload['resume_from'] = 'repair'
        payload['last_feedback'] = {
            'reason': 'runtime_activation_failed',
            'deployment': deployment,
        }
        payload['recovery'] = _recovery_options(
            reason=str(deployment.get('message') or 'The candidate runtime failed verification.'),
            can_continue=can_continue,
            workspace_preserved=self.engine.can_resume(run_id),
            category='deployment_failed',
        )
        _set_run(
            payload,
            self.store.save,
            status='paused' if can_continue else 'blocked',
            message=(
                'Candidate runtime failed; the previous source and runtime were restored. '
                'Continue to resume the same fixer session with deployment feedback.'
                if can_continue
                else 'Candidate runtime failed and automatic source rollback needs user attention.'
            ),
            completed_at=_now(),
        )
        return payload

    def record_bundle_refresh(
        self,
        run_id: str,
        refresh: dict[str, object],
    ) -> dict[str, object]:
        """Finalize an asset-only source change after regenerating the served bundle."""

        payload = self.get(run_id)
        result_value = payload.get('result')
        result = dict(result_value) if isinstance(result_value, dict) else {}
        result['restart_required'] = False
        result['dashboard_refreshed'] = True
        result['dashboard_refresh'] = refresh
        payload['result'] = result
        _append_activity(
            payload,
            phase='Deployment',
            message='The updated dashboard bundle was generated without a process restart.',
            timestamp=_now(),
        )
        _set_run(
            payload,
            self.store.save,
            status='passed',
            message='Local source passed verification and the updated dashboard bundle is ready.',
            completed_at=_now(),
        )
        self.engine.discard(run_id)
        return payload

    def reusable_audit_run_id(
        self,
        *,
        result: AnalysisResult,
        dashboard_state: dict[str, object],
    ) -> str | None:
        """Return the latest actionable audit when it matches the current immutable selection."""

        payload = self.store.latest()
        if payload is None or payload.get('kind') != 'audit' or payload.get('status') != 'audited':
            return None
        audit = payload.get('audit')
        if not isinstance(audit, dict) or not audit.get('requires_fix'):
            return None
        selection = _validated_selection(result, dashboard_state)
        if any(payload.get(key) != selection[key] for key in ('session_id', 'turn_id', 'event_sequence')):
            return None
        if payload.get('source_workspace') != str(self.engine.source_workspace):
            return None
        if payload.get('source_snapshot_digest') != self._current_source_digest():
            return None
        return str(payload['run_id'])

    def cancel(self, run_id: str) -> dict[str, object]:
        with self._lock:
            cancel_event = self._cancel_events.get(run_id)
        if cancel_event is None:
            payload = self.get(run_id)
            if payload.get('status') in _TERMINAL_STATUSES:
                return payload
            raise ValueError('agent run is not active')
        cancel_event.set()
        payload = self.get(run_id)
        payload['message'] = 'Cancellation requested; stopping after the current agent step.'
        payload['updated_at'] = _now()
        _append_activity(
            payload,
            phase='Run',
            message='Cancellation requested; waiting for the current agent step.',
            timestamp=str(payload['updated_at']),
        )
        self.store.save(payload)
        return payload

    def act(
        self,
        run_id: str,
        *,
        action: str,
        result: AnalysisResult,
        settings: QASettings,
        instruction: str | None = None,
        authorization: ApprovedSourceAction | None = None,
    ) -> dict[str, object]:
        if action not in {'continue', 'restart', 'discard'}:
            raise ValueError('unknown agent run action')
        if instruction is not None and len(instruction) > 2_000:
            raise ValueError('continuation instruction must be 2000 characters or fewer')
        payload = self.get(run_id)
        if action in {'continue', 'restart'}:
            self._validate_run_authorization(
                run_id=run_id,
                action=action,
                payload=payload,
                authorization=authorization,
            )
        with self._lock:
            if self._active_run_id is not None:
                raise ValueError('another agent workflow is already running')
        if action == 'discard':
            self.engine.discard(run_id)
            payload['recovery'] = None
            _set_run(
                payload,
                self.store.save,
                status='discarded',
                message='Preserved repair workspace discarded; local source was not changed.',
                completed_at=_now(),
            )
            return payload
        if action == 'restart':
            dashboard_state = (
                authorization.dashboard_state() if authorization is not None else payload.get('dashboard_state')
            )
            if not isinstance(dashboard_state, dict):
                raise ValueError('the original dashboard selection is unavailable')
            kind = str(payload.get('kind') or '')
            self.engine.discard(run_id)
            payload['recovery'] = None
            _set_run(
                payload,
                self.store.save,
                status='discarded',
                message='Previous candidate discarded before starting a new run.',
                completed_at=_now(),
            )
            return self.start(
                cast('RepairKind', kind),
                result=result,
                dashboard_state=dashboard_state,
                settings=settings,
                instruction=_optional_string(payload.get('user_instruction')),
                authorization=authorization,
            )

        recovery = payload.get('recovery')
        if not isinstance(recovery, dict) or not recovery.get('can_continue'):
            raise ValueError('this run cannot continue; start a new run instead')
        if not settings.configured and self._workflow_requires_api(str(payload.get('kind') or 'repair')):
            raise ValueError('Configure an API provider before continuing the agent workflow')
        if not self.engine.can_resume(run_id):
            raise ValueError('the preserved repair workspace is unavailable; start a new run instead')
        dashboard_state = payload.get('dashboard_state')
        if not isinstance(dashboard_state, dict):
            raise ValueError('the original dashboard selection is unavailable')
        selection, selection_fallback = _validated_recovery_selection(result, dashboard_state)
        if selection_fallback:
            payload['dashboard_state'] = selection['dashboard_state']
            payload['turn_id'] = selection['turn_id']
            payload['event_sequence'] = selection['event_sequence']
            _append_activity(
                payload,
                phase='Resume',
                message=selection_fallback,
                timestamp=_now(),
            )
        attempts = payload.get('attempts')
        attempt_count = len(attempts) if isinstance(attempts, list) else 0
        maximum = _payload_max_attempts(payload, self.engine.max_attempts)
        if attempt_count >= maximum:
            if maximum >= 10:
                raise ValueError('this run reached the ten-attempt safety limit; start a new run instead')
            payload['max_attempts'] = min(maximum + 2, 10)
        previous_provider = str(payload.get('provider') or '')
        previous_model = str(payload.get('model') or '')
        previous_harness = str(payload.get('harness') or '')
        selected_harness = self._workflow_status(settings)
        current_harness = self._harness_id()
        if payload.get('kind') in _SELECTED_HARNESS_KINDS and not selected_harness.get('available'):
            raise ValueError(str(selected_harness.get('detail') or 'the selected coding harness is unavailable'))
        current_provider = str(selected_harness.get('provider', settings.provider))
        current_model = str(selected_harness.get('model', settings.model))
        provider_changed = (previous_provider, previous_model) != (current_provider, current_model)
        harness_changed = previous_harness != current_harness
        if provider_changed or harness_changed:
            fallbacks = payload.get('fallbacks')
            fallback_history = fallbacks if isinstance(fallbacks, list) else []
            fallback_history.append(
                {
                    'at': _now(),
                    'from_provider': previous_provider,
                    'from_model': previous_model,
                    'to_provider': current_provider,
                    'to_model': current_model,
                    'from_harness': previous_harness,
                    'to_harness': current_harness,
                }
            )
            payload['fallbacks'] = fallback_history[-8:]
            fallback_label = str(selected_harness.get('label') or current_harness)
            _append_activity(
                payload,
                phase='Fallback',
                message=f'Continuing with {fallback_label} and model {current_model}.',
                timestamp=_now(),
            )
            payload['pending_model_turn'] = False
            if not instruction or instruction == 'Continue the preserved source-change workflow.':
                instruction = (
                    'Continue from the existing fixer checkpoint after the provider or model change, or after '
                    'changing coding harness. '
                    'Re-check the current candidate before making additional edits.'
                )
        payload['provider'] = current_provider
        payload['model'] = current_model
        payload['harness'] = current_harness
        payload['harness_label'] = selected_harness.get('label')
        if authorization is not None:
            payload['source_authorization'] = {
                'id': authorization.authorization_id,
                'action': authorization.action,
                'request_hash': authorization.request_hash,
                'selection_hash': authorization.selection_hash,
                'source_workspace': authorization.source_workspace,
                'baseline_digest': authorization.baseline_digest,
                'activation_mode': authorization.activation_mode,
                'issued_at': authorization.issued_at,
                'expires_at': authorization.expires_at,
            }
        payload['status'] = 'queued'
        payload['message'] = 'Continuing from the preserved repair checkpoint.'
        payload['completed_at'] = None
        payload['updated_at'] = _now()
        payload['recovery'] = None
        _append_activity(
            payload,
            phase='Resume',
            message='User requested continuation from the preserved checkpoint.',
            timestamp=str(payload['updated_at']),
        )
        self.store.save(payload)
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[run_id] = cancel_event
            self._active_run_id = run_id
        thread = threading.Thread(
            target=self._run_worker,
            args=(
                payload,
                result,
                selection['source_path'],
                settings,
                cancel_event,
                True,
                instruction,
            ),
            name=f'agent-trace-resume-{run_id[:8]}',
            daemon=True,
        )
        thread.start()
        return payload

    def _current_source_digest(self) -> str:
        source_workspace = self.engine.source_workspace
        if source_workspace is None:
            raise ValueError('source changes require an Agent Trace Studio source workspace')
        return _snapshot_digest(snapshot_workspace(source_workspace))

    def _validate_actionable_audit(
        self,
        audit_run_id: str,
        *,
        selection: dict[str, object],
        baseline_digest: str,
    ) -> dict[str, object]:
        audit_run = self.get(audit_run_id)
        audit = audit_run.get('audit')
        if (
            audit_run.get('kind') != 'audit'
            or audit_run.get('status') != 'audited'
            or not isinstance(audit, dict)
            or not audit.get('requires_fix')
        ):
            raise ValueError('source approval requires a completed audit with supported issues')
        for key in ('session_id', 'turn_id', 'event_sequence'):
            if audit_run.get(key) != selection[key]:
                raise ValueError('the completed audit does not match the selected dashboard context')
        if audit_run.get('source_workspace') != str(self.engine.source_workspace):
            raise ValueError('the completed audit belongs to a different source workspace')
        if audit_run.get('source_snapshot_digest') != baseline_digest:
            raise ValueError('the completed audit does not match the current source snapshot')
        return audit_run

    def _validate_start_authorization(
        self,
        *,
        kind: RepairKind,
        selection: dict[str, object],
        instruction: str | None,
        audit_run_id: str | None,
        authorization: ApprovedSourceAction | None,
    ) -> None:
        if authorization is None:
            raise ValueError('source-changing workflows require an approved one-time source action')
        expected_action: SourceMutationAction = 'repair_parser' if kind == 'repair' else 'customize_dashboard'
        if authorization.action not in {expected_action, 'restart_run'}:
            raise ValueError('source action approval does not match the requested workflow')
        if authorization.source_workspace != str(self.engine.source_workspace):
            raise ValueError('source action approval belongs to a different workspace')
        if authorization.baseline_digest != self._current_source_digest():
            raise ValueError('local source changed after approval was requested; request a new approval')
        if authorization.activation_mode != VERIFIED_RUNTIME_ACTIVATION_MODE:
            raise ValueError('source action approval does not authorize verified runtime activation')
        if authorization.dashboard_state() != selection['dashboard_state']:
            raise ValueError('source action approval does not match the dashboard selection')
        if authorization.action != 'restart_run' and authorization.message != instruction:
            raise ValueError('source action approval does not match the requested instruction')
        if authorization.action != 'restart_run' and authorization.audit_run_id != audit_run_id:
            raise ValueError('source action approval does not match the selected audit')

    def _validate_run_authorization(
        self,
        *,
        run_id: str,
        action: str,
        payload: dict[str, object],
        authorization: ApprovedSourceAction | None,
    ) -> None:
        if authorization is None:
            raise ValueError('continuing a source-changing workflow requires one-time approval')
        expected: SourceMutationAction = 'continue_run' if action == 'continue' else 'restart_run'
        if authorization.action != expected or authorization.run_id != run_id:
            raise ValueError('source action approval does not match the requested run action')
        if payload.get('kind') not in _SOURCE_CHANGE_KINDS:
            raise ValueError('source action approval cannot control a read-only workflow')
        if authorization.source_workspace != str(self.engine.source_workspace):
            raise ValueError('source action approval belongs to a different workspace')
        if authorization.baseline_digest != self._current_source_digest():
            raise ValueError('local source changed after approval was requested; request a new approval')
        if authorization.activation_mode != VERIFIED_RUNTIME_ACTIVATION_MODE:
            raise ValueError('source action approval does not authorize verified runtime activation')

    def close(self) -> None:
        with self._lock:
            events = tuple(self._cancel_events.values())
        for event in events:
            event.set()

    def _harness_catalog(self) -> list[dict[str, object]]:
        catalog = getattr(self.engine.agents, 'harness_catalog', None)
        if callable(catalog):
            values = catalog()
            if isinstance(values, list) and values:
                return [value for value in values if isinstance(value, dict)]
        return [
            {
                'id': 'fixed',
                'label': self.engine.agents.label,
                'available': True,
                'detail': 'This server uses a fixed agent backend.',
                'version': '',
                'model': '',
            }
        ]

    def _harness_id(self) -> str:
        value = getattr(self.engine.agents, 'harness_id', 'fixed')
        return str(value or 'fixed')

    def _selected_harness(self) -> dict[str, object]:
        harness_id = self._harness_id()
        catalog = self._harness_catalog()
        return next((item for item in catalog if item.get('id') == harness_id), catalog[0])

    def _workflow_requires_api(self, kind: str) -> bool:
        required = getattr(self.engine.agents, 'requires_api_settings', None)
        if required is not None:
            return bool(required)
        if kind == 'checkpoints':
            return bool(getattr(self.engine.agents, 'checkpoint_requires_api_settings', True))
        return True

    def _workflow_status(self, settings: QASettings) -> dict[str, object]:
        workflow_status = getattr(self.engine.agents, 'workflow_status', None)
        if callable(workflow_status):
            return workflow_status(settings)
        return {**self._selected_harness(), 'provider': settings.provider, 'model': settings.model}

    def _run_worker(
        self,
        payload: dict[str, object],
        result: AnalysisResult,
        source_path: Path,
        settings: QASettings,
        cancel_event: threading.Event,
        resume: bool,
        user_instruction: str | None,
    ) -> None:
        run_id = str(payload['run_id'])
        try:
            self.engine.run(
                payload,
                result=result,
                source_path=source_path,
                settings=settings,
                cancel_event=cancel_event,
                update=self.store.save,
                resume=resume,
                user_instruction=user_instruction,
            )
        except BaseException as exc:
            message = _safe_error(exc, settings)
            can_continue = payload.get('kind') in _SOURCE_CHANGE_KINDS and self.engine.can_resume(run_id)
            payload['recovery'] = _recovery_options(
                reason=message,
                can_continue=can_continue,
                workspace_preserved=can_continue,
                category='unexpected_error',
            )
            _set_run(
                payload,
                self.store.save,
                status='failed',
                message=message,
                completed_at=_now(),
            )
        finally:
            with self._lock:
                self._cancel_events.pop(run_id, None)
                if self._active_run_id == run_id:
                    self._active_run_id = None


def build_parser_audit_evidence(
    result: AnalysisResult,
    *,
    session_id: str,
    turn_id: str | None,
    event_sequence: int | None,
) -> dict[str, object]:
    """Build bounded structural evidence without raw prompts, outputs, or encrypted reasoning."""

    trace = next((item for item in result.traces if item.session_id == session_id), None)
    if trace is None:
        raise ValueError(f'no trace found for session {session_id}')
    rows = _read_journal_rows(Path(trace.session_file))
    represented_lines = {
        line for event in trace.events for line in (event.line_number, event.output_line_number) if line is not None
    }
    top_types: Counter[str] = Counter()
    payload_types: Counter[str] = Counter()
    signatures: Counter[str] = Counter()
    represented_signatures: Counter[str] = Counter()
    for line_number, row in enumerate(rows.rows, start=1):
        if not isinstance(row, dict):
            signatures['non_object'] += 1
            continue
        signature = _record_signature(row)
        signatures[signature] += 1
        if line_number in represented_lines:
            represented_signatures[signature] += 1
        top_types[str(row.get('type') or 'unknown')] += 1
        payload = row.get('payload')
        if isinstance(payload, dict):
            payload_types[str(payload.get('type') or 'none')] += 1
    selected_event = _selected_event(trace, turn_id, event_sequence)
    ordered_signatures = signatures.most_common(_MAX_EVIDENCE_SIGNATURES)
    included = {signature for signature, _count in ordered_signatures}
    samples: dict[str, object] = {}
    for row in rows.rows:
        signature = _record_signature(row) if isinstance(row, dict) else 'non_object'
        if signature not in included or signature in samples:
            continue
        samples[signature] = _shape(row) if isinstance(row, dict) else {'value_type': type(row).__name__}
        if len(samples) == len(included):
            break
    return {
        'schema': 'agent-trace-studio.parser-audit.v1',
        'content_policy': {
            'raw_rows_included': False,
            'raw_text_included': False,
            'encrypted_reasoning_included': False,
            'structural_samples_only': True,
        },
        'selection': {
            'session_id': session_id,
            'turn_id': turn_id,
            'event_sequence': event_sequence,
        },
        'journal': {
            'rows': len(rows.rows),
            'malformed_rows': rows.malformed_rows,
            'top_level_types': dict(sorted(top_types.items())),
            'payload_types': dict(sorted(payload_types.items())),
            'signature_count': len(signatures),
            'signatures_included': len(included),
            'signatures_omitted': max(len(signatures) - len(included), 0),
        },
        'record_shapes': [
            {
                'signature': signature,
                'count': count,
                'represented_rows': represented_signatures[signature],
                'structural_sample': samples[signature],
            }
            for signature, count in ordered_signatures
        ],
        'normalized_trace': {
            'source_rows': trace.source_rows,
            'events_total': trace.events_total,
            'collapsed_rows': trace.collapsed_rows,
            'truncated_events': trace.truncated_events,
            'category_counts': dict(sorted(Counter(event.category for event in trace.events).items())),
            'kind_counts': dict(sorted(Counter(event.kind for event in trace.events).items())),
            'selected_event': _event_metadata(selected_event),
        },
        'parse_issues': [
            {'reason': issue.reason, 'count': issue.count}
            for issue in result.issues
            if issue.session_file == trace.session_file
        ],
    }


def run_parser_probe(workspace: Path, source_path: Path, session_id: str) -> dict[str, object]:
    environment = _subprocess_environment(workspace)
    command = [
        sys.executable,
        '-m',
        'agent_trace_studio.repair_probe',
        str(source_path.resolve()),
        session_id,
    ]
    completed = subprocess.run(
        command,
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or 'parser replay failed')
    parsed = json.loads(completed.stdout)
    if not isinstance(parsed, dict):
        raise RuntimeError('parser replay returned an unexpected result')
    return cast('dict[str, object]', parsed)


def run_deterministic_checks(workspace: Path, source_path: Path, session_id: str) -> list[DeterministicCheck]:
    environment = _subprocess_environment(workspace)
    ruff = shutil.which('ruff')
    commands: list[tuple[str, list[str]]] = [
        ('Unit tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests']),
        (
            'Journal replay',
            [
                sys.executable,
                '-m',
                'agent_trace_studio.repair_probe',
                str(source_path.resolve()),
                session_id,
            ],
        ),
    ]
    if ruff:
        commands[1:1] = [
            ('Ruff lint', [ruff, 'check', '.']),
            ('Ruff format', [ruff, 'format', '--check', '.']),
        ]
    javascript = workspace / 'src/agent_trace_studio/assets/dashboard.js'
    node = shutil.which('node')
    checks = [_run_check(name, command, workspace, environment) for name, command in commands]
    if not ruff:
        checks.extend(
            [
                DeterministicCheck('Ruff lint', 'skipped', 'ruff check .', None, 'Ruff is not installed.'),
                DeterministicCheck('Ruff format', 'skipped', 'ruff format --check .', None, 'Ruff is not installed.'),
            ]
        )
    if javascript.is_file() and node:
        checks.append(_run_check('Dashboard syntax', [node, '--check', str(javascript)], workspace, environment))
    elif javascript.is_file():
        checks.append(
            DeterministicCheck(
                name='Dashboard syntax',
                status='skipped',
                command='node --check src/agent_trace_studio/assets/dashboard.js',
                exit_code=None,
                output='Node.js is not installed.',
            )
        )
    return checks


def _run_check(name: str, command: list[str], workspace: Path, environment: dict[str, str]) -> DeterministicCheck:
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = _completed_output(exc.stdout, exc.stderr)
        return DeterministicCheck(name, 'failed', shlex.join(command), None, output or 'Timed out after 300 seconds.')
    output = _completed_output(completed.stdout, completed.stderr)
    return DeterministicCheck(
        name=name,
        status='passed' if completed.returncode == 0 else 'failed',
        command=shlex.join(command),
        exit_code=completed.returncode,
        output=output,
    )


def _subprocess_environment(workspace: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in _SECRET_ENV_MARKERS)
    }
    existing_pythonpath = environment.get('PYTHONPATH')
    source_path = str(workspace / 'src')
    environment['PYTHONPATH'] = (
        f'{source_path}{os.pathsep}{existing_pythonpath}' if existing_pythonpath else source_path
    )
    return environment


def _validated_selection(result: AnalysisResult, dashboard_state: dict[str, object]) -> dict[str, object]:
    source = dashboard_state.get('source')
    selection = dashboard_state.get('selection')
    if not isinstance(source, dict) or not isinstance(selection, dict):
        raise ValueError('dashboard_state must include source and selection')
    session_id = _optional_string(source.get('session_id'))
    if not session_id:
        raise ValueError('select a loaded trace before starting an agent workflow')
    trace = next((item for item in result.traces if item.session_id == session_id), None)
    if trace is None:
        raise ValueError('the selected trace is no longer loaded')
    turn_id = _optional_string(selection.get('turn_id'))
    event_sequence = _optional_int(selection.get('event_sequence'))
    if turn_id and not any(event.turn_id == turn_id for event in trace.events):
        raise ValueError('the selected turn is no longer available')
    if event_sequence is not None and not any(
        event.turn_id == turn_id and event.sequence == event_sequence for event in trace.events
    ):
        raise ValueError('the selected event is no longer available')
    return {
        'session_id': session_id,
        'turn_id': turn_id,
        'event_sequence': event_sequence,
        'source_path': Path(trace.session_file).resolve(),
        'dashboard_state': dashboard_state,
    }


def _validated_recovery_selection(
    result: AnalysisResult,
    dashboard_state: dict[str, object],
) -> tuple[dict[str, object], str | None]:
    try:
        return _validated_selection(result, dashboard_state), None
    except ValueError as exc:
        reason = str(exc)
        if reason not in {'the selected turn is no longer available', 'the selected event is no longer available'}:
            raise
    normalized = dict(dashboard_state)
    saved_selection = dashboard_state.get('selection')
    selection = dict(saved_selection) if isinstance(saved_selection, dict) else {}
    if reason == 'the selected turn is no longer available':
        selection['turn_id'] = None
        selection['event_sequence'] = None
        message = 'The saved turn is no longer available after the trace refresh; continuing with session context.'
    else:
        selection['event_sequence'] = None
        message = 'The saved event is no longer available after the trace refresh; continuing with turn context.'
    normalized['selection'] = selection
    return _validated_selection(result, normalized), message


def _accepted(
    audit: ParserAudit,
    delta: WorkspaceDelta,
    gates_passed: bool,
    verdict: VerificationVerdict,
    *,
    allow_already_satisfied: bool = False,
) -> bool:
    if not gates_passed or verdict.status != 'pass' or verdict.unresolved_issues or verdict.regressions:
        return False
    return not audit.requires_fix or not delta.empty or (allow_already_satisfied and verdict.already_satisfied)


def _verification_feedback(
    audit: ParserAudit,
    delta: WorkspaceDelta,
    gates_passed: bool,
    checks: list[DeterministicCheck],
    parser_after: dict[str, object],
    verdict: VerificationVerdict,
) -> dict[str, object]:
    return {
        'reason': 'audit_requires_change_but_patch_is_empty'
        if audit.requires_fix and delta.empty
        else 'verification_failed',
        'deterministic_gates_passed': gates_passed,
        'failed_checks': [asdict(check) for check in checks if check.status == 'failed'],
        'parser_probe_error': parser_after.get('error'),
        'verifier': verdict.model_dump(mode='json'),
    }


def _failure_signature(feedback: dict[str, object] | None) -> str:
    if feedback is None:
        return ''
    verifier = feedback.get('verifier')
    verifier_payload = verifier if isinstance(verifier, dict) else {}
    checks = feedback.get('failed_checks') or feedback.get('checks')
    check_items = checks if isinstance(checks, list) else []
    signature = {
        'reason': feedback.get('reason'),
        'failed_checks': [
            {'name': item.get('name'), 'status': item.get('status'), 'exit_code': item.get('exit_code')}
            for item in check_items
            if isinstance(item, dict)
        ],
        'parser_probe_error': feedback.get('parser_probe_error'),
        'verifier_status': verifier_payload.get('status'),
        'unresolved_issues': verifier_payload.get('unresolved_issues'),
        'regressions': verifier_payload.get('regressions'),
        'required_changes': verifier_payload.get('required_changes'),
    }
    return json.dumps(signature, sort_keys=True, ensure_ascii=True)


def _checks_passed(checks: list[DeterministicCheck]) -> bool:
    return bool(checks) and all(check.status in {'passed', 'skipped'} for check in checks)


def _checks_from_records(records: list[object]) -> list[DeterministicCheck] | None:
    checks: list[DeterministicCheck] = []
    for record in records:
        if not isinstance(record, dict):
            return None
        name = record.get('name')
        status = record.get('status')
        command = record.get('command')
        exit_code = record.get('exit_code')
        output = record.get('output')
        if (
            not isinstance(name, str)
            or not name.strip()
            or status not in {'passed', 'failed', 'skipped'}
            or not isinstance(command, str)
            or (exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)))
            or not isinstance(output, str)
        ):
            return None
        checks.append(
            DeterministicCheck(
                name=name,
                status=cast('Literal["passed", "failed", "skipped"]', status),
                command=command,
                exit_code=exit_code,
                output=output,
            )
        )
    return checks


def _classify_check_regressions(
    baseline_checks: list[DeterministicCheck],
    candidate_checks: list[DeterministicCheck],
    *,
    baseline_workspace: Path,
    candidate_workspace: Path,
    changed_files: tuple[str, ...],
) -> tuple[list[DeterministicCheck], list[DeterministicCheck], list[DeterministicCheck]]:
    effective: list[DeterministicCheck] = []
    inherited: list[DeterministicCheck] = []
    new: list[DeterministicCheck] = []
    roots = (baseline_workspace.resolve(), candidate_workspace.resolve())
    baseline_failures = Counter(
        _check_failure_fingerprint(check, roots) for check in baseline_checks if check.status == 'failed'
    )
    baseline_catalog = Counter(_check_catalog_key(check, roots) for check in baseline_checks)
    candidate_catalog = Counter(_check_catalog_key(check, roots) for check in candidate_checks)
    for check in candidate_checks:
        fingerprint = _check_failure_fingerprint(check, roots)
        if (
            check.status == 'failed'
            and check.name in _INHERITABLE_BASELINE_FAILURES
            and baseline_failures[fingerprint] > 0
            and not _check_mentions_changed_file(check, changed_files, roots)
            and not _check_control_surface_changed(check.name, changed_files)
        ):
            baseline_failures[fingerprint] -= 1
            inherited.append(check)
            effective.append(
                DeterministicCheck(
                    name=check.name,
                    status='skipped',
                    command=check.command,
                    exit_code=None,
                    output=(
                        'Unchanged baseline failure; recorded for visibility but not counted as a candidate '
                        f'regression.\n{check.output}'
                    ).rstrip(),
                )
            )
            continue
        effective.append(check)
        if check.status == 'failed':
            new.append(check)
    missing_catalog = baseline_catalog - candidate_catalog
    for (name, command), count in missing_catalog.items():
        for _ in range(count):
            missing = DeterministicCheck(
                name=name,
                status='failed',
                command=command,
                exit_code=None,
                output='Candidate did not return this baseline check; verification cannot prove that the gate ran.',
            )
            effective.append(missing)
            new.append(missing)
    return effective, inherited, new


def _check_failure_fingerprint(check: DeterministicCheck, roots: tuple[Path, ...]) -> tuple[object, ...]:
    return (
        check.name,
        check.exit_code,
        _normalize_check_text(check.command, roots),
        _normalize_check_text(check.output, roots),
    )


def _check_catalog_key(check: DeterministicCheck, roots: tuple[Path, ...]) -> tuple[str, str]:
    return check.name, _normalize_check_text(check.command, roots)


def _check_mentions_changed_file(
    check: DeterministicCheck,
    changed_files: tuple[str, ...],
    roots: tuple[Path, ...],
) -> bool:
    evidence = _normalize_check_text(f'{check.command}\n{check.output}', roots).replace('\\', '/')
    return any(relative.replace('\\', '/') in evidence or Path(relative).name in evidence for relative in changed_files)


def _check_control_surface_changed(check_name: str, changed_files: tuple[str, ...]) -> bool:
    if check_name in {'Ruff lint', 'Ruff format'}:
        return any(Path(relative).name in _RUFF_CONFIGURATION_NAMES for relative in changed_files)
    return False


def _check_environment_fingerprint(
    check_runner: Callable[[Path, Path, str], list[DeterministicCheck]],
    *,
    runtime_nonce: str,
) -> str:
    runner_module = str(getattr(check_runner, '__module__', type(check_runner).__module__))
    runner_name = str(getattr(check_runner, '__qualname__', type(check_runner).__qualname__))
    identity = {
        'runner': f'{runner_module}:{runner_name}',
        'runner_implementation': _callable_implementation_digest(check_runner),
        'runtime_nonce': runtime_nonce,
        'python': {'version': sys.version, **_executable_identity(sys.executable)},
        'ruff': _executable_identity(shutil.which('ruff')),
        'node': _executable_identity(shutil.which('node')),
    }
    serialized = json.dumps(identity, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def _callable_implementation_digest(
    check_runner: Callable[[Path, Path, str], list[DeterministicCheck]],
) -> str:
    target = getattr(check_runner, '__func__', check_runner)
    code = getattr(target, '__code__', None)
    if code is None and callable(target):
        call_target = next(
            (namespace['__call__'] for cls in type(target).__mro__ if '__call__' in (namespace := vars(cls))),
            None,
        )
        code = getattr(call_target, '__code__', None)
        target = call_target or target
    digest = hashlib.sha256()
    if code is not None:
        digest.update(marshal.dumps(code))
        source_file = Path(code.co_filename)
        with suppress(OSError):
            digest.update(hashlib.sha256(source_file.read_bytes()).digest())
    else:
        digest.update(f'{type(target).__module__}:{type(target).__qualname__}'.encode())
    digest.update(repr(getattr(target, '__defaults__', None)).encode())
    digest.update(repr(getattr(target, '__kwdefaults__', None)).encode())
    return digest.hexdigest()


def _executable_identity(path: str | None) -> dict[str, object]:
    if not path:
        return {'path': None}
    executable = Path(path).resolve()
    try:
        metadata = executable.stat()
    except OSError:
        return {'path': str(executable), 'unavailable': True}
    return {
        'path': str(executable),
        'size': metadata.st_size,
        'mtime_ns': metadata.st_mtime_ns,
    }


def _normalize_check_text(value: str, roots: tuple[Path, ...]) -> str:
    normalized = value.replace('\r\n', '\n').replace('\r', '\n')
    for root in sorted(roots, key=lambda item: len(str(item)), reverse=True):
        normalized = normalized.replace(str(root), '<WORKSPACE>')
    normalized = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', normalized)
    normalized = re.sub(r'0x[0-9a-fA-F]+', '0xADDRESS', normalized)
    normalized = re.sub(r'(?m)^(Ran \d+ tests? in )\d+(?:\.\d+)?s$', r'\1<TIME>s', normalized)
    return normalized.strip()


def _probe_or_error(
    runner: Callable[[Path, Path, str], dict[str, object]],
    workspace: Path,
    source_path: Path,
    session_id: str,
) -> dict[str, object]:
    try:
        return runner(workspace, source_path, session_id)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return {'error': str(exc)}


def _cancelled(
    payload: dict[str, object],
    cancel_event: threading.Event,
    update: Callable[[dict[str, object]], None],
) -> bool:
    if not cancel_event.is_set():
        return False
    can_continue = payload.get('kind') in _SOURCE_CHANGE_KINDS
    payload['recovery'] = _recovery_options(
        reason='Agent workflow cancelled; local source was not changed.',
        can_continue=can_continue,
        workspace_preserved=can_continue,
        category='user_cancelled',
    )
    _set_run(
        payload,
        update,
        status='cancelled',
        message='Agent workflow cancelled; local source was not changed.',
        completed_at=_now(),
    )
    return True


def _set_run(
    payload: dict[str, object],
    update: Callable[[dict[str, object]], None],
    *,
    status: str,
    message: str,
    completed_at: str | None = None,
    result: dict[str, object] | None = None,
) -> None:
    payload['status'] = status
    payload['message'] = message
    payload['updated_at'] = _now()
    if status in _ACTIVE_STATUSES or status in {
        'investigated',
        'extracted',
        'summarized',
        'audited',
        'passed',
        'discarded',
    }:
        payload['recovery'] = None
    if completed_at is not None:
        payload['completed_at'] = completed_at
    if result is not None:
        payload['result'] = result
    _append_activity(
        payload,
        phase=_activity_phase(status),
        message=message,
        timestamp=str(payload['updated_at']),
    )
    update(payload)


def _record_activity(
    payload: dict[str, object],
    update: Callable[[dict[str, object]], None],
    *,
    phase: str,
    message: str,
) -> None:
    timestamp = _now()
    payload['updated_at'] = timestamp
    _append_activity(payload, phase=phase, message=message, timestamp=timestamp)
    update(payload)


def _append_activity(
    payload: dict[str, object],
    *,
    phase: str,
    message: str,
    timestamp: str,
) -> None:
    clean_phase = ' '.join(phase.split()).strip()[:40] or 'Agent'
    clean_message = ' '.join(message.split()).strip()[:_MAX_ACTIVITY_MESSAGE_CHARS]
    if not clean_message:
        return
    raw_activity = payload.get('activity')
    activity = raw_activity if isinstance(raw_activity, list) else []
    previous = activity[-1] if activity and isinstance(activity[-1], dict) else None
    if previous and previous.get('phase') == clean_phase and previous.get('message') == clean_message:
        previous['at'] = timestamp
        previous['repeat_count'] = int(previous.get('repeat_count') or 1) + 1
        payload['activity'] = activity[-_MAX_ACTIVITY_ENTRIES:]
        return
    previous_sequence = int(previous.get('sequence') or 0) if previous else 0
    activity.append(
        {
            'sequence': previous_sequence + 1,
            'at': timestamp,
            'phase': clean_phase,
            'message': clean_message,
        }
    )
    payload['activity'] = activity[-_MAX_ACTIVITY_ENTRIES:]


def _activity_phase(status: str) -> str:
    return {
        'queued': 'Queue',
        'investigating': 'Investigate',
        'extracting': 'Memories',
        'summarizing': 'Checkpoints',
        'auditing': 'Audit',
        'repairing': 'Repair',
        'retrying': 'Retry',
        'checking': 'Checks',
        'verifying': 'Verify',
        'applying': 'Apply',
        'investigated': 'Complete',
        'extracted': 'Complete',
        'summarized': 'Complete',
        'audited': 'Complete',
        'passed': 'Complete',
        'paused': 'Action needed',
        'blocked': 'Stopped',
        'failed': 'Failed',
        'cancelled': 'Stopped',
        'interrupted': 'Stopped',
        'discarded': 'Stopped',
    }.get(status, 'Agent')


def _audit_message(audit: ParserAudit) -> str:
    count = len(audit.issues)
    if not audit.requires_fix:
        return 'Audit completed without a supported parser defect.'
    return f'Audit found {count} supported issue{"s" if count != 1 else ""}.'


def _run_summary(payload: dict[str, object] | None) -> dict[str, object] | None:
    if payload is None:
        return None
    summary = {
        key: payload.get(key)
        for key in (
            'run_id',
            'kind',
            'status',
            'message',
            'updated_at',
            'session_id',
            'turn_id',
            'event_sequence',
            'attempt',
            'max_attempts',
            'recovery',
            'harness',
            'harness_label',
        )
    }
    audit = payload.get('audit')
    summary['audit_requires_fix'] = bool(isinstance(audit, dict) and audit.get('requires_fix'))
    return summary


def _record_signature(row: dict[object, object]) -> str:
    top_type = str(row.get('type') or 'unknown')
    payload = row.get('payload')
    if not isinstance(payload, dict):
        return f'{top_type}|payload:{type(payload).__name__}'
    payload_type = str(payload.get('type') or 'none')
    keys = ','.join(sorted(str(key) for key in payload)[:40])
    return f'{top_type}|{payload_type}|{keys}'


def _shape(value: object, *, key: str = '', depth: int = 0) -> object:
    if depth >= 5:
        return '<nested>'
    if isinstance(value, dict):
        return {
            str(item_key): _shape(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in list(value.items())[:40]
            if str(item_key) not in {'encrypted_content', 'encrypted_reasoning'}
        }
    if isinstance(value, list):
        return [_shape(item, key=key, depth=depth + 1) for item in value[:3]]
    if isinstance(value, str):
        if key in _PRESERVED_SHAPE_KEYS and len(value) <= 120:
            return value
        return f'<text:{len(value)} chars>'
    if value is None or isinstance(value, bool | int | float):
        return value
    return f'<{type(value).__name__}>'


def _selected_event(trace: SessionTrace, turn_id: str | None, event_sequence: int | None) -> TraceEvent | None:
    if turn_id is None or event_sequence is None:
        return None
    return next(
        (event for event in trace.events if event.turn_id == turn_id and event.sequence == event_sequence),
        None,
    )


def _event_metadata(event: TraceEvent | None) -> dict[str, object] | None:
    if event is None:
        return None
    return {
        'turn_id': event.turn_id,
        'sequence': event.sequence,
        'line_number': event.line_number,
        'output_line_number': event.output_line_number,
        'category': event.category,
        'kind': event.kind,
        'role': event.role,
        'phase': event.phase,
        'tool_name': event.tool_name,
        'status': event.status,
        'text_chars': len(event.text),
        'input_chars': len(event.input_text),
        'output_chars': len(event.output_text),
        'truncated_fields': list(event.truncated_fields),
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + '\n', encoding='utf-8')


def _completed_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    parts: list[str] = []
    for value in (stdout, stderr):
        text = value.decode('utf-8', errors='replace').strip() if isinstance(value, bytes) else (value or '').strip()
        if text:
            parts.append(text)
    return '\n'.join(parts)


def _safe_error(exc: BaseException, settings: QASettings) -> str:
    message = str(exc) or type(exc).__name__
    if settings.api_key:
        message = message.replace(settings.api_key, '[redacted]')
    return message


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _trace_coverage_digest(trace: SessionTrace, *, through_sequence: int) -> str:
    """Hash normalized event content through one session cursor."""

    digest = hashlib.sha256()
    for event in sorted(trace.events, key=lambda item: (item.sequence, item.line_number)):
        if event.sequence > through_sequence:
            continue
        record = (
            event.sequence,
            event.line_number,
            event.turn_id,
            event.timestamp,
            event.category,
            event.kind,
            event.role,
            event.phase,
            event.title,
            event.text,
            event.tool_name,
            event.status,
            event.input_text,
            event.output_text,
        )
        digest.update(json.dumps(record, ensure_ascii=True, separators=(',', ':')).encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()


def _merge_session_checkpoints(
    previous: dict[str, object],
    current: dict[str, object],
) -> list[dict[str, object]]:
    """Preserve sealed historical checkpoints while accepting updated/new checkpoints."""

    old_values = previous.get('checkpoints')
    new_values = current.get('checkpoints')
    old = [dict(item) for item in old_values if isinstance(item, dict)] if isinstance(old_values, list) else []
    new = [dict(item) for item in new_values if isinstance(item, dict)] if isinstance(new_values, list) else []
    new_titles = {' '.join(str(item.get('title') or '').lower().split()) for item in new}
    new_ranges = {
        (_optional_int(item.get('start_event_sequence')), _optional_int(item.get('end_event_sequence'))) for item in new
    }
    preserved: list[dict[str, object]] = []
    for item in old:
        if item.get('status') == 'in_progress':
            continue
        title = ' '.join(str(item.get('title') or '').lower().split())
        event_range = (
            _optional_int(item.get('start_event_sequence')),
            _optional_int(item.get('end_event_sequence')),
        )
        if (title and title in new_titles) or (event_range != (None, None) and event_range in new_ranges):
            continue
        preserved.append(item)
    combined = [*preserved, *new]
    indexed = list(enumerate(combined))
    indexed.sort(
        key=lambda pair: (
            _optional_int(pair[1].get('start_event_sequence')) or 2**31,
            _optional_int(pair[1].get('end_event_sequence')) or 2**31,
            pair[0],
        )
    )
    ordered = [item for _, item in indexed]
    if len(ordered) <= 32:
        return ordered
    return [*ordered[:8], *ordered[-24:]]


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()
