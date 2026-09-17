"""Loopback-only server for trace loading and Q&A."""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from uuid import UUID

from agent_trace_studio.activity import normalize_activity_details
from agent_trace_studio.agent_backend import AgentProgress, AgentProgressCallback
from agent_trace_studio.agent_control import (
    VERIFIED_RUNTIME_ACTIVATION_MODE,
    SourceMutationAction,
    action_acknowledgement,
    classify_source_action_request,
)
from agent_trace_studio.analytics import build_dashboard_payload
from agent_trace_studio.audit_rules import AuditRule, AuditRuleDraft, AuditRuleStore, evaluate_audit_rules
from agent_trace_studio.credentials import CredentialPersistenceError
from agent_trace_studio.live import (
    CanonicalJournalStore,
    FileFingerprint,
    default_live_server_path,
    fingerprint,
    remove_server_descriptor,
    write_server_descriptor,
)
from agent_trace_studio.live_actions import AuditActionDispatcher
from agent_trace_studio.models import AnalysisResult
from agent_trace_studio.parser import SessionResolutionError, analyze_journals, resolve_session_ids
from agent_trace_studio.qa import (
    ControllerAction,
    DashboardResourceAccess,
    JournalQA,
    QAUnavailableError,
    StudioAgentCancelled,
)
from agent_trace_studio.repair import RepairCoordinator, RepairKind
from agent_trace_studio.report import write_report
from agent_trace_studio.supervisor import SupervisorClient, child_deployment_context

