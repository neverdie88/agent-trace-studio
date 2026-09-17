"""Guarded, user-triggered actions for deterministic live-audit violations."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_trace_studio.audit_rules import AuditRule

AuditActionStatus = Literal['queued', 'running', 'delivered', 'failed', 'interrupted']
_ACTION_SCHEMA_VERSION = 'agent-trace-studio.audit-actions.v1'
_DELIVERY_TIMEOUT_SECONDS = 180
_MAX_RECEIPTS = 500


class AuditActionReceipt(BaseModel):
    """Persisted metadata for one bounded session-notification attempt."""

    model_config = ConfigDict(extra='forbid', frozen=True)

    schema_version: Literal['agent-trace-studio.audit-actions.v1'] = _ACTION_SCHEMA_VERSION
    id: str = Field(min_length=1, max_length=120)
    dedupe_key: str = Field(min_length=64, max_length=64)
    session_id: str = Field(min_length=1, max_length=200)
    rule_id: str = Field(min_length=1, max_length=64)
    rule_version: int = Field(ge=1)
    trigger: Literal['manual', 'automatic'] = 'manual'
    status: AuditActionStatus
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=500)


class SessionMessageSender(Protocol):
    """Transport boundary for a message to an existing agent session."""

    def send(self, *, session_id: str, message: str) -> None: ...


class CodexSessionMessageSender:
    """Resume one Codex session for a read-only notification turn."""

    def __init__(self, *, timeout_seconds: int = _DELIVERY_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = max(timeout_seconds, 1)

    def send(self, *, session_id: str, message: str) -> None:
        _validate_codex_session_id(session_id)
        executable = shutil.which('codex')
        if executable is None:
            raise RuntimeError('Codex CLI is unavailable')
        command = [
            executable,
            '--ask-for-approval',
            'never',
            'exec',
            '--sandbox',
            'read-only',
            '--skip-git-repo-check',
            'resume',
            session_id,
            '-',
        ]
        try:
            completed = subprocess.run(
                command,
                input=message,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=_safe_subprocess_environment(),
                check=False,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('Codex session notification timed out') from exc
        except OSError as exc:
            raise RuntimeError('Codex session notification could not start') from exc
        if completed.returncode != 0:
            raise RuntimeError('Codex session notification was rejected')


class AuditActionDispatcher:
    """Persist and deliver user-approved live-audit messages without trace text."""

    def __init__(
        self,
        path: Path,
        *,
        sender: SessionMessageSender | None = None,
        changed: Callable[[], None] | None = None,
    ) -> None:
        self.path = path.expanduser().resolve()
        self._sender = sender or CodexSessionMessageSender()
        self._changed = changed
        self._lock = threading.RLock()
        self._receipts = self._load()
        self._mark_interrupted()

    def set_changed_callback(self, changed: Callable[[], None] | None) -> None:
        with self._lock:
            self._changed = changed

    def dispatch(
        self,
        *,
        session_id: str,
        rule: AuditRule,
        contract: Mapping[str, object],
        action_message: str | None = None,
        automatic: bool = False,
    ) -> dict[str, object]:
        """Queue one notification after the caller revalidates the violation."""

        _validate_codex_session_id(session_id)
        if contract.get('status') != 'violated':
            raise ValueError('the audit rule is no longer violated')
        delivery_message = (
            _automatic_action_message(rule=rule, contract=contract, message=action_message)
            if action_message is not None
            else _notification_message(rule=rule, contract=contract)
        )
        dedupe_key = _dedupe_key(session_id=session_id, rule=rule, contract=contract)
        with self._lock:
            existing = next(
                (
                    receipt
                    for receipt in reversed(self._receipts)
                    if receipt.dedupe_key == dedupe_key and receipt.status in {'queued', 'running', 'delivered'}
                ),
                None,
            )
            if existing is not None:
                return existing.model_dump(mode='json')
            now = _timestamp()
            receipt = AuditActionReceipt(
                id=secrets.token_urlsafe(18),
                dedupe_key=dedupe_key,
                session_id=session_id,
                rule_id=rule.id,
                rule_version=rule.version,
                trigger='automatic' if automatic else 'manual',
                status='queued',
                created_at=now,
                updated_at=now,
                message='Session-agent notification queued.',
            )
            self._record_locked(receipt)
        worker = threading.Thread(
            target=self._deliver,
            args=(receipt, delivery_message),
            name=f'agent-trace-audit-action-{receipt.id[:8]}',
            daemon=True,
        )
        worker.start()
        return receipt.model_dump(mode='json')

    def latest(
        self,
        *,
        session_id: str,
        rule_id: str,
        rule_version: int,
        violation_key: str | None = None,
    ) -> dict[str, object] | None:
        with self._lock:
            receipt = next(
                (
                    candidate
                    for candidate in reversed(self._receipts)
                    if candidate.session_id == session_id
                    and candidate.rule_id == rule_id
                    and candidate.rule_version == rule_version
                    and (violation_key is None or candidate.dedupe_key == violation_key)
                ),
                None,
            )
        return receipt.model_dump(mode='json') if receipt is not None else None

    def violation_key(
        self,
        *,
        session_id: str,
        rule: AuditRule,
        contract: Mapping[str, object],
    ) -> str:
        return _dedupe_key(session_id=session_id, rule=rule, contract=contract)

    def _deliver(self, receipt: AuditActionReceipt, message: str) -> None:
        label = 'automatic action' if receipt.trigger == 'automatic' else 'read-only notification'
        self._update(receipt, status='running', message=f'Sending the {label} to the session agent.')
        try:
            self._sender.send(session_id=receipt.session_id, message=message)
        except Exception:
            self._update(receipt, status='failed', message='Session-agent notification failed.')
        else:
            self._update(receipt, status='delivered', message=f'Session-agent {label} delivered.')

    def _update(self, receipt: AuditActionReceipt, *, status: AuditActionStatus, message: str) -> None:
        updated = receipt.model_copy(update={'status': status, 'message': message, 'updated_at': _timestamp()})
        with self._lock:
            self._record_locked(updated)

    def _record_locked(self, receipt: AuditActionReceipt) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(receipt.model_dump(mode='json'), sort_keys=True, separators=(',', ':')) + '\n')
        self._receipts.append(receipt)
        if len(self._receipts) > _MAX_RECEIPTS:
            self._receipts = self._receipts[-_MAX_RECEIPTS:]
        changed = self._changed
        if changed is not None:
            changed()

    def _load(self) -> list[AuditActionReceipt]:
        if not self.path.is_file():
            return []
        receipts: list[AuditActionReceipt] = []
        try:
            lines = self.path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeError):
            return []
        for line in lines[-(_MAX_RECEIPTS * 3) :]:
            try:
                receipts.append(AuditActionReceipt.model_validate_json(line))
            except ValueError:
                continue
        return receipts[-_MAX_RECEIPTS:]

    def _mark_interrupted(self) -> None:
        latest: dict[str, AuditActionReceipt] = {}
        for receipt in self._receipts:
            latest[receipt.id] = receipt
        for receipt in latest.values():
            if receipt.status in {'queued', 'running'}:
                self._update(
                    receipt,
                    status='interrupted',
                    message='Session-agent notification was interrupted by a dashboard restart.',
                )


def _notification_message(*, rule: AuditRule, contract: Mapping[str, object]) -> str:
    anchors: list[str] = []
    evidence = contract.get('evidence')
    if isinstance(evidence, Sequence) and not isinstance(evidence, str | bytes):
        for item in evidence[:8]:
            if not isinstance(item, Mapping):
                continue
            turn_id = str(item.get('turn_id') or '').strip()
            line_number = item.get('line_number')
            if turn_id and isinstance(line_number, int):
                anchors.append(f'[turn {turn_id}, line {line_number}]')
    anchor_text = ', '.join(anchors) or 'No bounded anchor was available.'
    return (
        'Agent Trace Studio live-audit notification. This is host-authored policy metadata, not trace content.\n\n'
        f'Rule {rule.id} version {rule.version} is violated.\n'
        f'Expected behavior: {rule.expectation}\n'
        f'Observation: {contract.get("observation") or "The deterministic rule reported a violation."!s}\n'
        f'Evidence anchors: {anchor_text}\n\n'
        'Review this notification and report the safest next step to the user. '
        'Do not modify files or execute tools in this notification turn.'
    )


def _automatic_action_message(*, rule: AuditRule, contract: Mapping[str, object], message: str | None) -> str:
    normalized = (message or '').strip()
    if not normalized:
        raise ValueError('automatic session action requires a non-empty message')
    anchors = _evidence_anchors(contract)
    return (
        'Agent Trace Studio automatic live-audit action. This action was explicitly configured in a user-approved '
        'rule; trace content did not author it.\n\n'
        f'Rule: {rule.id} version {rule.version}\n'
        f'Evidence anchors: {anchors}\n\n'
        f'Configured message:\n{normalized}\n\n'
        'Handle the configured message in this read-only turn. Do not modify files, deploy, or execute tools.'
    )


def _evidence_anchors(contract: Mapping[str, object]) -> str:
    anchors: list[str] = []
    evidence = contract.get('evidence')
    if isinstance(evidence, Sequence) and not isinstance(evidence, str | bytes):
        for item in evidence[:8]:
            if not isinstance(item, Mapping):
                continue
            turn_id = str(item.get('turn_id') or '').strip()
            line_number = item.get('line_number')
            if turn_id and isinstance(line_number, int):
                anchors.append(f'[turn {turn_id}, line {line_number}]')
    return ', '.join(anchors) or 'No bounded anchor was available.'


def _dedupe_key(*, session_id: str, rule: AuditRule, contract: Mapping[str, object]) -> str:
    evidence = contract.get('evidence')
    anchors: list[tuple[str, int]] = []
    if isinstance(evidence, Sequence) and not isinstance(evidence, str | bytes):
        for item in evidence:
            if isinstance(item, Mapping) and isinstance(item.get('line_number'), int):
                anchors.append((str(item.get('turn_id') or ''), int(item['line_number'])))
    value = json.dumps(
        {
            'session_id': session_id,
            'rule_id': rule.id,
            'rule_version': rule.version,
            'evaluation_fingerprint': str(contract.get('evaluation_fingerprint') or ''),
            'anchors': anchors,
        },
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _validate_codex_session_id(session_id: str) -> None:
    try:
        parsed = UUID(session_id)
    except ValueError as exc:
        raise ValueError('session-agent messaging requires an exact Codex UUID') from exc
    if str(parsed) != session_id.lower():
        raise ValueError('session-agent messaging requires a canonical Codex UUID')


def _safe_subprocess_environment() -> dict[str, str]:
    allowed = ('HOME', 'PATH', 'TMPDIR', 'SHELL', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'WINDIR', 'CODEX_HOME')
    return {name: os.environ[name] for name in allowed if os.environ.get(name)}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')
