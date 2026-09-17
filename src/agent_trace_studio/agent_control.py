"""Deterministic policy and one-time approval for source-changing actions."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from agent_trace_studio.qa import ControllerAction

SourceMutationAction = Literal['repair_parser', 'customize_dashboard', 'continue_run', 'restart_run', 'activate_run']
ActivationMode = Literal['activate_verified_runtime']
EligibilityBlocker = Literal[
    'unsupported_action',
    'question',
    'hypothetical',
    'negated',
    'reported_speech',
    'trace_reference',
    'missing_parser_scope',
    'missing_command',
]

_SOURCE_MUTATION_ACTIONS = frozenset(
    {'repair_parser', 'customize_dashboard', 'continue_run', 'restart_run', 'activate_run'}
)
_QUESTION_PATTERN = re.compile(
    r'\b(?:should\s+i|what\s+(?:would|should)|how\s+(?:would|could)|'
    r'can\s+(?:it|the\s+agent)|could\s+(?:it|the\s+agent))\b',
    re.IGNORECASE,
)
_HYPOTHETICAL_PATTERN = re.compile(
    r'\b(?:what\s+if|only\s+in\s+theory|hypothetically|suppose|if\s+we\s+(?:were\s+to\s+)?(?:changed?|made|did))\b',
    re.IGNORECASE,
)
_REPORTED_SPEECH_PATTERN = re.compile(
    r'\b(?:'
    r'is\s+what\s+(?:the\s+)?(?:previous|prior)\s+user\s+(?:requested|said)|'
    r'(?:previous|prior)\s+user\s+(?:requested|said)|'
    r'(?:user|assistant|message)\s+(?:said|says|asked|asks|requested|requests))\b',
    re.IGNORECASE,
)
_TRACE_REFERENCE_PATTERN = re.compile(
    r'\b(?:trace|journal|session|event|tool\s+output|selected\s+text|highlighted\s+text)\s+'
    r'(?:said|says|contains|contained|asked|asks|requested|requests|tells|told)\b',
    re.IGNORECASE,
)
_NEITHER_PATTERN = re.compile(r'\bneither\b', re.IGNORECASE)
_PARSER_PATTERN = re.compile(r'\b(?:parser|parsing|journal\s+reader|trace\s+reader)\b', re.IGNORECASE)
_COMMAND_PREFIX = (
    r'^\s*(?:(?:after|once)\s+(?:checking|reviewing|inspecting)\b[^,;]*[,;]\s*)?'
    r'(?:please\s+)?(?:(?:can|could|would)\s+you\s+|'
    r'i\s+(?:want|need)\s+you\s+to\s+|go\s+ahead\s+(?:and\s+)?)?'
)
_REPAIR_COMMAND_PATTERN = re.compile(
    _COMMAND_PREFIX + r'(?:(?:audit|check)\s+(?:and|then)\s+)?(?:fix|repair|patch|correct|resolve)\b',
    re.IGNORECASE,
)
_CUSTOMIZE_COMMAND_PATTERN = re.compile(
    _COMMAND_PREFIX
    + r'(?:customize|change|modify|update|add|remove|redesign|implement|make|move|resize|rename|hide|show|'
    r'turn|set|switch|recolor|restyle)\b',
    re.IGNORECASE,
)
_REPAIR_NEGATION_PATTERN = re.compile(
    r"\b(?:do\s+not|don't|never)\s+(?:fix|repair|patch|correct|resolve)\b[^.?!;]*\b"
    r'(?:parser|parsing|journal\s+reader|trace\s+reader)\b',
    re.IGNORECASE,
)
_CUSTOMIZE_NEGATION_PATTERN = re.compile(
    r"\b(?:do\s+not|don't|never)\s+"
    r'(?:customize|change|modify|update|add|remove|redesign|make|move|resize|rename|hide|show|turn|set|switch)'
    r'\b[^.?!;]*(?:dashboard|theme|ui|interface|layout|panel|pane|button)\b',
    re.IGNORECASE,
)
_GENERAL_SOURCE_NEGATION_PATTERN = re.compile(r"\b(?:neither|do\s+not|don't|never)\b", re.IGNORECASE)
_CLIENT_NONCE_PATTERN = re.compile(r'^[A-Za-z0-9_-]{24,160}$')
_DIGEST_PATTERN = re.compile(r'^[a-f0-9]{64}$')
_DEFAULT_APPROVAL_TTL_SECONDS = 120.0
VERIFIED_RUNTIME_ACTIVATION_MODE: ActivationMode = 'activate_verified_runtime'
VERIFIED_RUNTIME_ACTIVATION_EFFECT = (
    'Apply verified local source changes and activate the updated dashboard after health checks pass. '
    'Keep the current runtime when candidate health checks fail.'
)

_ACKNOWLEDGEMENTS: dict[ControllerAction, str] = {
    'investigate': 'Starting an investigation of the current session.',
    'extract_memories': 'Starting memory extraction for the current session.',
    'summarize_checkpoints': 'Starting a session-wide checkpoint summary.',
    'audit_parser': 'Starting a read-only parser audit.',
    'manage_audit_rules': 'Drafting an audit rule for review. It will not become active without approval.',
    'repair_parser': 'Review and approve the parser source change and verified activation before it starts.',
    'customize_dashboard': 'Review and approve the dashboard source change and verified activation before it starts.',
    'continue_run': 'Review and approve continuing the preserved source-change workflow and activating its result.',
    'restart_run': 'Review and approve starting the source-change workflow over and activating its result.',
    'discard_run': 'Discarding the preserved candidate.',
    'cancel_run': 'Requesting the current workflow to stop.',
    'answer': '',
}


@dataclass(frozen=True)
class RequestEligibility:
    """Whether a model-proposed source action may show an approval card."""

    eligible: bool
    action: str
    reason: str
    blocker: EligibilityBlocker | None = None


@dataclass(frozen=True)
class ApprovedSourceAction:
    """One consumed source-change authorization with immutable request bindings."""

    authorization_id: str
    action: SourceMutationAction
    message: str
    request_hash: str
    selection_hash: str
    dashboard_state_json: str
    source_workspace: str
    baseline_digest: str
    activation_mode: ActivationMode
    client_session_nonce: str
    issued_at: float
    expires_at: float
    audit_run_id: str | None = None
    run_id: str | None = None
    change_digest: str | None = None

    def dashboard_state(self) -> dict[str, object]:
        value = json.loads(self.dashboard_state_json)
        if not isinstance(value, dict):
            raise ValueError('approved dashboard state is invalid')
        return cast('dict[str, object]', value)


@dataclass(frozen=True)
class _PendingSourceAction:
    grant: ApprovedSourceAction
    token_digest: str


class SourceActionAuthorizer:
    """Issue and atomically consume short-lived, request-bound approvals."""

    def __init__(
        self,
        *,
        ttl_seconds: float = _DEFAULT_APPROVAL_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError('approval TTL must be positive')
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._pending: dict[str, _PendingSourceAction] = {}
        self._used: dict[str, float] = {}

    def issue(
        self,
        *,
        action: str,
        message: str,
        dashboard_state: dict[str, object],
        source_workspace: str,
        baseline_digest: str,
        activation_mode: str,
        client_session_nonce: str,
        audit_run_id: str | None = None,
        run_id: str | None = None,
        change_digest: str | None = None,
    ) -> dict[str, object]:
        normalized_action = _validated_source_mutation_action(action)
        normalized_message = normalize_source_request(message)
        normalized_nonce = _validated_client_session_nonce(client_session_nonce)
        normalized_activation_mode = _validated_activation_mode(activation_mode)
        normalized_workspace = source_workspace.strip()
        if not normalized_workspace:
            raise ValueError('source workspace is required for approval')
        if not _DIGEST_PATTERN.fullmatch(baseline_digest):
            raise ValueError('source baseline digest is invalid')
        normalized_change_digest = change_digest.strip() if isinstance(change_digest, str) else None
        if normalized_action == 'activate_run':
            if run_id is None:
                raise ValueError('runtime activation approval requires a run ID')
            if normalized_change_digest is None or not _DIGEST_PATTERN.fullmatch(normalized_change_digest):
                raise ValueError('runtime activation approval requires a valid change digest')
        elif normalized_change_digest is not None:
            raise ValueError('change digest is only supported for runtime activation approval')
        dashboard_state_json = json.dumps(
            dashboard_state,
            ensure_ascii=True,
            separators=(',', ':'),
            sort_keys=True,
        )
        now = self._clock()
        authorization_id = secrets.token_urlsafe(18)
        token = secrets.token_urlsafe(32)
        grant = ApprovedSourceAction(
            authorization_id=authorization_id,
            action=normalized_action,
            message=normalized_message,
            request_hash=_sha256_text(normalized_message),
            selection_hash=_sha256_text(dashboard_state_json),
            dashboard_state_json=dashboard_state_json,
            source_workspace=normalized_workspace,
            baseline_digest=baseline_digest,
            activation_mode=normalized_activation_mode,
            client_session_nonce=normalized_nonce,
            issued_at=now,
            expires_at=now + self._ttl_seconds,
            audit_run_id=audit_run_id,
            run_id=run_id,
            change_digest=normalized_change_digest,
        )
        with self._lock:
            self._prune_locked(now)
            self._pending[authorization_id] = _PendingSourceAction(
                grant=grant,
                token_digest=_sha256_text(token),
            )
        return {
            'id': authorization_id,
            'token': token,
            'action': normalized_action,
            'message': normalized_message,
            'request_hash': grant.request_hash,
            'selection_hash': grant.selection_hash,
            'source_workspace': normalized_workspace,
            'baseline_digest': baseline_digest,
            'activation_mode': normalized_activation_mode,
            'activation_effect': VERIFIED_RUNTIME_ACTIVATION_EFFECT,
            'audit_run_id': audit_run_id,
            'run_id': run_id,
            'change_digest': normalized_change_digest,
            'expires_at': datetime.fromtimestamp(grant.expires_at, tz=UTC).isoformat(),
            'expires_in_seconds': int(self._ttl_seconds),
        }

    def consume(self, *, authorization_id: str, token: str, client_session_nonce: str) -> ApprovedSourceAction:
        normalized_nonce = _validated_client_session_nonce(client_session_nonce)
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            if authorization_id in self._used:
                raise ValueError('source action authorization was already used or cancelled')
            pending = self._pending.get(authorization_id)
            if pending is None:
                raise ValueError('source action authorization was not found')
            if now >= pending.grant.expires_at:
                self._pending.pop(authorization_id, None)
                self._used[authorization_id] = now
                raise ValueError('source action authorization expired')
            if not hmac.compare_digest(pending.token_digest, _sha256_text(token)):
                raise ValueError('source action authorization token is invalid')
            if not hmac.compare_digest(pending.grant.client_session_nonce, normalized_nonce):
                raise ValueError('source action authorization belongs to a different client session')
            self._pending.pop(authorization_id)
            self._used[authorization_id] = now
            return pending.grant

    def cancel(self, *, authorization_id: str, token: str, client_session_nonce: str) -> None:
        self.consume(
            authorization_id=authorization_id,
            token=token,
            client_session_nonce=client_session_nonce,
        )

    def _prune_locked(self, now: float) -> None:
        for authorization_id, pending in tuple(self._pending.items()):
            if now >= pending.grant.expires_at:
                self._pending.pop(authorization_id, None)
                self._used[authorization_id] = now
        used_cutoff = now - max(self._ttl_seconds * 2, 300.0)
        self._used = {key: used_at for key, used_at in self._used.items() if used_at >= used_cutoff}


def classify_source_action_request(
    message: str,
    action: str,
    *,
    actionable_audit: bool = False,
) -> RequestEligibility:
    """Suppress recognized non-requests; never authorize a write by itself."""

    normalized = normalize_source_request(message)
    if action not in _SOURCE_MUTATION_ACTIONS:
        return RequestEligibility(False, action, 'unknown source mutation action', 'unsupported_action')
    if _QUESTION_PATTERN.search(normalized):
        return RequestEligibility(False, action, 'recognized direct question about an action', 'question')
    if _HYPOTHETICAL_PATTERN.search(normalized):
        return RequestEligibility(False, action, 'recognized hypothetical source-change wording', 'hypothetical')
    if _REPORTED_SPEECH_PATTERN.search(normalized):
        return RequestEligibility(
            False,
            action,
            'recognized reported speech rather than a current request',
            'reported_speech',
        )
    if _TRACE_REFERENCE_PATTERN.search(normalized):
        return RequestEligibility(
            False,
            action,
            'recognized trace content reference rather than a current request',
            'trace_reference',
        )
    if _NEITHER_PATTERN.search(normalized):
        return RequestEligibility(False, action, 'recognized explicit neither/negated source-change wording', 'negated')
    if action == 'repair_parser':
        if _REPAIR_NEGATION_PATTERN.search(normalized):
            return RequestEligibility(False, action, 'recognized parser-repair negation', 'negated')
        if not _REPAIR_COMMAND_PATTERN.search(normalized):
            return RequestEligibility(False, action, 'parser repair requires repair wording', 'missing_command')
        if not (_PARSER_PATTERN.search(normalized) or actionable_audit):
            return RequestEligibility(
                False,
                action,
                'parser repair requires parser wording or an exact matching actionable audit',
                'missing_parser_scope',
            )
        return RequestEligibility(True, action, 'recognized current parser repair request')
    if action == 'customize_dashboard':
        if _CUSTOMIZE_NEGATION_PATTERN.search(normalized) or _GENERAL_SOURCE_NEGATION_PATTERN.search(normalized):
            return RequestEligibility(False, action, 'recognized dashboard customization negation', 'negated')
        return RequestEligibility(True, action, 'model-proposed customization is eligible for explicit approval')
    if action in {'continue_run', 'restart_run', 'activate_run'}:
        return RequestEligibility(True, action, 'run control requires explicit approval')
    return RequestEligibility(False, action, 'unsupported source mutation action', 'unsupported_action')


def source_change_authorized(
    message: str,
    action: str,
    *,
    actionable_audit: bool = False,
) -> bool:
    """Compatibility wrapper; true means approval-card eligible, not authorized."""

    return classify_source_action_request(
        message,
        action,
        actionable_audit=actionable_audit,
    ).eligible


def action_acknowledgement(action: ControllerAction) -> str:
    """Return host-authored text that never overstates workflow completion."""

    return _ACKNOWLEDGEMENTS[action]


def normalize_source_request(message: str) -> str:
    normalized = ' '.join(message.split())
    if not normalized:
        raise ValueError('source-change instruction is required')
    if len(normalized) > 4_000:
        raise ValueError('source-change instruction must be 4000 characters or fewer')
    return normalized


def _validated_source_mutation_action(action: str) -> SourceMutationAction:
    if action not in _SOURCE_MUTATION_ACTIONS:
        raise ValueError('unknown or non-mutating source action')
    return cast('SourceMutationAction', action)


def _validated_activation_mode(value: str) -> ActivationMode:
    if value != VERIFIED_RUNTIME_ACTIVATION_MODE:
        raise ValueError('unknown or unsupported source activation mode')
    return VERIFIED_RUNTIME_ACTIVATION_MODE


def _validated_client_session_nonce(value: str) -> str:
    normalized = value.strip()
    if not _CLIENT_NONCE_PATTERN.fullmatch(normalized):
        raise ValueError('client session nonce is invalid')
    return normalized


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()