_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_UPLOAD_BYTES = 256 * 1024 * 1024
_MAX_LOADED_FILES = 64
_MAX_HIGHLIGHT_CHARS = 2_000
_MAX_CHECKPOINT_SUMMARY_CHARS = 2_000
_MAX_STUDIO_REQUEST_RECORDS = 64
_MAX_STUDIO_ACTIVITY_MESSAGE_CHARS = 400
_JOURNAL_SUFFIXES = frozenset({'.json', '.jsonl'})
_DASHBOARD_ASSET_PREFIX = 'src/agent_trace_studio/assets/'
_AGENT_CONVERSATION_TERMINAL_STATUSES = frozenset(
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


class DashboardState:
    """Thread-safe current dashboard payload and uploaded-file lifetime."""

    def __init__(
        self,
        result: AnalysisResult,
        *,
        title: str,
        qa: JournalQA,
        repair: RepairCoordinator | None = None,
        live: bool = False,
        live_poll_interval: float = 0.75,
        live_state_dir: Path | None = None,
        live_server_path: Path | None = None,
        codex_home: Path | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        trace_char_limit: int = 20_000,
        assurance: dict[str, object] | None = None,
        output_dir: Path | None = None,
        audit_rules: AuditRuleStore | None = None,
        audit_actions: AuditActionDispatcher | None = None,
    ) -> None:
        self._result = result
        self._title = title
        self._qa = qa
        self._repair = repair
        self._lock = threading.RLock()
        self._live_condition = threading.Condition(self._lock)
        self._uploads = tempfile.TemporaryDirectory(prefix='agent-trace-studio-')
        self._source_paths = tuple(Path(path).resolve() for path in result.source_paths)
        self._upload_sequence = 0
        self._codex_home = codex_home.expanduser().resolve() if codex_home is not None else None
        self._from_time = from_time
        self._to_time = to_time
        self._trace_char_limit = max(trace_char_limit, 1)
        self._assurance = assurance
        self._output_dir = output_dir.expanduser().resolve() if output_dir is not None else None
        self._audit_rules = audit_rules
        self._audit_actions = audit_actions or (
            AuditActionDispatcher(audit_rules.path.parent / 'audit-actions.jsonl') if audit_rules is not None else None
        )
        self._dashboard_refreshes: dict[str, dict[str, object]] = {}
        self._runtime_deployments: dict[str, dict[str, object]] = {}
        self._studio_requests: dict[str, threading.Event] = {}
        self._studio_request_records: dict[str, dict[str, object]] = {}
        self._supervisor = SupervisorClient.from_environment()
        self._deployment_lock = threading.Lock()
        self._deployment_stop = threading.Event()
        self._deployment_thread: threading.Thread | None = None
        self._live_enabled = live
        self._live_poll_interval = max(live_poll_interval, 0.1)
        self._live_revision = 0
        self._live_changed_at = ''
        self._live_error = ''
        self._live_connected = False
        self._closed = False
        self._live_token = secrets.token_urlsafe(32) if live else ''
        self._live_server_path = (live_server_path or default_live_server_path()).resolve()
        self._live_server_url = ''
        self._live_state_root = (live_state_dir or self._live_server_path.parent / 'streams').resolve()
        self._live_store = CanonicalJournalStore(self._live_state_root) if live else None
        self._live_sources: dict[str, dict[str, object]] = {}
        self._automatic_action_seen: set[str] = set()
        self._automatic_action_pending_baseline: set[str] = set()
        self._fingerprints: dict[Path, FileFingerprint] = {path: fingerprint(path) for path in self._source_paths}
        self._live_stop = threading.Event()
        self._live_wake = threading.Event()
        self._live_thread: threading.Thread | None = None
        if self._audit_actions is not None:
            self._audit_actions.set_changed_callback(self._publish_audit_action_update)
        if live:
            for path in self._source_paths:
                self._live_sources[str(path)] = {
                    'adapter': 'file',
                    'session_id': self._session_id_for_path(path),
                    'path': str(path),
                    'status': 'watching',
                }
            self._baseline_automatic_audit_actions_locked()
            self._live_thread = threading.Thread(target=self._monitor_live_sources, daemon=True)
            self._live_thread.start()
        if self._supervisor is not None and self._repair is not None:
            self._deployment_thread = threading.Thread(
                target=self._monitor_verified_deployments,
                name='agent-trace-deployment-monitor',
                daemon=True,
            )
            self._deployment_thread.start()

    def close(self) -> None:
        with self._lock:
            studio_requests = tuple(self._studio_requests.values())
            self._studio_requests.clear()
        for cancel_event in studio_requests:
            cancel_event.set()
        self._live_stop.set()
        self._live_wake.set()
        self._deployment_stop.set()
        with self._live_condition:
            self._closed = True
            self._live_condition.notify_all()
        if self._live_thread is not None:
            self._live_thread.join(timeout=3)
        if self._deployment_thread is not None:
            self._deployment_thread.join(timeout=3)
        if self._live_enabled:
            remove_server_descriptor(self._live_server_path, token=self._live_token)
        if self._audit_actions is not None:
            self._audit_actions.set_changed_callback(None)
        if self._repair is not None:
            self._repair.close()
        self._qa.close()
        self._uploads.cleanup()

    def status(self) -> dict[str, object]:
        qa_status = self._qa.public_status()
        agent_status = self._agent_status()
        deployment = self._deployment_status()
        with self._lock:
            return {
                'interactive': True,
                'qa': qa_status,
                'agent': agent_status,
                'live': self._live_status_locked(),
                'deployment': deployment,
                'audit_rules': self._audit_rule_status(),
                'source_paths': [str(path) for path in self._source_paths],
                'sessions': len(self._result.sessions),
                'turns': len(self._result.turns),
            }

    def payload(self) -> dict[str, object]:
        qa_status = self._qa.public_status()
        agent_status = self._agent_status()
        with self._lock:
            return self._payload_locked(qa_status=qa_status, agent_status=agent_status)

    def begin_studio_request(self, request_id: str) -> threading.Event:
        """Register a cancellable Studio request without exposing provider internals."""

        try:
            normalized = str(UUID(request_id))
        except ValueError as exc:
            raise ValueError('request_id must be a UUID') from exc
        if normalized != request_id.lower():
            raise ValueError('request_id must be a canonical UUID')
        with self._lock:
            if normalized in self._studio_requests:
                raise ValueError('request_id is already active')
            cancel_event = threading.Event()
            self._studio_requests[normalized] = cancel_event
            timestamp = _studio_activity_timestamp()
            self._studio_request_records[normalized] = {
                'request_id': normalized,
                'status': 'running',
                'started_at': timestamp,
                'updated_at': timestamp,
                'completed_at': '',
                'activity': [],
                'activity_revision': 0,
            }
            while len(self._studio_request_records) > _MAX_STUDIO_REQUEST_RECORDS:
                oldest = next(iter(self._studio_request_records))
                if oldest in self._studio_requests:
                    break
                self._studio_request_records.pop(oldest, None)
            return cancel_event

    def finish_studio_request(
        self,
        request_id: str,
        cancel_event: threading.Event,
        *,
        status: str = 'completed',
        message: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            if self._studio_requests.get(request_id) is cancel_event:
                self._studio_requests.pop(request_id, None)
            timestamp = _studio_activity_timestamp()
            record = self._studio_request_records.get(request_id)
            if record is not None:
                record['status'] = status
                record['updated_at'] = timestamp
                record['completed_at'] = timestamp
                if status in {'failed', 'cancelled'}:
                    for item in record.get('activity', []):
                        details = item.get('details')
                        if isinstance(details, dict) and details.get('status') == 'running':
                            details['status'] = status
                            item['at'] = timestamp
                if status != 'completed':
                    terminal_message = message or {
                        'cancelled': 'Studio request stopped.',
                        'failed': 'Studio request failed.',
                    }.get(status, 'Studio request finished.')
                    self._append_studio_activity_locked(
                        request_id,
                        phase='Stopped' if status == 'cancelled' else 'Error',
                        message=terminal_message,
                        timestamp=timestamp,
                    )
            return self._studio_request_record_locked(request_id)

    def record_studio_activity(self, request_id: str, progress: AgentProgress) -> None:
        """Record one privacy-safe native or host-observed Studio activity event."""

        with self._lock:
            self._append_studio_activity_locked(
                request_id,
                phase=progress.phase,
                message=progress.message,
                timestamp=_studio_activity_timestamp(),
                details=progress.details,
            )

    def studio_request(self, request_id: str) -> dict[str, object]:
        try:
            normalized = str(UUID(request_id))
        except ValueError as exc:
            raise ValueError('request_id must be a UUID') from exc
        with self._lock:
            if normalized not in self._studio_request_records:
                raise ValueError('Studio request not found')
            return self._studio_request_record_locked(normalized)

    def cancel_studio_request(self, request_id: str) -> dict[str, object]:
        try:
            normalized = str(UUID(request_id))
        except ValueError as exc:
            raise ValueError('request_id must be a UUID') from exc
        with self._lock:
            cancel_event = self._studio_requests.get(normalized)
            if cancel_event is not None:
                cancel_event.set()
                record = self._studio_request_records.get(normalized)
                if record is not None:
                    record['status'] = 'cancelling'
                self._append_studio_activity_locked(
                    normalized,
                    phase='Stop',
                    message='Stop requested; waiting for the active harness turn to exit.',
                    timestamp=_studio_activity_timestamp(),
                )
        if cancel_event is None:
            return {'cancelled': False, 'request_id': normalized}
        return {'cancelled': True, 'request_id': normalized}

    def _studio_request_record_locked(self, request_id: str) -> dict[str, object]:
        record = self._studio_request_records.get(request_id)
        return deepcopy(record) if record is not None else {}

    def _append_studio_activity_locked(
        self,
        request_id: str,
        *,
        phase: str,
        message: str,
        timestamp: str,
        details: dict[str, object] | None = None,
    ) -> None:
        record = self._studio_request_records.get(request_id)
        if record is None:
            return
        clean_phase = ' '.join(str(phase).split()).strip()[:40] or 'Agent'
        clean_message = ' '.join(str(message).split()).strip()[:_MAX_STUDIO_ACTIVITY_MESSAGE_CHARS]
        if not clean_message:
            return
        raw_activity = record.get('activity')
        activity = raw_activity if isinstance(raw_activity, list) else []
        public_details = normalize_activity_details(details)
        if public_details is not None:
            # One row per call, including when it finishes between browser polls.
            existing = next(
                (
                    item
                    for item in activity
                    if isinstance(item, dict)
                    and isinstance(item.get('details'), dict)
                    and item['details'].get('id') == public_details['id']
                ),
                None,
            )
            if existing is not None:
                existing.update(phase=clean_phase, message=clean_message, at=timestamp, details=public_details)
                record['updated_at'] = timestamp
                record['activity_revision'] = int(record.get('activity_revision') or 0) + 1
                return
        previous = activity[-1] if activity and isinstance(activity[-1], dict) else None
        if (
            public_details is None
            and previous
            and not previous.get('details')
            and previous.get('phase') == clean_phase
            and previous.get('message') == clean_message
        ):
            previous['at'] = timestamp
            previous['repeat_count'] = int(previous.get('repeat_count') or 1) + 1
        else:
            previous_sequence = int(previous.get('sequence') or 0) if previous else 0
            activity.append(
                {
                    'sequence': previous_sequence + 1,
                    'at': timestamp,
                    'phase': clean_phase,
                    'message': clean_message,
                    **({'details': public_details} if public_details is not None else {}),
                }
            )
        record['activity'] = activity
        record['updated_at'] = timestamp
        record['activity_revision'] = int(record.get('activity_revision') or 0) + 1

    def live_status(self) -> dict[str, object]:
        with self._lock:
            return self._live_status_locked()

    def attach_live_server(self, url: str) -> None:
        self._live_server_url = url
        if not self._live_enabled:
            return
        write_server_descriptor(self._live_server_path, url=url, token=self._live_token)
        with self._live_condition:
            self._live_connected = True
            self._publish_live_update_locked()

    def start_live_monitoring(self) -> dict[str, object]:
        """Enable file polling and live audit after an explicit dashboard action."""

        with self._live_condition:
            if self._closed:
                raise ValueError('dashboard server is closing')
            if self._live_enabled:
                return self._live_status_locked()
            token = secrets.token_urlsafe(32)
            if self._live_server_url:
                write_server_descriptor(self._live_server_path, url=self._live_server_url, token=token)
            self._live_token = token
            self._live_store = CanonicalJournalStore(self._live_state_root)
            self._live_sources = {
                str(path): {
                    'adapter': self._infer_adapter(path),
                    'session_id': self._session_id_for_path(path),
                    'path': str(path),
                    'status': 'watching',
                }
                for path in self._source_paths
            }
            self._fingerprints = {path: fingerprint(path) for path in self._source_paths}
            self._live_stop = threading.Event()
            self._live_wake = threading.Event()
            self._live_enabled = True
            self._live_connected = bool(self._live_server_url)
            self._live_error = ''
            self._baseline_automatic_audit_actions_locked()
            self._live_thread = threading.Thread(
                target=self._monitor_live_sources,
                name='agent-trace-live-monitor',
                daemon=True,
            )
            self._live_thread.start()
            self._publish_live_update_locked()
            return self._live_status_locked()

    def authorize_live_ingest(self, authorization: str | None) -> bool:
        if not self._live_enabled or not authorization:
            return False
        scheme, _, supplied = authorization.partition(' ')
        return scheme.lower() == 'bearer' and hmac.compare_digest(supplied, self._live_token)

    def register_live_source(self, payload: dict[str, object]) -> dict[str, object]:
        if not self._live_enabled:
            raise ValueError('live monitoring is not enabled')
        raw_path = _optional_text(payload.get('transcript_path'))
        if raw_path is None:
            return {'registered': False, 'live': self.status()['live']}
        path = Path(raw_path).expanduser().resolve()
        self._validate_live_path(path)
        adapter = _optional_text(payload.get('adapter')) or self._infer_adapter(path)
        session_id = _optional_text(payload.get('session_id')) or path.stem
        with self._live_condition:
            combined = tuple(dict.fromkeys((*self._source_paths, path)))
            if len(combined) > _MAX_LOADED_FILES:
                raise ValueError(f'no more than {_MAX_LOADED_FILES} session files may be loaded')
            self._source_paths = combined
            self._fingerprints.pop(path, None)
            self._live_sources[str(path)] = {
                'adapter': adapter,
                'session_id': session_id,
                'path': str(path),
                'cwd': _optional_text(payload.get('cwd')) or '',
                'status': 'watching',
            }
            if path.is_file() and path.stat().st_size:
                self._automatic_action_pending_baseline.add(session_id)
        self._live_wake.set()
        return {'registered': True, 'path': str(path), 'live': self.status()['live']}

    def ingest_live_event(self, payload: dict[str, object]) -> dict[str, object]:
        if not self._live_enabled or self._live_store is None:
            raise ValueError('live monitoring is not enabled')
        adapter = _required_text(payload, 'adapter')
        session_id = _required_text(payload, 'session_id')
        event = _required_object(payload, 'event')
        path = self._live_store.append_event(
            adapter=adapter,
            session_id=session_id,
            event=event,
            cwd=_optional_text(payload.get('cwd')) or '',
            model=_optional_text(payload.get('model')) or '',
            turn_id=_optional_text(payload.get('turn_id')) or '',
        )
        with self._live_condition:
            combined = tuple(dict.fromkeys((*self._source_paths, path)))
            if len(combined) > _MAX_LOADED_FILES:
                raise ValueError(f'no more than {_MAX_LOADED_FILES} session files may be loaded')
            self._source_paths = combined
            self._fingerprints.pop(path, None)
            self._live_sources[str(path)] = {
                'adapter': adapter,
                'session_id': session_id,
                'path': str(path),
                'cwd': _optional_text(payload.get('cwd')) or '',
                'status': 'watching',
            }
        self._live_wake.set()
        return {'accepted': True, 'session_id': session_id}

    def finish_live_source(self, payload: dict[str, object]) -> dict[str, object]:
        if not self._live_enabled:
            raise ValueError('live monitoring is not enabled')
        raw_path = _optional_text(payload.get('transcript_path'))
        adapter = _optional_text(payload.get('adapter'))
        session_id = _optional_text(payload.get('session_id'))
        path: Path | None = Path(raw_path).expanduser().resolve() if raw_path else None
        if path is None and adapter and session_id and self._live_store is not None:
            path = self._live_store.finish(
                adapter=adapter,
                session_id=session_id,
                turn_id=_optional_text(payload.get('turn_id')) or '',
                failed=payload.get('failed') is True,
                reason=_optional_text(payload.get('reason')) or '',
            )
        with self._live_condition:
            if path is not None:
                source = self._live_sources.get(str(path))
                if source is not None:
                    source['status'] = 'completed'
                self._fingerprints.pop(path, None)
            elif session_id:
                for source in self._live_sources.values():
                    if source.get('session_id') == session_id:
                        source['status'] = 'completed'
            self._run_automatic_audit_actions_locked()
            self._publish_live_update_locked()
        self._live_wake.set()
        return {'completed': True, 'session_id': session_id or ''}

    def wait_for_live_update(self, after_revision: int, timeout: float = 15.0) -> dict[str, object] | None:
        with self._live_condition:
            self._live_condition.wait_for(
                lambda: self._live_revision > after_revision or self._closed,
                timeout=timeout,
            )
            if self._closed:
                return {'closed': True, 'revision': self._live_revision}
            if self._live_revision <= after_revision:
                return None
            return self._live_status_locked()

    def load_path(self, raw_path: str) -> dict[str, object]:
        path = Path(raw_path).expanduser().resolve()
        self._validate_journal(path)
        return self._add_paths((path,))

    def load_session_id(self, session_id: str) -> dict[str, object]:
        try:
            paths = resolve_session_ids((session_id,), codex_home=self._codex_home)
        except SessionResolutionError as exc:
            raise ValueError(str(exc)) from exc
        return self._add_paths(paths)

    def load_upload(self, filename: str, content: bytes) -> dict[str, object]:
        safe_name = Path(filename).name or 'uploaded-session.jsonl'
        suffix = Path(safe_name).suffix.lower()
        if suffix not in _JOURNAL_SUFFIXES:
            raise ValueError('session file must use .json or .jsonl')
        with self._lock:
            self._upload_sequence += 1
            path = Path(self._uploads.name) / f'{self._upload_sequence:04d}-{safe_name}'
        path.write_bytes(content)
        return self._add_paths((path,))

    def ask(
        self,
        *,
        question: str,
        scope: str,
        session_id: str,
        turn_id: str | None,
        history: list[dict[str, object]],
        event_sequence: int | None = None,
        view_state: dict[str, str] | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            result = self._result
        active_view_state = self._view_state_with_session_brief(result, session_id, view_state)
        dashboard_resources = self._dashboard_resource_access(
            result,
            session_id=session_id,
            turn_id=turn_id,
            event_sequence=event_sequence,
            view_state=active_view_state,
        )
        return self._qa.answer(
            result,
            question=question,
            scope=scope,
            session_id=session_id,
            turn_id=turn_id,
            event_sequence=event_sequence,
            view_state=active_view_state,
            history=history,
            conversation_id=conversation_id,
            dashboard_resources=dashboard_resources,
        )

    def handle_agent_message(
        self,
        *,
        message: str,
        scope: str,
        session_id: str,
        turn_id: str | None,
        dashboard_state: dict[str, object],
        history: list[dict[str, object]],
        client_session_nonce: str | None = None,
        event_sequence: int | None = None,
        view_state: dict[str, str] | None = None,
        cancellation_event: threading.Event | None = None,
        conversation_id: str | None = None,
        progress: AgentProgressCallback | None = None,
    ) -> dict[str, object]:
        with self._lock:
            result = self._result
        active_view_state = self._view_state_with_session_brief(result, session_id, view_state)
        workflow_state = self._controller_workflow_state()
        dashboard_resources = self._dashboard_resource_access(
            result,
            session_id=session_id,
            turn_id=turn_id,
            event_sequence=event_sequence,
            view_state=active_view_state,
        )
        routed = self._qa.route_message(
            result,
            message=message,
            scope=scope,
            session_id=session_id,
            turn_id=turn_id,
            event_sequence=event_sequence,
            view_state=active_view_state,
            history=history,
            workflow_state=workflow_state,
            cancellation_event=cancellation_event,
            conversation_id=conversation_id,
            progress=progress,
            dashboard_resources=dashboard_resources,
        )
        if cancellation_event is not None and cancellation_event.is_set():
            raise StudioAgentCancelled('Dashboard agent stopped.')
        action = routed.decision.action
        if progress is not None and action != 'answer':
            progress(AgentProgress('Action', f'Studio selected the {action.replace("_", " ")} workflow.'))
        response: dict[str, object] = {
            'kind': 'answer' if action == 'answer' else 'workflow',
            'action': action,
            'answer': routed.decision.answer.strip() if action == 'answer' else action_acknowledgement(action),
            'model': routed.model,
            'provider': routed.provider,
            'harness': routed.harness,
            'usage': routed.usage,
            'tools': routed.tools,
        }
        if action == 'answer':
            return response
        if action == 'manage_audit_rules':
            if client_session_nonce is None:
                raise ValueError('client session nonce is required for an agent audit rule proposal')
            response['kind'] = 'audit_rule_proposal'
            response['proposal'] = self.propose_audit_rule(
                instruction=message,
                session_id=session_id,
                turn_id=turn_id,
                event_sequence=event_sequence,
                view_state=active_view_state,
                client_session_nonce=client_session_nonce,
                cancellation_event=cancellation_event,
                conversation_id=conversation_id,
                progress=progress,
            )
            return response
        if self._repair is None:
            raise ValueError('agent workflows are unavailable in this server')
        latest = workflow_state.get('latest_run')
        latest_run = latest if isinstance(latest, dict) else {}
        if action in {'repair_parser', 'customize_dashboard'}:
            audit_run_id: str | None = None
            if action == 'repair_parser':
                audit_run_id = self._repair.reusable_audit_run_id(
                    result=result,
                    dashboard_state=dashboard_state,
                )
            eligibility = classify_source_action_request(
                message,
                action,
                actionable_audit=audit_run_id is not None,
            )
            if not eligibility.eligible:
                raise ValueError(f'Source change approval suppressed: {eligibility.reason}')
            if client_session_nonce is None:
                raise ValueError('client session nonce is required for source-action approval')
            response['kind'] = 'authorization'
            response['authorization'] = self.prepare_source_action(
                action=action,
                dashboard_state=dashboard_state,
                instruction=message,
                client_session_nonce=client_session_nonce,
                audit_run_id=audit_run_id,
            )
            return response
        if action == 'cancel_run':
            run_id = _optional_text(workflow_state.get('active_run_id'))
            if run_id is None:
                raise ValueError('there is no active agent workflow to stop')
            response['run'] = self.cancel_agent_run(run_id)
            return response
        if action in {'continue_run', 'restart_run', 'discard_run'}:
            run_id = _optional_text(latest_run.get('run_id'))
            if run_id is None:
                raise ValueError('there is no agent workflow to control')
            run_action = {
                'continue_run': 'continue',
                'restart_run': 'restart',
                'discard_run': 'discard',
            }[action]
            controlled = self.act_on_agent_run(
                run_id,
                action=run_action,
                instruction=message if action != 'discard_run' else None,
                client_session_nonce=client_session_nonce,
            )
            if controlled.get('kind') == 'authorization':
                response['kind'] = 'authorization'
                response['authorization'] = controlled['authorization']
                response['answer'] = controlled['answer']
            else:
                response['run'] = controlled
            return response
        workflow_kind: dict[str, RepairKind] = {
            'investigate': 'investigate',
            'extract_memories': 'memories',
            'summarize_checkpoints': 'checkpoints',
            'audit_parser': 'audit',
        }
        response['run'] = self.start_agent(
            workflow_kind[action],
            dashboard_state,
        )
        return response

    def session_brief(self, session_id: str) -> dict[str, object]:
        """Return the persisted session-wide checkpoint brief and live coverage."""

        if self._repair is None:
            return {
                'available': False,
                'session_id': session_id,
                'status': 'unavailable',
                'result': None,
            }
        with self._lock:
            result = self._result
        return self._repair.session_brief(result, session_id)

    def audit_rule_catalog(self, session_id: str) -> dict[str, object]:
        """Return the current rule set and its deterministic result for one session."""

        if self._audit_rules is None:
            raise ValueError('audit rule management is unavailable in this server')
        with self._lock:
            result = self._result
            trace = next((candidate for candidate in result.traces if candidate.session_id == session_id), None)
            if trace is None:
                raise ValueError('the selected trace is no longer loaded')
            session_open = self._session_open_for_audit_locked(trace.session_file)
        rule_set = self._audit_rules.snapshot()
        serialized_rules = rule_set.get('rules')
        if not isinstance(serialized_rules, list):
            raise ValueError('audit rule store returned an invalid rule list')
        rules = tuple(AuditRule.model_validate(rule) for rule in serialized_rules)
        assurance = evaluate_audit_rules(
            result,
            rules,
            session_id=session_id,
            session_open=session_open,
        )
        assurance['rules_revision'] = rule_set['revision']
        self._decorate_audit_actions(assurance, rules=rules, session_id=session_id, result=result)
        return {'rule_set': rule_set, 'assurance': assurance}

    def send_audit_message(
        self,
        *,
        session_id: str,
        rule_id: str,
        rule_version: int,
    ) -> dict[str, object]:
        """Send one user-triggered, host-authored notification for a current violation."""

        if not self._live_enabled:
            raise ValueError('start live monitor and audit before sending a session-agent message')
        if self._audit_rules is None or self._audit_actions is None:
            raise ValueError('live audit actions are unavailable in this server')
        with self._lock:
            result = self._result
            trace = next((candidate for candidate in result.traces if candidate.session_id == session_id), None)
            if trace is None:
                raise ValueError('the selected trace is no longer loaded')
            if not self._session_supports_agent_message(result, session_id):
                raise ValueError('session-agent messaging is available only for Codex sessions with an exact UUID')
            session_open = self._session_open_for_audit_locked(trace.session_file)
        rule = next((candidate for candidate in self._audit_rules.rules() if candidate.id == rule_id), None)
        if rule is None or not rule.enabled:
            raise ValueError('the audit rule is no longer active')
        if rule.version != rule_version:
            raise ValueError('the audit rule changed; review the current violation before sending')
        assurance = evaluate_audit_rules(
            result,
            (rule,),
            session_id=session_id,
            session_open=session_open,
        )
        contracts = assurance.get('contracts')
        contract = contracts[0] if isinstance(contracts, list) and contracts else None
        if not isinstance(contract, dict) or contract.get('status') != 'violated':
            raise ValueError('the audit rule is no longer violated')
        receipt = self._audit_actions.dispatch(
            session_id=session_id,
            rule=rule,
            contract=contract,
            action_message=rule.automatic_action.message if rule.automatic_action is not None else None,
        )
        return {'action': receipt, 'assurance': assurance}

    def save_audit_rule(
        self,
        rule: dict[str, object],
        *,
        expected_version: int | None,
    ) -> dict[str, object]:
        if self._audit_rules is None:
            raise ValueError('audit rule management is unavailable in this server')
        saved = self._audit_rules.upsert(
            AuditRuleDraft.model_validate(rule),
            expected_version=expected_version,
        )
        with self._live_condition:
            self._baseline_automatic_audit_actions_locked()
        return {'rule': saved.model_dump(mode='json'), 'rule_set': self._audit_rules.snapshot()}

    def archive_audit_rule(self, rule_id: str, *, expected_version: int) -> dict[str, object]:
        if self._audit_rules is None:
            raise ValueError('audit rule management is unavailable in this server')
        saved = self._audit_rules.archive(rule_id, expected_version=expected_version)
        return {'rule': saved.model_dump(mode='json'), 'rule_set': self._audit_rules.snapshot()}

    def propose_audit_rule(
        self,
        *,
        instruction: str,
        session_id: str,
        turn_id: str | None,
        event_sequence: int | None,
        view_state: dict[str, str] | None,
        client_session_nonce: str,
        cancellation_event: threading.Event | None = None,
        conversation_id: str | None = None,
        progress: AgentProgressCallback | None = None,
    ) -> dict[str, object]:
        if self._audit_rules is None:
            raise ValueError('audit rule management is unavailable in this server')
        with self._lock:
            result = self._result
        active_view_state = self._view_state_with_session_brief(result, session_id, view_state)
        dashboard_resources = self._dashboard_resource_access(
            result,
            session_id=session_id,
            turn_id=turn_id,
            event_sequence=event_sequence,
            view_state=active_view_state,
        )
        current_rules = self._audit_rules.snapshot()['rules']
        if not isinstance(current_rules, list):
            raise ValueError('audit rule store returned an invalid rule list')
        base_versions = {
            str(rule.get('id')): int(rule.get('version'))
            for rule in current_rules
            if isinstance(rule, dict) and rule.get('id') and isinstance(rule.get('version'), int)
        }
        agent_result = self._qa.propose_audit_rule(
            result,
            instruction=instruction,
            session_id=session_id,
            turn_id=turn_id,
            event_sequence=event_sequence,
            view_state=active_view_state,
            current_rules=cast('list[dict[str, object]]', current_rules),
            cancellation_event=cancellation_event,
            conversation_id=conversation_id,
            progress=progress,
            dashboard_resources=dashboard_resources,
        )
        if cancellation_event is not None and cancellation_event.is_set():
            raise StudioAgentCancelled('Dashboard agent stopped.')
        return self._audit_rules.prepare_agent_proposal(
            agent_result.content,
            instruction=instruction,
            client_session_nonce=client_session_nonce,
            base_rule_version=base_versions.get(agent_result.content.rule.id),
            harness=agent_result.harness,
            model=agent_result.model,
            provider=agent_result.provider,
        )

    def approve_audit_rule_proposal(
        self,
        proposal_id: str,
        *,
        token: str,
        client_session_nonce: str,
    ) -> dict[str, object]:
        if self._audit_rules is None:
            raise ValueError('audit rule management is unavailable in this server')
        rule = self._audit_rules.approve_agent_proposal(
            proposal_id,
            token=token,
            client_session_nonce=client_session_nonce,
        )
        with self._live_condition:
            self._baseline_automatic_audit_actions_locked()
        return {'rule': rule.model_dump(mode='json'), 'rule_set': self._audit_rules.snapshot()}

    def cancel_audit_rule_proposal(
        self,
        proposal_id: str,
        *,
        token: str,
        client_session_nonce: str,
    ) -> None:
        if self._audit_rules is None:
            raise ValueError('audit rule management is unavailable in this server')
        self._audit_rules.cancel_agent_proposal(
            proposal_id,
            token=token,
            client_session_nonce=client_session_nonce,
        )

    def _audit_rule_status(self) -> dict[str, object]:
        if self._audit_rules is None:
            return {'available': False, 'revision': 0, 'active_count': 0}
        snapshot = self._audit_rules.snapshot()
        return {
            'available': True,
            'revision': snapshot['revision'],
            'active_count': snapshot['active_count'],
            'automatic_action_count': len(
                [rule for rule in self._audit_rules.rules() if rule.enabled and rule.automatic_action is not None]
            ),
            'live_actions': self._audit_actions is not None,
        }

    def _dashboard_resource_access(
        self,
        result: AnalysisResult,
        *,
        session_id: str,
        turn_id: str | None,
        event_sequence: int | None,
        view_state: dict[str, str],
    ) -> DashboardResourceAccess:
        """Expose compact metadata and bounded public reads without preloading resource contents."""

        trace = next((item for item in result.traces if item.session_id == session_id), None)
        if trace is None:
            raise ValueError('the selected trace is no longer loaded')
        audit_status = self._audit_rule_status()
        saved_rule_count = len(self._audit_rules.rules()) if self._audit_rules is not None else 0
        brief = self._repair.session_brief(result, session_id) if self._repair is not None else {'available': False}
        agent_status = self._agent_status()
        run_summaries = _dashboard_run_summaries(agent_status)
        catalog = (
            {
                'name': 'audit_rules',
                'description': 'Saved deterministic audit-rule definitions and matchers.',
                'revision': audit_status.get('revision', 0),
                'item_count': saved_rule_count,
                'active_count': audit_status.get('active_count', 0),
                'available': bool(audit_status.get('available')),
            },
            {
                'name': 'audit_findings',
                'description': 'Current deterministic results and bounded evidence anchors for saved audit rules.',
                'revision': audit_status.get('revision', 0),
                'item_count': audit_status.get('active_count', 0),
                'available': bool(audit_status.get('available')),
            },
            {
                'name': 'session_brief',
                'description': 'Persisted Session Understanding brief, checkpoints, blockers, and next steps.',
                'revision': brief.get('revision', 0),
                'item_count': 1 if brief.get('available') else 0,
                'available': bool(brief.get('available')),
            },
            {
                'name': 'agent_runs',
                'description': 'Current and recent dashboard-agent workflow summaries and activity.',
                'revision': max((str(item.get('updated_at') or '') for item in run_summaries), default=''),
                'item_count': len(run_summaries),
                'available': bool(agent_status.get('available')),
            },
            {
                'name': 'session_metrics',
                'description': 'Current parsed session, token, turn-status, model, and tool aggregates.',
                'revision': trace.events_total,
                'item_count': 1,
                'available': True,
            },
            {
                'name': 'current_selection',
                'description': 'Current turn, event, filters, highlight, checkpoint, or selected audit finding.',
                'revision': event_sequence or 0,
                'item_count': 1,
                'available': True,
            },
        )
        safe_view_state = dict(view_state)

        def read(resource: str, resource_id: str, limit: int) -> dict[str, object]:
            return self._read_dashboard_resource(
                resource,
                resource_id=resource_id,
                limit=limit,
                result=result,
                session_id=session_id,
                turn_id=turn_id,
                event_sequence=event_sequence,
                view_state=safe_view_state,
            )

        return DashboardResourceAccess(catalog=tuple(deepcopy(catalog)), reader=read)

    def _read_dashboard_resource(
        self,
        resource: str,
        *,
        resource_id: str,
        limit: int,
        result: AnalysisResult,
        session_id: str,
        turn_id: str | None,
        event_sequence: int | None,
        view_state: dict[str, str],
    ) -> dict[str, object]:
        """Read one allowlisted public resource; never return journal rows, trace text, paths, or secrets."""

        bounded_limit = max(1, min(limit, 80))
        if resource == 'audit_rules':
            if self._audit_rules is None:
                raise ValueError('audit rules are unavailable')
            snapshot = self._audit_rules.snapshot()
            raw_rules = snapshot.get('rules')
            rules = [item for item in raw_rules if isinstance(item, dict)] if isinstance(raw_rules, list) else []
            selected = [item for item in rules if not resource_id or str(item.get('id') or '') == resource_id]
            if resource_id and not selected:
                raise ValueError(f'audit rule not found: {resource_id}')
            return {
                'resource': resource,
                'session_id': session_id,
                'revision': snapshot.get('revision', 0),
                'total_items': len(selected),
                'items': deepcopy(selected[:bounded_limit]),
                'truncated': len(selected) > bounded_limit,
            }
        if resource == 'audit_findings':
            catalog = self.audit_rule_catalog(session_id)
            assurance = catalog.get('assurance')
            assurance_value = assurance if isinstance(assurance, dict) else {}
            raw_contracts = assurance_value.get('contracts')
            contracts = (
                [item for item in raw_contracts if isinstance(item, dict)] if isinstance(raw_contracts, list) else []
            )
            selected = [item for item in contracts if not resource_id or str(item.get('id') or '') == resource_id]
            if resource_id and not selected:
                raise ValueError(f'audit finding not found: {resource_id}')
            return {
                'resource': resource,
                'session_id': session_id,
                'summary': assurance_value.get('summary', ''),
                'total_items': len(selected),
                'items': [_bounded_audit_finding(item) for item in selected[:bounded_limit]],
                'truncated': len(selected) > bounded_limit,
            }
        if resource == 'session_brief':
            if self._repair is None:
                raise ValueError('session understanding is unavailable')
            brief = deepcopy(self._repair.session_brief(result, session_id))
            brief_result = brief.get('result')
            if isinstance(brief_result, dict):
                checkpoints = brief_result.get('checkpoints')
                if isinstance(checkpoints, list):
                    brief_result['checkpoint_count'] = len(checkpoints)
                    brief_result['checkpoints'] = checkpoints[:bounded_limit]
                    brief_result['checkpoints_truncated'] = len(checkpoints) > bounded_limit
            return {'resource': resource, **brief}
        if resource == 'agent_runs':
            if self._repair is None:
                raise ValueError('dashboard-agent runs are unavailable')
            if resource_id:
                run = self._repair.public_get(resource_id)
                return {'resource': resource, 'total_items': 1, 'items': [_bounded_agent_run(run)]}
            summaries = _dashboard_run_summaries(self._agent_status())
            return {
                'resource': resource,
                'total_items': len(summaries),
                'items': summaries[:bounded_limit],
                'truncated': len(summaries) > bounded_limit,
            }
        if resource == 'session_metrics':
            return _dashboard_session_metrics(result, session_id)
        if resource == 'current_selection':
            selection_view = {key: value for key, value in view_state.items() if not key.startswith('session_brief_')}
            return {
                'resource': resource,
                'session_id': session_id,
                'turn_id': turn_id,
                'event_sequence': event_sequence,
                'view_state': deepcopy(selection_view),
            }
        raise ValueError(f'unknown dashboard resource: {resource}')

    def _decorate_audit_actions(
        self,
        assurance: dict[str, object],
        *,
        rules: tuple[AuditRule, ...],
        session_id: str,
        result: AnalysisResult,
    ) -> None:
        contracts = assurance.get('contracts')
        if not isinstance(contracts, list):
            return
        by_id = {rule.id: rule for rule in rules}
        supported = self._session_supports_agent_message(result, session_id)
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            rule = by_id.get(str(contract.get('id') or ''))
            if rule is None:
                continue
            automatic = rule.automatic_action is not None
            violation = contract.get('status') == 'violated'
            reason = ''
            if not self._live_enabled:
                reason = 'Start live monitor and audit to enable session-agent messaging.'
            elif not supported:
                reason = 'Session-agent messaging requires a Codex session with an exact UUID.'
            elif not violation:
                reason = 'The action becomes available when this rule is violated.'
            receipt = (
                self._audit_actions.latest(
                    session_id=session_id,
                    rule_id=rule.id,
                    rule_version=rule.version,
                    violation_key=self._audit_actions.violation_key(
                        session_id=session_id,
                        rule=rule,
                        contract=contract,
                    ),
                )
                if self._audit_actions is not None
                else None
            )
            failed = isinstance(receipt, dict) and receipt.get('status') in {'failed', 'interrupted'}
            available = self._live_enabled and supported and violation and (not automatic or failed)
            if automatic and violation and not receipt:
                reason = 'Automatic action armed for new violation evidence.'
            contract['action'] = {
                'kind': 'message_session_agent',
                'available': available,
                'automatic': automatic,
                'requires_user_action': not automatic or failed,
                'reason': reason,
                'receipt': receipt,
            }

    @staticmethod
    def _session_supports_agent_message(result: AnalysisResult, session_id: str) -> bool:
        try:
            parsed = UUID(session_id)
        except ValueError:
            return False
        if str(parsed) != session_id.lower():
            return False
        summary = next((candidate for candidate in result.sessions if candidate.session_id == session_id), None)
        if summary is None:
            return False
        originator = summary.originator.casefold()
        path = Path(summary.session_file)
        return 'codex' in originator or DashboardState._infer_adapter(path) == 'codex'

    def _session_open_for_audit_locked(self, session_file: str) -> bool:
        if not self._live_enabled:
            return False
        source = self._live_sources.get(str(Path(session_file).resolve()))
        return isinstance(source, dict) and source.get('status') not in {'completed', 'failed'}

    def _view_state_with_session_brief(
        self,
        result: AnalysisResult,
        session_id: str,
        view_state: dict[str, str] | None,
    ) -> dict[str, str]:
        active = dict(view_state or {})
        if self._repair is None:
            return active
        brief = self._repair.session_brief(result, session_id)
        summary = brief.get('result')
        if not isinstance(summary, dict):
            return active
        active.update(
            {
                'session_brief_revision': str(brief.get('revision') or ''),
                'session_brief_through_sequence': str(brief.get('through_event_sequence') or 0),
                'session_brief_event_count': str(brief.get('event_count') or 0),
                'session_brief_new_event_count': str(brief.get('new_event_count') or 0),
                'session_brief_title': str(summary.get('title') or ''),
                'session_brief_summary': str(summary.get('summary') or ''),
                'session_brief_objective': str(summary.get('objective') or ''),
                'session_brief_outcome': str(summary.get('outcome') or 'unclear'),
                'session_brief_checkpoints': json.dumps(
                    summary.get('checkpoints') or [],
                    ensure_ascii=True,
                    separators=(',', ':'),
                ),
                'session_brief_blockers': json.dumps(
                    summary.get('blockers') or [],
                    ensure_ascii=True,
                    separators=(',', ':'),
                ),
                'session_brief_next_steps': json.dumps(
                    summary.get('next_steps') or [],
                    ensure_ascii=True,
                    separators=(',', ':'),
                ),
            }
        )
        return active

    def configure_qa(
        self,
        *,
        api_key: str | None,
        provider: str,
        model: str,
        base_url: str,
        remember: bool,
        vault_password: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            return self._qa.configure(
                api_key=api_key,
                provider=provider,
                model=model,
                base_url=base_url,
                remember=remember,
                vault_password=vault_password,
            )

    def unlock_qa(self, *, vault_password: str | None) -> dict[str, object]:
        with self._lock:
            return self._qa.unlock(vault_password=vault_password)

    def clear_qa_api_key(self) -> dict[str, object]:
        with self._lock:
            return self._qa.clear_api_key()

    def clear_studio_conversation(self, *, conversation_id: str, session_id: str) -> dict[str, object]:
        removed = self._qa.clear_conversation(conversation_id=conversation_id, session_id=session_id)
        return {'cleared': True, 'harness_branches_removed': removed}

    def configure_agent_harness(self, *, harness: str) -> dict[str, object]:
        if self._repair is None:
            raise ValueError('agent workflows are unavailable in this server')
        return {
            'agent': self._repair.configure_harness(harness),
            'qa': self._qa.public_status(),
        }

    def start_agent(
        self,
        kind: RepairKind,
        dashboard_state: dict[str, object],
        *,
        audit_run_id: str | None = None,
        instruction: str | None = None,
    ) -> dict[str, object]:
        if self._repair is None:
            raise ValueError('agent workflows are unavailable in this server')
        with self._lock:
            result = self._result
            settings = self._qa.settings
        return self._repair.start(
            kind,
            result=result,
            dashboard_state=dashboard_state,
            settings=settings,
            audit_run_id=audit_run_id,
            instruction=instruction,
        )

    def prepare_source_action(
        self,
        *,
        action: str,
        dashboard_state: dict[str, object],
        instruction: str,
        client_session_nonce: str,
        audit_run_id: str | None = None,
    ) -> dict[str, object]:
        if self._repair is None:
            raise ValueError('agent workflows are unavailable in this server')
        with self._lock:
            result = self._result
        return self._repair.prepare_source_action(
            cast('SourceMutationAction', action),
            result=result,
            dashboard_state=dashboard_state,
            instruction=instruction,
            client_session_nonce=client_session_nonce,
            audit_run_id=audit_run_id,
        )

    def approve_source_action(
        self,
        *,
        authorization_id: str,
        token: str,
        client_session_nonce: str,
    ) -> dict[str, object]:
        if self._repair is None:
            raise ValueError('agent workflows are unavailable in this server')
        with self._lock:
            result = self._result
            settings = self._qa.settings
        run = self._repair.approve_source_action(
            authorization_id=authorization_id,
            token=token,
            client_session_nonce=client_session_nonce,
            result=result,
            settings=settings,
        )
        return self._refresh_dashboard_bundle(run)

    def cancel_source_action(
        self,
        *,
        authorization_id: str,
        token: str,
        client_session_nonce: str,
    ) -> None:
        if self._repair is None:
            raise ValueError('agent workflows are unavailable in this server')
        self._repair.cancel_source_action(
            authorization_id=authorization_id,
            token=token,
            client_session_nonce=client_session_nonce,
        )

    def agent_run(self, run_id: str) -> dict[str, object]:
        if self._repair is None:
            raise ValueError('agent workflows are unavailable')
        public_get = getattr(self._repair, 'public_get', self._repair.get)
        run = self._refresh_dashboard_bundle(public_get(run_id))
        conversation_answer = _agent_run_conversation_answer(run)
        if conversation_answer is None:
            return run
        response = deepcopy(run)
        response['conversation_answer'] = conversation_answer
        return response

    def _refresh_dashboard_bundle(self, run: dict[str, object]) -> dict[str, object]:
        result = run.get('result')
        if (
            run.get('status') != 'passed'
            or run.get('kind') not in {'repair', 'customize'}
            or not isinstance(result, dict)
            or not result.get('restart_required')
        ):
            return run
        if not _run_authorizes_verified_activation(run):
            response = deepcopy(run)
            response['status'] = 'blocked'
            response['message'] = 'The verified source is waiting for explicit runtime activation approval.'
            response['recovery'] = {
                'reason': 'The original source approval did not include activation of this verified revision.',
                'category': 'activation_approval_required',
                'can_continue': False,
                'workspace_preserved': True,
                'actions': ['activate'],
            }
            response['dashboard_refresh'] = {
                'status': 'approval_required',
                'message': 'Verified runtime activation was not included in the consumed source approval.',
                'auto_reload': False,
            }
            return response
        run_id = str(run.get('run_id') or '')
        changed_files = result.get('changed_files')
        files = [str(value).replace('\\', '/') for value in changed_files] if isinstance(changed_files, list) else []
        runtime_files = [value for value in files if value.startswith('src/agent_trace_studio/')]
        can_refresh = bool(runtime_files) and all(value.startswith(_DASHBOARD_ASSET_PREFIX) for value in runtime_files)
        if not can_refresh and self._supervisor is not None:
            return self._activate_verified_runtime(run, result)
        with self._lock:
            refresh = self._dashboard_refreshes.get(run_id)
            if refresh is None:
                if self._output_dir is None or not can_refresh:
                    refresh = {
                        'status': 'restart_required',
                        'message': 'Restart Agent Trace Studio to load the updated Python source.',
                    }
                else:
                    try:
                        bundle = write_report(
                            self._result,
                            output_dir=self._output_dir,
                            title=self._title,
                            assurance=self._assurance,
                        )
                    except (OSError, UnicodeError, ValueError) as exc:
                        refresh = {
                            'status': 'failed',
                            'message': f'Automatic dashboard refresh failed: {exc}',
                        }
                    else:
                        published: dict[str, object] | None = None
                        if self._supervisor is not None:
                            try:
                                published = self._supervisor.publish()
                            except (OSError, ValueError, urllib.error.URLError):
                                published = None
                        refresh = {
                            'status': 'ready',
                            'message': 'The updated dashboard bundle is ready.',
                            'revision': str(bundle.index_path.stat().st_mtime_ns),
                            'auto_reload': True,
                            'published': published,
                        }
                self._dashboard_refreshes[run_id] = refresh
        response = deepcopy(run)
        response['dashboard_refresh'] = deepcopy(refresh)
        response_result = response.get('result')
        if isinstance(response_result, dict) and refresh.get('status') == 'ready':
            if self._repair is not None:
                response = self._repair.record_bundle_refresh(run_id, refresh)
                response['dashboard_refresh'] = deepcopy(refresh)
            else:
                response_result['restart_required'] = False
                response_result['dashboard_refreshed'] = True
                response['message'] = 'Local source passed verification and the updated dashboard bundle is ready.'
        return response

    def _activate_verified_runtime(
        self,
        run: dict[str, object],
        result: dict[str, object],
    ) -> dict[str, object]:
        if self._repair is None or self._supervisor is None:
            return run
        if not _run_authorizes_verified_activation(run):
            raise ValueError('verified runtime activation was not authorized')
        run_id = str(run.get('run_id') or '')
        change_digest = str(result.get('change_digest') or '')
        activation_id = f'{run_id}:{change_digest}'
        with self._deployment_lock:
            deployment = self._runtime_deployments.get(activation_id)
            now = time.monotonic()
            if deployment is not None and deployment.get('status') == 'retrying':
                retry_at = deployment.get('_retry_at')
                if isinstance(retry_at, int | float) and now < retry_at:
                    return _run_with_pending_deployment(run, deployment)
            if deployment is not None and deployment.get('status') == 'promoted':
                try:
                    supervisor_status = self._supervisor.status()
                except (OSError, ValueError, urllib.error.URLError):
                    return _run_with_pending_deployment(
                        run,
                        {
                            'status': 'retrying',
                            'run_id': run_id,
                            'message': 'Confirming the active runtime generation with the supervisor.',
                        },
                    )
                active = supervisor_status.get('active')
                active_payload = active if isinstance(active, dict) else {}
                if active_payload.get('generation') == deployment.get('generation') and active_payload.get('alive'):
                    return self._repair.record_deployment_result(run_id, _public_deployment(deployment))
                self._runtime_deployments.pop(activation_id, None)
                deployment = None
            previous_attempts = int(deployment.get('_retry_attempts') or 0) if deployment is not None else 0
            try:
                deployment = self._supervisor.activate(
                    {
                        'run_id': run_id,
                        'change_digest': change_digest,
                        'changed_files': result.get('changed_files'),
                        'rollback_manifest': {
                            'backup_path': result.get('backup_path'),
                            'source_delta': result.get('source_delta'),
                            'applied_records': result.get('applied_records'),
                        },
                    }
                )
            except (OSError, ValueError, urllib.error.URLError) as exc:
                attempts = previous_attempts + 1
                delay = min(2 ** min(attempts, 5), 30)
                deployment = {
                    'status': 'retrying',
                    'run_id': run_id,
                    'message': f'Supervisor connection failed; retrying activation in {delay} seconds: {exc}',
                    'retry_count': attempts,
                    'retry_in_seconds': delay,
                    '_retry_attempts': attempts,
                    '_retry_at': now + delay,
                }
                self._runtime_deployments[activation_id] = deployment
                return _run_with_pending_deployment(run, deployment)
            if deployment.get('status') == 'promoted':
                self._runtime_deployments[activation_id] = deployment
            else:
                self._runtime_deployments.pop(activation_id, None)
            return self._repair.record_deployment_result(run_id, deployment)

    def authorize_supervisor(self, authorization: str | None) -> bool:
        return self._supervisor is not None and self._supervisor.authorize(authorization)

    def record_runtime_deployment(
        self,
        *,
        run_id: str,
        deployment: dict[str, object],
    ) -> dict[str, object]:
        if self._repair is None:
            raise ValueError('agent workflows are unavailable')
        return self._repair.record_deployment_result(run_id, deployment)

    def _deployment_status(self) -> dict[str, object]:
        context = child_deployment_context()
        if self._supervisor is None:
            return context
        try:
            supervisor = self._supervisor.status()
        except (OSError, ValueError, urllib.error.URLError) as exc:
            context['supervisor_status'] = 'unavailable'
            context['message'] = str(exc)
        else:
            context['supervisor_status'] = str(supervisor.get('status') or 'unknown')
            context['active'] = supervisor.get('active')
            context['standby'] = supervisor.get('standby')
            context['last_deployment'] = supervisor.get('last_deployment')
        return context

    def _monitor_verified_deployments(self) -> None:
        while not self._deployment_stop.wait(1):
            if self._repair is None or self._supervisor is None:
                return
            context = child_deployment_context()
            generation = str(context.get('generation') or '')
            try:
                supervisor = self._supervisor.status()
            except (OSError, ValueError, urllib.error.URLError):
                continue
            active = supervisor.get('active')
            active_payload = active if isinstance(active, dict) else {}
            if not generation or active_payload.get('generation') != generation:
                continue
            last_deployment = supervisor.get('last_deployment')
            if isinstance(last_deployment, dict) and last_deployment.get('status') in {'failed', 'rolled_back'}:
                failed_run_id = last_deployment.get('run_id')
                if isinstance(failed_run_id, str) and failed_run_id:
                    try:
                        failed_run = self._repair.get(failed_run_id)
                        recorded = failed_run.get('deployment')
                        if not isinstance(recorded, dict) or recorded.get('status') != last_deployment.get('status'):
                            self._repair.record_deployment_result(failed_run_id, last_deployment)
                    except (OSError, RuntimeError, ValueError):
                        pass
            pending_deployments = getattr(self._repair, 'pending_deployments', None)
            pending_runs = pending_deployments() if callable(pending_deployments) else []
            for run in pending_runs:
                try:
                    self._refresh_dashboard_bundle(run)
                except (OSError, RuntimeError, ValueError, urllib.error.URLError):
                    continue

    def cancel_agent_run(self, run_id: str) -> dict[str, object]:
        if self._repair is None:
            raise ValueError('agent workflows are unavailable')
        return self._repair.cancel(run_id)

    def act_on_agent_run(
        self,
        run_id: str,
        *,
        action: str,
        instruction: str | None,
        client_session_nonce: str | None = None,
    ) -> dict[str, object]:
        if self._repair is None:
            raise ValueError('agent workflows are unavailable')
        with self._lock:
            result = self._result
            settings = self._qa.settings
        if action in {'continue', 'restart', 'activate'}:
            if client_session_nonce is None:
                raise ValueError('client session nonce is required for source-action approval')
            authorization = self._repair.prepare_run_action(
                run_id,
                action=action,
                result=result,
                instruction=instruction,
                client_session_nonce=client_session_nonce,
            )
            return {
                'kind': 'authorization',
                'action': f'{action}_run',
                'answer': 'Review and approve this source-changing run action.',
                'authorization': authorization,
            }
        return self._repair.act(
            run_id,
            action=action,
            result=result,
            settings=settings,
            instruction=instruction,
        )

    def _add_paths(self, paths: tuple[Path, ...]) -> dict[str, object]:
        with self._lock:
            combined_paths = tuple(dict.fromkeys((*self._source_paths, *paths)))
            if len(combined_paths) > _MAX_LOADED_FILES:
                raise ValueError(f'no more than {_MAX_LOADED_FILES} session files may be loaded')
            result = self._analyze(combined_paths)
            if combined_paths == self._source_paths:
                self._result = result
                if self._live_enabled:
                    self._publish_live_update_locked()
                return self._payload_locked()
            skipped = set(result.skipped_files)
            if any(str(path) in skipped for path in paths):
                raise ValueError('file does not contain a recognized agent trace')
            if not result.sessions or not result.traces:
                raise ValueError('file does not contain a recognized agent trace')
            added_files = {str(path) for path in paths}
            added_session_ids = [
                session.session_id for session in result.sessions if session.session_file in added_files
            ]
            existing_session_ids = {session.session_id for session in self._result.sessions}
            duplicate_session_id = next(
                (
                    session_id
                    for session_id in added_session_ids
                    if session_id in existing_session_ids or added_session_ids.count(session_id) > 1
                ),
                None,
            )
            if duplicate_session_id:
                raise ValueError(f'session {duplicate_session_id} is already loaded')
            self._result = result
            self._source_paths = combined_paths
            for path in paths:
                self._fingerprints[path] = fingerprint(path)
                if self._live_enabled:
                    self._live_sources.setdefault(
                        str(path),
                        {
                            'adapter': 'file',
                            'session_id': self._session_id_for_path(path),
                            'path': str(path),
                            'status': 'watching',
                        },
                    )
            if self._live_enabled:
                self._baseline_automatic_audit_actions_locked()
                self._publish_live_update_locked()
            return self._payload_locked()

    def _payload_locked(
        self,
        *,
        qa_status: dict[str, object] | None = None,
        agent_status: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload = build_dashboard_payload(self._result, title=self._title, assurance=self._assurance)
        payload['runtime'] = {
            'interactive': True,
            'qa': qa_status if qa_status is not None else self._qa.public_status(),
            'agent': agent_status if agent_status is not None else self._agent_status(),
            'live': self._live_status_locked(),
            'audit_rules': self._audit_rule_status(),
        }
        return payload

    def _analyze(self, paths: tuple[Path, ...]) -> AnalysisResult:
        return analyze_journals(
            paths,
            from_time=self._from_time,
            to_time=self._to_time,
            include_trace=True,
            trace_char_limit=self._trace_char_limit,
        )

    def _monitor_live_sources(self) -> None:
        while not self._live_stop.is_set():
            self._live_wake.wait(self._live_poll_interval)
            self._live_wake.clear()
            if self._live_stop.is_set():
                return
            with self._lock:
                paths = self._source_paths
                previous = dict(self._fingerprints)
            current = {path: fingerprint(path) for path in paths}
            if current == previous:
                continue
            try:
                result = self._analyze(paths)
            except Exception as exc:
                with self._live_condition:
                    self._live_error = f'{type(exc).__name__}: {exc}'
                    self._publish_live_update_locked()
                continue
            with self._live_condition:
                if paths != self._source_paths:
                    continue
                self._result = result
                self._fingerprints = current
                self._live_error = ''
                for path, state in current.items():
                    source = self._live_sources.get(str(path))
                    if source is not None and source.get('status') != 'completed':
                        source['status'] = 'watching' if state.exists else 'missing'
                self._run_automatic_audit_actions_locked()
                self._publish_live_update_locked()

    def _baseline_automatic_audit_actions_locked(self) -> None:
        for session_id, rule, contract in self._automatic_audit_violations_locked():
            if self._audit_actions is None:
                return
            self._automatic_action_seen.add(
                self._audit_actions.violation_key(session_id=session_id, rule=rule, contract=contract)
            )

    def _run_automatic_audit_actions_locked(self) -> None:
        if not self._live_enabled or self._audit_actions is None:
            return
        pending = set(self._automatic_action_pending_baseline)
        materialized_sessions = {trace.session_id for trace in self._result.traces}
        for session_id, rule, contract in self._automatic_audit_violations_locked():
            key = self._audit_actions.violation_key(session_id=session_id, rule=rule, contract=contract)
            if session_id in pending:
                self._automatic_action_seen.add(key)
                continue
            if key in self._automatic_action_seen:
                continue
            self._automatic_action_seen.add(key)
            action = rule.automatic_action
            if action is None:
                continue
            self._audit_actions.dispatch(
                session_id=session_id,
                rule=rule,
                contract=contract,
                action_message=action.message,
                automatic=True,
            )
        self._automatic_action_pending_baseline.difference_update(materialized_sessions)

    def _automatic_audit_violations_locked(self) -> list[tuple[str, AuditRule, dict[str, object]]]:
        if self._audit_rules is None or self._audit_actions is None:
            return []
        rules = tuple(rule for rule in self._audit_rules.rules() if rule.enabled and rule.automatic_action is not None)
        if not rules:
            return []
        violations: list[tuple[str, AuditRule, dict[str, object]]] = []
        by_id = {rule.id: rule for rule in rules}
        for trace in self._result.traces:
            session_id = trace.session_id
            if not self._session_supports_agent_message(self._result, session_id):
                continue
            assurance = evaluate_audit_rules(
                self._result,
                rules,
                session_id=session_id,
                session_open=self._session_open_for_audit_locked(trace.session_file),
            )
            contracts = assurance.get('contracts')
            if not isinstance(contracts, list):
                continue
            for contract in contracts:
                if not isinstance(contract, dict) or contract.get('status') != 'violated':
                    continue
                rule = by_id.get(str(contract.get('id') or ''))
                if rule is not None:
                    violations.append((session_id, rule, contract))
        return violations

    def _publish_live_update_locked(self) -> None:
        self._live_revision += 1
        self._live_changed_at = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
        self._live_condition.notify_all()

    def _publish_audit_action_update(self) -> None:
        with self._live_condition:
            if self._live_enabled and not self._closed:
                self._publish_live_update_locked()

    def _live_status_locked(self) -> dict[str, object]:
        state = 'disabled'
        if self._live_enabled:
            state = 'error' if self._live_error else ('watching' if self._live_connected else 'starting')
        return {
            'enabled': self._live_enabled,
            'state': state,
            'revision': self._live_revision,
            'updated_at': self._live_changed_at,
            'monitored_files': len(self._source_paths) if self._live_enabled else 0,
            'error': self._live_error,
            'sources': list(self._live_sources.values()) if self._live_enabled else [],
            'audit': {
                'enabled': self._live_enabled and self._audit_rules is not None,
                'active_rules': len([rule for rule in self._audit_rules.rules() if rule.enabled])
                if self._audit_rules is not None
                else 0,
                'automatic_actions': len(
                    [rule for rule in self._audit_rules.rules() if rule.enabled and rule.automatic_action is not None]
                )
                if self._audit_rules is not None
                else 0,
                'message_action': 'automatic_and_manual' if self._audit_actions is not None else 'unavailable',
            },
        }

    def _session_id_for_path(self, path: Path) -> str:
        target = str(path)
        session = next((item for item in self._result.sessions if item.session_file == target), None)
        return session.session_id if session is not None else path.stem

    @staticmethod
    def _infer_adapter(path: Path) -> str:
        normalized = str(path).replace('\\', '/').lower()
        if '/.claude/' in normalized:
            return 'claude-code'
        if '/.codex/' in normalized or path.name.startswith('rollout-'):
            return 'codex'
        return 'file'

    @staticmethod
    def _validate_live_path(path: Path) -> None:
        if path.suffix.lower() not in _JOURNAL_SUFFIXES:
            raise ValueError('transcript file must use .json or .jsonl')
        if path.is_file() and path.stat().st_size > _MAX_UPLOAD_BYTES:
            raise ValueError('transcript file exceeds the 256 MB limit')

    def _agent_status(self) -> dict[str, object]:
        if self._repair is None:
            return {
                'available': False,
                'reason': 'Agent workflows are not configured for this server.',
            }
        return self._repair.status()

    def _controller_workflow_state(self) -> dict[str, object]:
        status = self._agent_status()
        live = self.live_status()
        live_audit = live.get('audit') if isinstance(live.get('audit'), dict) else {}
        return {
            'available': bool(status.get('available')),
            'workflows': status.get('workflows') if isinstance(status.get('workflows'), dict) else {},
            'active_run_id': status.get('active_run_id'),
            'latest_run': status.get('latest_run') if isinstance(status.get('latest_run'), dict) else None,
            'audit_rules': self._audit_rule_status(),
            'live_monitor': {
                'enabled': bool(live.get('enabled')),
                'state': live.get('state'),
                'revision': live.get('revision'),
                'monitored_files': live.get('monitored_files'),
                'audit': live_audit,
            },
        }

    @staticmethod
    def _validate_journal(path: Path) -> None:
        if not path.is_file():
            raise ValueError(f'session file not found: {path}')
        if path.suffix.lower() not in _JOURNAL_SUFFIXES:
            raise ValueError('session file must use .json or .jsonl')
        if path.stat().st_size > _MAX_UPLOAD_BYTES:
            raise ValueError('session file exceeds the 256 MB limit')


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    """Serve the dashboard plus its local-only JSON API."""

    server_version = 'AgentTraceStudio/0.9'

    def __init__(self, *args: object, state: DashboardState, **kwargs: object) -> None:
        self.dashboard_state = state
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        if not self._client_is_loopback():
            self._send_api_error(HTTPStatus.FORBIDDEN, 'loopback access only')
            return
        route = urllib.parse.urlsplit(self.path).path
        if route == '/api/live/stream':
            self._send_live_stream()
            return
        if route == '/api/status':
            self._send_json(HTTPStatus.OK, self.dashboard_state.status())
            return
        if route == '/api/payload':
            self._send_json(HTTPStatus.OK, self.dashboard_state.payload())
            return
        if route == '/api/audit-rules':
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            session_id = _optional_text(next(iter(query.get('session_id', [])), None))
            if session_id is None:
                self._send_api_error(HTTPStatus.BAD_REQUEST, 'session_id is required')
                return
            try:
                self._send_json(HTTPStatus.OK, self.dashboard_state.audit_rule_catalog(session_id))
            except ValueError as exc:
                self._send_api_error(HTTPStatus.NOT_FOUND, str(exc))
            return
        if route == '/api/agent/session-brief':
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            session_id = _optional_text(next(iter(query.get('session_id', [])), None))
            if session_id is None:
                self._send_api_error(HTTPStatus.BAD_REQUEST, 'session_id is required')
                return
            try:
                self._send_json(HTTPStatus.OK, self.dashboard_state.session_brief(session_id))
            except ValueError as exc:
                self._send_api_error(HTTPStatus.NOT_FOUND, str(exc))
            return
        if route.startswith('/api/agent/messages/'):
            request_id = route.removeprefix('/api/agent/messages/').strip('/')
            try:
                self._send_json(HTTPStatus.OK, self.dashboard_state.studio_request(request_id))
            except ValueError as exc:
                self._send_api_error(HTTPStatus.NOT_FOUND, str(exc))
            return
        if route.startswith('/api/agent/runs/'):
            run_id = route.removeprefix('/api/agent/runs/')
            try:
                self._send_json(HTTPStatus.OK, self.dashboard_state.agent_run(run_id))
            except ValueError as exc:
                self._send_api_error(HTTPStatus.NOT_FOUND, str(exc))
            return
        super().do_GET()

    def do_POST(self) -> None:
        if not self._client_is_loopback():
            self._send_api_error(HTTPStatus.FORBIDDEN, 'loopback access only')
            return
        try:
            route = urllib.parse.urlsplit(self.path).path
            if route == '/api/runtime/deployment':
                if not self.dashboard_state.authorize_supervisor(self.headers.get('Authorization')):
                    self._send_api_error(HTTPStatus.UNAUTHORIZED, 'supervisor authorization failed')
                    return
                request = self._read_json()
                response = self.dashboard_state.record_runtime_deployment(
                    run_id=_required_text(request, 'run_id'),
                    deployment=_required_object(request, 'deployment'),
                )
                self._send_json(HTTPStatus.OK, response)
                return
            if route == '/api/live/start':
                self._send_json(
                    HTTPStatus.OK,
                    {'live': self.dashboard_state.start_live_monitoring()},
                )
                return
            if route == '/api/live/audit-actions/message':
                request = self._read_json()
                response = self.dashboard_state.send_audit_message(
                    session_id=_required_text(request, 'session_id'),
                    rule_id=_required_text(request, 'rule_id'),
                    rule_version=_required_positive_int(request.get('rule_version'), 'rule_version'),
                )
                self._send_json(HTTPStatus.ACCEPTED, response)
                return
            if route in {'/api/live/register', '/api/live/events', '/api/live/end'}:
                if not self.dashboard_state.authorize_live_ingest(self.headers.get('Authorization')):
                    self._send_api_error(HTTPStatus.UNAUTHORIZED, 'live ingestion authorization failed')
                    return
                request = self._read_json()
                if route == '/api/live/register':
                    response = self.dashboard_state.register_live_source(request)
                elif route == '/api/live/events':
                    response = self.dashboard_state.ingest_live_event(request)
                else:
                    response = self.dashboard_state.finish_live_source(request)
                self._send_json(HTTPStatus.ACCEPTED, response)
                return
            if route == '/api/session/id':
                request = self._read_json()
                session_id = _required_text(request, 'session_id')
                self._send_json(HTTPStatus.OK, self.dashboard_state.load_session_id(session_id))
                return
            if route == '/api/session/path':
                request = self._read_json()
                path = _required_text(request, 'path')
                self._send_json(HTTPStatus.OK, self.dashboard_state.load_path(path))
                return
            if route == '/api/session/upload':
                filename = urllib.parse.unquote(self.headers.get('X-Session-Filename', 'uploaded-session.jsonl'))
                content = self._read_body(_MAX_UPLOAD_BYTES)
                self._send_json(HTTPStatus.OK, self.dashboard_state.load_upload(filename, content))
                return
            if route == '/api/audit-rules':
                request = self._read_json()
                response = self.dashboard_state.save_audit_rule(
                    _required_object(request, 'rule'),
                    expected_version=_optional_nonnegative_int(request.get('expected_version'), 'expected_version'),
                )
                self._send_json(HTTPStatus.OK, response)
                return
            if route == '/api/audit-rules/proposals':
                request = self._read_json()
                session_id = _required_text(request, 'session_id')
                turn_id = _optional_text(request.get('turn_id'))
                event_sequence, view_state = _qa_dashboard_context(
                    request,
                    session_id=session_id,
                    turn_id=turn_id,
                )
                proposal = self.dashboard_state.propose_audit_rule(
                    instruction=_required_text(request, 'instruction'),
                    session_id=session_id,
                    turn_id=turn_id,
                    event_sequence=event_sequence,
                    view_state=view_state,
                    client_session_nonce=_required_text(request, 'client_session_nonce'),
                    conversation_id=_optional_conversation_id(request.get('conversation_id')),
                )
                self._send_json(HTTPStatus.ACCEPTED, {'proposal': proposal})
                return
            if route.startswith('/api/audit-rules/proposals/') and route.endswith('/approve'):
                proposal_id = route.removeprefix('/api/audit-rules/proposals/').removesuffix('/approve').strip('/')
                request = self._read_json()
                response = self.dashboard_state.approve_audit_rule_proposal(
                    proposal_id,
                    token=_required_text(request, 'token'),
                    client_session_nonce=_required_text(request, 'client_session_nonce'),
                )
                self._send_json(HTTPStatus.OK, response)
                return
            if route.startswith('/api/audit-rules/proposals/') and route.endswith('/cancel'):
                proposal_id = route.removeprefix('/api/audit-rules/proposals/').removesuffix('/cancel').strip('/')
                request = self._read_json()
                self.dashboard_state.cancel_audit_rule_proposal(
                    proposal_id,
                    token=_required_text(request, 'token'),
                    client_session_nonce=_required_text(request, 'client_session_nonce'),
                )
                self._send_json(HTTPStatus.OK, {'cancelled': True})
                return
            if self.path == '/api/qa/config':
                request = self._read_json()
                status = self.dashboard_state.configure_qa(
                    api_key=_optional_text(request.get('api_key')),
                    provider=_required_text(request, 'provider'),
                    model=_required_text(request, 'model'),
                    base_url=_required_text(request, 'base_url'),
                    remember=_boolean(request.get('remember'), default=True),
                    vault_password=_optional_secret(request.get('vault_password')),
                )
                self._send_json(HTTPStatus.OK, {'qa': status})
                return
            if self.path == '/api/qa/unlock':
                request = self._read_json()
                status = self.dashboard_state.unlock_qa(
                    vault_password=_optional_secret(request.get('vault_password')),
                )
                self._send_json(HTTPStatus.OK, {'qa': status})
                return
            if self.path == '/api/agent/message':
                request = self._read_json()
                request_id = _required_text(request, 'request_id')
                history = request.get('history')
                normalized_history = (
                    [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
                )
                session_id = _required_text(request, 'session_id')
                turn_id = _optional_text(request.get('turn_id'))
                event_sequence, view_state = _qa_dashboard_context(
                    request,
                    session_id=session_id,
                    turn_id=turn_id,
                )
                cancellation_event = self.dashboard_state.begin_studio_request(request_id)
                try:
                    response = self.dashboard_state.handle_agent_message(
                        message=_required_text(request, 'message'),
                        scope=_optional_text(request.get('scope')) or 'journal',
                        session_id=session_id,
                        turn_id=turn_id,
                        dashboard_state=_required_object(request, 'dashboard_state'),
                        history=cast('list[dict[str, object]]', normalized_history),
                        client_session_nonce=_optional_text(request.get('client_session_nonce')),
                        event_sequence=event_sequence,
                        view_state=view_state,
                        cancellation_event=cancellation_event,
                        conversation_id=_optional_conversation_id(request.get('conversation_id')),
                        progress=partial(self.dashboard_state.record_studio_activity, request_id),
                    )
                except StudioAgentCancelled:
                    self.dashboard_state.finish_studio_request(
                        request_id,
                        cancellation_event,
                        status='cancelled',
                    )
                    raise
                except Exception:
                    self.dashboard_state.finish_studio_request(
                        request_id,
                        cancellation_event,
                        status='failed',
                    )
                    raise
                else:
                    response['request_activity'] = self.dashboard_state.finish_studio_request(
                        request_id,
                        cancellation_event,
                    )
                self._send_json(
                    HTTPStatus.ACCEPTED
                    if response.get('kind') in {'workflow', 'authorization', 'audit_rule_proposal'}
                    else HTTPStatus.OK,
                    response,
                )
                return
            if self.path == '/api/qa':
                request = self._read_json()
                history = request.get('history')
                normalized_history = (
                    [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
                )
                session_id = _required_text(request, 'session_id')
                turn_id = _optional_text(request.get('turn_id'))
                event_sequence, view_state = _qa_dashboard_context(
                    request,
                    session_id=session_id,
                    turn_id=turn_id,
                )
                response = self.dashboard_state.ask(
                    question=_required_text(request, 'question'),
                    scope=_required_text(request, 'scope'),
                    session_id=session_id,
                    turn_id=turn_id,
                    history=cast('list[dict[str, object]]', normalized_history),
                    event_sequence=event_sequence,
                    view_state=view_state,
                    conversation_id=_optional_conversation_id(request.get('conversation_id')),
                )
                self._send_json(HTTPStatus.OK, response)
                return
            if self.path == '/api/agent/config':
                request = self._read_json()
                response = self.dashboard_state.configure_agent_harness(
                    harness=_required_text(request, 'harness'),
                )
                self._send_json(HTTPStatus.OK, response)
                return
            if route.startswith('/api/agent/source-actions/') and route.endswith('/approve'):
                authorization_id = route.removeprefix('/api/agent/source-actions/').removesuffix('/approve').strip('/')
                request = self._read_json()
                run = self.dashboard_state.approve_source_action(
                    authorization_id=authorization_id,
                    token=_required_text(request, 'token'),
                    client_session_nonce=_required_text(request, 'client_session_nonce'),
                )
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    {
                        'kind': 'workflow',
                        'answer': 'Source change approved; the verified workflow is starting.',
                        'run': run,
                    },
                )
                return
            if route.startswith('/api/agent/source-actions/') and route.endswith('/cancel'):
                authorization_id = route.removeprefix('/api/agent/source-actions/').removesuffix('/cancel').strip('/')
                request = self._read_json()
                self.dashboard_state.cancel_source_action(
                    authorization_id=authorization_id,
                    token=_required_text(request, 'token'),
                    client_session_nonce=_required_text(request, 'client_session_nonce'),
                )
                self._send_json(HTTPStatus.OK, {'kind': 'cancelled', 'message': 'Source change approval cancelled.'})
                return
            if route.startswith('/api/agent/runs/') and route.endswith('/actions'):
                run_id = route.removeprefix('/api/agent/runs/').removesuffix('/actions').strip('/')
                request = self._read_json()
                response = self.dashboard_state.act_on_agent_run(
                    run_id,
                    action=_required_text(request, 'action'),
                    instruction=_optional_text(request.get('instruction')),
                    client_session_nonce=_optional_text(request.get('client_session_nonce')),
                )
                self._send_json(HTTPStatus.ACCEPTED, response)
                return
            if self.path in {'/api/agent/repair', '/api/agent/customize'}:
                request = self._read_json()
                action = 'repair_parser' if self.path.endswith('/repair') else 'customize_dashboard'
                authorization = self.dashboard_state.prepare_source_action(
                    action=action,
                    dashboard_state=_required_object(request, 'dashboard_state'),
                    instruction=_required_text(request, 'instruction'),
                    client_session_nonce=_required_text(request, 'client_session_nonce'),
                    audit_run_id=_optional_text(request.get('audit_run_id')),
                )
                self._send_json(
                    HTTPStatus.ACCEPTED,
                    {
                        'kind': 'authorization',
                        'action': action,
                        'answer': action_acknowledgement(cast('ControllerAction', action)),
                        'authorization': authorization,
                    },
                )
                return
            agent_routes: dict[str, RepairKind] = {
                '/api/agent/investigate': 'investigate',
                '/api/agent/memories': 'memories',
                '/api/agent/checkpoints': 'checkpoints',
                '/api/agent/audit': 'audit',
            }
            if self.path in agent_routes:
                request = self._read_json()
                dashboard_state = _required_object(request, 'dashboard_state')
                kind = agent_routes[self.path]
                response = self.dashboard_state.start_agent(
                    kind,
                    dashboard_state,
                    audit_run_id=_optional_text(request.get('audit_run_id')),
                    instruction=_optional_text(request.get('instruction')),
                )
                self._send_json(HTTPStatus.ACCEPTED, response)
                return
            self._send_api_error(HTTPStatus.NOT_FOUND, 'unknown API endpoint')
        except (BrokenPipeError, ConnectionResetError):
            return
        except StudioAgentCancelled as exc:
            self._send_json(HTTPStatus.CONFLICT, {'error': str(exc), 'cancelled': True})
        except QAUnavailableError as exc:
            self._send_api_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        except (CredentialPersistenceError, ValueError, OSError, UnicodeError) as exc:
            self._send_api_error(HTTPStatus.BAD_REQUEST, str(exc))

    def do_DELETE(self) -> None:
        if not self._client_is_loopback():
            self._send_api_error(HTTPStatus.FORBIDDEN, 'loopback access only')
            return
        route = urllib.parse.urlsplit(self.path).path
        if route == '/api/qa/config':
            try:
                self._send_json(HTTPStatus.OK, {'qa': self.dashboard_state.clear_qa_api_key()})
            except CredentialPersistenceError as exc:
                self._send_api_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if route.startswith('/api/agent/conversations/'):
            try:
                conversation_id = _optional_conversation_id(route.removeprefix('/api/agent/conversations/').strip('/'))
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                session_id = _optional_text(next(iter(query.get('session_id', [])), None))
                if conversation_id is None or session_id is None:
                    raise ValueError('conversation_id and session_id are required')
                self._send_json(
                    HTTPStatus.OK,
                    self.dashboard_state.clear_studio_conversation(
                        conversation_id=conversation_id,
                        session_id=session_id,
                    ),
                )
            except ValueError as exc:
                self._send_api_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if route.startswith('/api/agent/messages/'):
            request_id = route.removeprefix('/api/agent/messages/').strip('/')
            try:
                self._send_json(HTTPStatus.OK, self.dashboard_state.cancel_studio_request(request_id))
            except ValueError as exc:
                self._send_api_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if route.startswith('/api/audit-rules/'):
            rule_id = route.removeprefix('/api/audit-rules/').strip('/')
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            raw_version = next(iter(query.get('expected_version', [])), None)
            try:
                expected_version = _optional_nonnegative_int(raw_version, 'expected_version')
                if expected_version is None:
                    raise ValueError('expected_version is required')
                self._send_json(
                    HTTPStatus.OK,
                    self.dashboard_state.archive_audit_rule(rule_id, expected_version=expected_version),
                )
            except ValueError as exc:
                self._send_api_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if route.startswith('/api/agent/runs/'):
            run_id = route.removeprefix('/api/agent/runs/')
            try:
                self._send_json(HTTPStatus.ACCEPTED, self.dashboard_state.cancel_agent_run(run_id))
            except ValueError as exc:
                self._send_api_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._send_api_error(HTTPStatus.NOT_FOUND, 'unknown API endpoint')

    def end_headers(self) -> None:
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        if self.path.startswith('/api/'):
            return
        super().log_message(format, *args)

    def _read_json(self) -> dict[str, object]:
        media_type = self.headers.get('Content-Type', '').partition(';')[0].strip().lower()
        if media_type != 'application/json':
            raise ValueError('Content-Type must be application/json')
        content = self._read_body(_MAX_JSON_BYTES)
        try:
            parsed = json.loads(content.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError('request body must be valid JSON') from exc
        if not isinstance(parsed, dict):
            raise ValueError('request body must be a JSON object')
        return cast('dict[str, object]', parsed)

    def _read_body(self, limit: int) -> bytes:
        raw_length = self.headers.get('Content-Length')
        if raw_length is None:
            raise ValueError('Content-Length is required')
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError('invalid Content-Length') from exc
        if length < 0 or length > limit:
            raise ValueError(f'request body exceeds the {limit // (1024 * 1024)} MB limit')
        return self.rfile.read(length)

    def _send_json(self, status: HTTPStatus, value: object) -> None:
        content = json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_live_stream(self) -> None:
        status = self.dashboard_state.status().get('live')
        if not isinstance(status, dict) or not status.get('enabled'):
            self._send_api_error(HTTPStatus.NOT_FOUND, 'live monitoring is not enabled')
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        after_text = self.headers.get('Last-Event-ID') or next(iter(query.get('after', ['0'])), '0')
        try:
            after_revision = max(int(after_text), 0)
        except ValueError:
            after_revision = 0
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        try:
            self.wfile.write(b'retry: 1500\n\n')
            self.wfile.flush()
            while True:
                update = self.dashboard_state.wait_for_live_update(after_revision)
                if update is None:
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
                    continue
                if update.get('closed'):
                    return
                revision = int(update.get('revision') or after_revision)
                payload = json.dumps(update, separators=(',', ':'), ensure_ascii=True)
                message = f'id: {revision}\nevent: trace-update\ndata: {payload}\n\n'.encode()
                self.wfile.write(message)
                self.wfile.flush()
                after_revision = revision
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _send_api_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {'error': message})

    def _client_is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False


def create_dashboard_server(
    *,
    output_dir: Path,
    state: DashboardState,
    port: int,
    attach_live: bool = True,
) -> ThreadingHTTPServer:
    handler = partial(DashboardRequestHandler, state=state, directory=str(output_dir))
    server = ThreadingHTTPServer(('127.0.0.1', port), handler)
    server.daemon_threads = True
    if attach_live:
        state.attach_live_server(dashboard_server_url(server))
    return server


def dashboard_server_url(server: ThreadingHTTPServer) -> str:
    return f'http://127.0.0.1:{server.server_address[1]}/'


def _required_text(payload: dict[str, object], key: str) -> str:
    value = _optional_text(payload.get(key))
    if not value:
        raise ValueError(f'{key} is required')
    return value


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _optional_conversation_id(value: object) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        normalized = str(UUID(text))
    except ValueError as exc:
        raise ValueError('conversation_id must be a UUID') from exc
    if normalized != text.lower():
        raise ValueError('conversation_id must be a canonical UUID')
    return normalized


def _run_authorizes_verified_activation(run: dict[str, object]) -> bool:
    authorization = run.get('source_authorization')
    return isinstance(authorization, dict) and authorization.get('activation_mode') == VERIFIED_RUNTIME_ACTIVATION_MODE


def _public_deployment(deployment: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in deployment.items() if not key.startswith('_')}


def _run_with_pending_deployment(
    run: dict[str, object],
    deployment: dict[str, object],
) -> dict[str, object]:
    response = deepcopy(run)
    response['status'] = 'applying'
    response['message'] = str(deployment.get('message') or 'Activating the verified runtime.')
    response['deployment'] = _public_deployment(deployment)
    return response


def _agent_run_conversation_answer(run: dict[str, object]) -> str | None:
    """Build a bounded chat answer from one durable terminal workflow record."""

    status = _optional_text(run.get('status')) or ''
    if status not in _AGENT_CONVERSATION_TERMINAL_STATUSES:
        return None
    kind = _optional_text(run.get('kind')) or 'agent'
    result_value = run.get('result')
    result = cast('dict[str, object]', result_value) if isinstance(result_value, dict) else {}
    if run.get('kind') in {'repair', 'customize'} and status == 'passed' and result.get('restart_required'):
        return None
    audit_value = run.get('audit')
    audit = cast('dict[str, object]', audit_value) if isinstance(audit_value, dict) else {}
    successful = status in {'investigated', 'extracted', 'summarized', 'audited', 'passed'}
    kind_label = {
        'investigate': 'Session investigation',
        'memories': 'Memory extraction',
        'checkpoints': 'Session brief',
        'audit': 'Parser audit',
        'repair': 'Parser repair',
        'customize': 'Dashboard customization',
    }.get(kind, 'Agent workflow')
    status_label = 'completed' if successful else status.replace('_', ' ')
    sections = [f'### {kind_label} {status_label}']

    if kind == 'audit' and audit:
        _append_conversation_paragraph(sections, audit.get('summary'))
        issues = _conversation_records(audit.get('issues'))
        sections.append(f'**Supported findings:** {len(issues)}')
        issue_lines = []
        for issue in issues[:8]:
            title = _bounded_conversation_text(issue.get('title'), max_chars=300) or 'Parser finding'
            severity = (_bounded_conversation_text(issue.get('severity'), max_chars=20) or 'low').upper()
            evidence = _bounded_conversation_text(issue.get('evidence'), max_chars=700)
            item = f'- **{severity}: {title}**'
            if evidence:
                item += f' {evidence}'
            issue_lines.append(item)
        if issue_lines:
            sections.append('\n'.join(issue_lines))
    elif kind == 'investigate' and result:
        _append_conversation_paragraph(sections, result.get('summary'))
        _append_conversation_field(sections, 'Outcome', result.get('outcome'))
        _append_conversation_list(sections, 'Key findings', result.get('findings'))
        _append_conversation_list(sections, 'Issues', result.get('issues'))
        _append_conversation_list(sections, 'Lessons', result.get('lessons'))
    elif kind == 'memories' and result:
        _append_conversation_paragraph(sections, result.get('summary'))
        candidates = _conversation_records(result.get('candidates'))
        sections.append(f'**Memory candidates:** {len(candidates)}')
        candidate_lines = []
        for candidate in candidates[:8]:
            title = _bounded_conversation_text(candidate.get('title'), max_chars=300) or 'Memory candidate'
            memory = _bounded_conversation_text(candidate.get('memory'), max_chars=700)
            candidate_lines.append(f'- **{title}**{f": {memory}" if memory else ""}')
        if candidate_lines:
            sections.append('\n'.join(candidate_lines))
    elif kind == 'checkpoints' and result:
        _append_conversation_paragraph(sections, result.get('summary'))
        _append_conversation_field(sections, 'Outcome', result.get('outcome'))
        _append_conversation_field(sections, 'Events covered', result.get('event_count'))
        checkpoints = _conversation_records(result.get('checkpoints'))
        sections.append(f'**Checkpoints:** {len(checkpoints)}')
        checkpoint_lines = []
        for checkpoint in checkpoints[:10]:
            title = _bounded_conversation_text(checkpoint.get('title'), max_chars=300) or 'Checkpoint'
            checkpoint_status = _bounded_conversation_text(checkpoint.get('status'), max_chars=40)
            summary = _bounded_conversation_text(checkpoint.get('summary'), max_chars=700)
            label = f'{title} ({checkpoint_status})' if checkpoint_status else title
            checkpoint_lines.append(f'- **{label}**{f": {summary}" if summary else ""}')
        if checkpoint_lines:
            sections.append('\n'.join(checkpoint_lines))
    elif kind in {'repair', 'customize'}:
        _append_conversation_paragraph(sections, run.get('message'))
        verifier_value = result.get('verifier')
        verifier = cast('dict[str, object]', verifier_value) if isinstance(verifier_value, dict) else {}
        _append_conversation_paragraph(sections, verifier.get('summary'))
        fixed_items: object = verifier.get('fixed_items')
        repair_summary_value = run.get('repair_summary')
        if not isinstance(fixed_items, list) and isinstance(repair_summary_value, dict):
            fixed_items = repair_summary_value.get('fixed_items')
        _append_conversation_list(sections, 'Completed items', fixed_items)
        changed_files = _conversation_strings(result.get('changed_files'), max_items=20, max_chars=500)
        if changed_files:
            sections.append('**Changed files**\n' + '\n'.join(f'- `{path.replace("`", "")}`' for path in changed_files))
        deployment_value = run.get('deployment') or result.get('deployment')
        deployment = cast('dict[str, object]', deployment_value) if isinstance(deployment_value, dict) else {}
        refresh_value = run.get('dashboard_refresh') or result.get('dashboard_refresh')
        refresh = cast('dict[str, object]', refresh_value) if isinstance(refresh_value, dict) else {}
        _append_conversation_paragraph(sections, deployment.get('message') or refresh.get('message'))
    else:
        _append_conversation_paragraph(sections, run.get('message'))

    if not successful:
        _append_conversation_paragraph(sections, run.get('message'))
        recovery_value = run.get('recovery')
        recovery = cast('dict[str, object]', recovery_value) if isinstance(recovery_value, dict) else {}
        _append_conversation_paragraph(sections, recovery.get('reason'))
        actions = _conversation_strings(recovery.get('actions'), max_items=6, max_chars=60)
        if actions:
            sections.append('**Available actions:** ' + ', '.join(action.replace('_', ' ') for action in actions))

    answer = '\n\n'.join(section for section in sections if section).strip()
    return answer[:12_000].rstrip()


def _bounded_conversation_text(value: object, *, max_chars: int = 3_000) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    return text if len(text) <= max_chars else f'{text[: max_chars - 3].rstrip()}...'


def _conversation_strings(value: object, *, max_items: int = 8, max_chars: int = 700) -> list[str]:
    if not isinstance(value, list):
        return []
    texts: list[str] = []
    for item in value[:max_items]:
        if isinstance(item, dict):
            item = item.get('item')
        text = _bounded_conversation_text(item, max_chars=max_chars)
        if text:
            texts.append(text)
    return texts


def _conversation_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [cast('dict[str, object]', item) for item in value if isinstance(item, dict)]


def _append_conversation_paragraph(sections: list[str], value: object) -> None:
    text = _bounded_conversation_text(value)
    if text and text not in sections:
        sections.append(text)


def _append_conversation_field(sections: list[str], label: str, value: object) -> None:
    text = _bounded_conversation_text(value, max_chars=120)
    if text:
        sections.append(f'**{label}:** {text.replace("_", " ")}')


def _append_conversation_list(sections: list[str], label: str, value: object) -> None:
    texts = _conversation_strings(value)
    if texts:
        sections.append(f'**{label}**\n' + '\n'.join(f'- {text}' for text in texts))


def _optional_secret(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value


def _boolean(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError('remember must be a boolean')
    return value


def _optional_nonnegative_int(value: object, label: str) -> int | None:
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a non-negative integer')
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label} must be a non-negative integer') from exc
    if parsed < 0 or str(parsed) != str(value).strip():
        raise ValueError(f'{label} must be a non-negative integer')
    return parsed


def _required_positive_int(value: object, label: str) -> int:
    parsed = _optional_nonnegative_int(value, label)
    if parsed is None or parsed < 1:
        raise ValueError(f'{label} must be a positive integer')
    return parsed


def _required_object(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f'{key} is required')
    return cast('dict[str, object]', value)


def _dashboard_run_summaries(agent_status: dict[str, object]) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    observed: set[str] = set()
    for key in ('latest_run', 'latest_historical_run'):
        value = agent_status.get(key)
        if not isinstance(value, dict):
            continue
        run_id = str(value.get('run_id') or '')
        if not run_id or run_id in observed:
            continue
        observed.add(run_id)
        summaries.append(_bounded_agent_run(value))
    return summaries


def _bounded_agent_run(run: dict[str, object]) -> dict[str, object]:
    fields = (
        'run_id',
        'kind',
        'status',
        'message',
        'started_at',
        'updated_at',
        'completed_at',
        'session_id',
        'turn_id',
        'event_sequence',
        'attempt',
        'max_attempts',
        'harness',
        'harness_label',
        'audit_requires_fix',
        'historical',
        'historical_reason',
        'recovery',
    )
    result = {key: deepcopy(run[key]) for key in fields if key in run}
    activity = run.get('activity')
    if isinstance(activity, list):
        result['activity_count'] = len(activity)
        result['activity'] = deepcopy([item for item in activity if isinstance(item, dict)][-80:])
        result['activity_truncated'] = len(activity) > 80
    return result


def _bounded_audit_finding(contract: dict[str, object]) -> dict[str, object]:
    fields = (
        'id',
        'version',
        'title',
        'severity',
        'status',
        'expectation',
        'observation',
        'evaluator',
        'rule_type',
        'updated_at',
        'action',
    )
    result = {key: deepcopy(contract[key]) for key in fields if key in contract}
    evidence = contract.get('evidence')
    if isinstance(evidence, list):
        result['evidence_count'] = len(evidence)
        result['evidence'] = deepcopy([item for item in evidence if isinstance(item, dict)][:8])
        result['evidence_truncated'] = len(evidence) > 8
    return result


def _dashboard_session_metrics(result: AnalysisResult, session_id: str) -> dict[str, object]:
    session = next((item for item in result.sessions if item.session_id == session_id), None)
    trace = next((item for item in result.traces if item.session_id == session_id), None)
    if session is None or trace is None:
        raise ValueError('the selected trace is no longer loaded')
    turns = [turn for turn in result.turns if turn.session_id == session_id]
    statuses = Counter(turn.status for turn in turns)
    tools: Counter[str] = Counter()
    models: Counter[str] = Counter()
    for turn in turns:
        tools.update(dict(turn.tool_breakdown))
        if turn.model:
            models[turn.model] += 1
    return {
        'resource': 'session_metrics',
        'session_id': session_id,
        'turns': len(turns),
        'turn_statuses': dict(statuses),
        'normalized_events': trace.events_total,
        'duration_secs': session.duration_secs,
        'first_turn_started_at': session.first_turn_started_at,
        'last_event_at': session.last_event_at,
        'tokens': {
            'input': session.prompt_tokens,
            'cached_input': session.cached_input_tokens,
            'output': session.completion_tokens,
            'reasoning': session.reasoning_tokens,
            'total': session.total_tokens,
        },
        'tool_calls': session.tool_call_total,
        'unique_tool_names': session.unique_tool_names,
        'top_tools': [{'name': name, 'calls': count} for name, count in tools.most_common(20)],
        'models': [{'name': name, 'turns': count} for name, count in models.most_common(12)],
        'context_compactions': session.context_compacted_total,
        'skipped_lines': session.skipped_lines,
    }


def _qa_dashboard_context(
    payload: dict[str, object],
    *,
    session_id: str,
    turn_id: str | None,
) -> tuple[int | None, dict[str, str]]:
    raw_state = payload.get('dashboard_state')
    if raw_state is None:
        return None, {}
    if not isinstance(raw_state, dict):
        raise ValueError('dashboard_state must be an object')
    state = cast('dict[str, object]', raw_state)
    view_state: dict[str, str] = {}
    source = state.get('source')
    if isinstance(source, dict):
        selected_session = _optional_text(source.get('session_id'))
        if selected_session and selected_session != session_id:
            raise ValueError('dashboard_state source does not match session_id')
    selection = state.get('selection')
    event_sequence: int | None = None
    if isinstance(selection, dict):
        selected_turn = _optional_text(selection.get('turn_id'))
        if selected_turn and selected_turn != turn_id:
            raise ValueError('dashboard_state selection does not match turn_id')
        raw_sequence = selection.get('event_sequence')
        if raw_sequence is not None:
            if isinstance(raw_sequence, bool) or not isinstance(raw_sequence, int) or raw_sequence < 1:
                raise ValueError('dashboard_state event_sequence must be a positive integer')
            event_sequence = raw_sequence
        raw_highlight = selection.get('highlight')
        if raw_highlight is not None:
            if not isinstance(raw_highlight, dict):
                raise ValueError('dashboard_state highlight must be an object')
            highlighted_text = _bounded_dashboard_text(
                raw_highlight.get('text'),
                key='dashboard_state highlighted text',
                max_chars=_MAX_HIGHLIGHT_CHARS,
            )
            highlight_origin = _bounded_dashboard_text(
                raw_highlight.get('origin'),
                key='dashboard_state highlight origin',
                max_chars=80,
            )
            if highlighted_text:
                view_state['highlighted_text'] = highlighted_text
                if highlight_origin:
                    view_state['highlight_origin'] = highlight_origin
        raw_ask_target = selection.get('ask_target')
        if raw_ask_target is not None:
            if not isinstance(raw_ask_target, dict):
                raise ValueError('dashboard_state ask_target must be an object')
            ask_target_kind = _bounded_dashboard_text(
                raw_ask_target.get('kind'),
                key='dashboard_state ask_target kind',
                max_chars=20,
            )
            if ask_target_kind not in {'turn', 'message', 'event'}:
                raise ValueError('dashboard_state ask_target kind must be turn, message, or event')
            ask_target_turn = _bounded_dashboard_text(
                raw_ask_target.get('turn_id'),
                key='dashboard_state ask_target turn_id',
                max_chars=200,
            )
            if not ask_target_turn:
                raise ValueError('dashboard_state ask_target turn_id is required')
            if ask_target_turn != turn_id:
                raise ValueError('dashboard_state ask_target does not match turn_id')
            raw_target_sequence = raw_ask_target.get('event_sequence')
            if raw_target_sequence is not None:
                if (
                    isinstance(raw_target_sequence, bool)
                    or not isinstance(raw_target_sequence, int)
                    or raw_target_sequence < 1
                ):
                    raise ValueError('dashboard_state ask_target event_sequence must be a positive integer')
                if raw_target_sequence != event_sequence:
                    raise ValueError('dashboard_state ask_target does not match event_sequence')
                view_state['ask_target_event_sequence'] = str(raw_target_sequence)
            elif ask_target_kind != 'turn':
                raise ValueError('dashboard_state message or event ask_target requires event_sequence')
            raw_target_line = raw_ask_target.get('line_number')
            if raw_target_line is not None:
                if isinstance(raw_target_line, bool) or not isinstance(raw_target_line, int) or raw_target_line < 1:
                    raise ValueError('dashboard_state ask_target line_number must be a positive integer')
                view_state['ask_target_line_number'] = str(raw_target_line)
            ask_target_fields = (
                ('label', 'ask_target_label', 200),
                ('summary', 'ask_target_summary', 1_000),
                ('text', 'ask_target_text', 2_000),
                ('role', 'ask_target_role', 40),
                ('category', 'ask_target_category', 40),
                ('status', 'ask_target_status', 40),
            )
            view_state['ask_target_kind'] = ask_target_kind
            for payload_key, context_key, max_chars in ask_target_fields:
                value = _bounded_dashboard_text(
                    raw_ask_target.get(payload_key),
                    key=f'dashboard_state ask_target {payload_key}',
                    max_chars=max_chars,
                )
                if value:
                    view_state[context_key] = value
        raw_checkpoint = selection.get('checkpoint')
        if raw_checkpoint is not None:
            if not isinstance(raw_checkpoint, dict):
                raise ValueError('dashboard_state checkpoint must be an object')
            raw_index = raw_checkpoint.get('index')
            if isinstance(raw_index, bool) or not isinstance(raw_index, int) or not 1 <= raw_index <= 1_000:
                raise ValueError('dashboard_state checkpoint index must be a positive integer')
            checkpoint_title = _bounded_dashboard_text(
                raw_checkpoint.get('title'),
                key='dashboard_state checkpoint title',
                max_chars=200,
            )
            checkpoint_status = _bounded_dashboard_text(
                raw_checkpoint.get('status'),
                key='dashboard_state checkpoint status',
                max_chars=40,
            )
            checkpoint_summary = _bounded_dashboard_text(
                raw_checkpoint.get('summary'),
                key='dashboard_state checkpoint summary',
                max_chars=_MAX_CHECKPOINT_SUMMARY_CHARS,
            )
            detail_fields = (
                ('actions', 'checkpoint_actions', 10),
                ('achievements', 'checkpoint_achievements', 10),
                ('blockers', 'checkpoint_blockers', 8),
                ('artifacts', 'checkpoint_artifacts', 10),
                ('next_steps', 'checkpoint_next_steps', 8),
            )
            checkpoint_details: dict[str, list[str]] = {}
            for payload_key, context_key, max_items in detail_fields:
                checkpoint_details[context_key] = _bounded_dashboard_text_list(
                    raw_checkpoint.get(payload_key, []),
                    key=f'dashboard_state checkpoint {payload_key}',
                    max_items=max_items,
                    max_chars=600,
                )
            raw_anchors = raw_checkpoint.get('evidence_anchors', [])
            anchors = _bounded_dashboard_text_list(
                raw_anchors,
                key='dashboard_state checkpoint evidence_anchors',
                max_items=8,
                max_chars=200,
            )
            view_state['checkpoint_index'] = str(raw_index)
            if checkpoint_title:
                view_state['checkpoint_title'] = checkpoint_title
            if checkpoint_status:
                view_state['checkpoint_status'] = checkpoint_status
            if checkpoint_summary:
                view_state['checkpoint_summary'] = checkpoint_summary
            checkpoint_turn_ids = _bounded_dashboard_text_list(
                raw_checkpoint.get('turn_ids', []),
                key='dashboard_state checkpoint turn_ids',
                max_items=12,
                max_chars=200,
            )
            if checkpoint_turn_ids:
                view_state['checkpoint_turn_ids'] = ' | '.join(checkpoint_turn_ids)
            start_sequence = raw_checkpoint.get('start_event_sequence')
            end_sequence = raw_checkpoint.get('end_event_sequence')
            for key, value in (
                ('checkpoint_start_event_sequence', start_sequence),
                ('checkpoint_end_event_sequence', end_sequence),
            ):
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError(f'dashboard_state {key} must be a positive integer')
                view_state[key] = str(value)
            if isinstance(start_sequence, int) and isinstance(end_sequence, int) and start_sequence > end_sequence:
                raise ValueError('dashboard_state checkpoint event range is invalid')
            for context_key, values in checkpoint_details.items():
                if values:
                    view_state[context_key] = ' | '.join(values)
            if anchors:
                view_state['checkpoint_anchors'] = ' | '.join(anchors)
        raw_contract = selection.get('contract')
        if raw_contract is not None:
            if not isinstance(raw_contract, dict):
                raise ValueError('dashboard_state contract must be an object')
            contract_turn = _bounded_dashboard_text(
                raw_contract.get('turn_id'),
                key='dashboard_state contract turn_id',
                max_chars=200,
            )
            if contract_turn and contract_turn != turn_id:
                raise ValueError('dashboard_state contract does not match turn_id')
            contract_fields = (
                ('id', 'contract_id', 120),
                ('version', 'contract_version', 40),
                ('title', 'contract_title', 200),
                ('severity', 'contract_severity', 20),
                ('status', 'contract_status', 20),
                ('expectation', 'contract_expectation', 2_000),
                ('observation', 'contract_observation', 2_000),
            )
            for payload_key, context_key, max_chars in contract_fields:
                value = _bounded_dashboard_text(
                    raw_contract.get(payload_key),
                    key=f'dashboard_state contract {payload_key}',
                    max_chars=max_chars,
                )
                if value:
                    view_state[context_key] = value
            contract_anchors = _bounded_dashboard_text_list(
                raw_contract.get('evidence_anchors', []),
                key='dashboard_state contract evidence_anchors',
                max_items=8,
                max_chars=200,
            )
            if contract_anchors:
                view_state['contract_anchors'] = ' | '.join(contract_anchors)
    view = state.get('view')
    if isinstance(view, dict):
        for key in ('search_query', 'category', 'tool'):
            value = _optional_text(view.get(key))
            if value:
                view_state[key] = value
    return event_sequence, view_state


def _bounded_dashboard_text(value: object, *, key: str, max_chars: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{key} must be text')
    text = ' '.join(value.split())
    if len(text) > max_chars:
        raise ValueError(f'{key} must be {max_chars} characters or fewer')
    return text or None


def _bounded_dashboard_text_list(value: object, *, key: str, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(f'{key} must be a list of at most {max_items} items')
    return [text for item in value if (text := _bounded_dashboard_text(item, key=f'{key} item', max_chars=max_chars))]


def _studio_activity_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')
