"""Versioned audit rules and deterministic execution-trace evaluation."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_trace_studio.models import AnalysisResult, TraceEvent

AuditSeverity = Literal['critical', 'high', 'medium', 'low']
AuditRuleType = Literal['forbid_event', 'require_event', 'require_before', 'require_after']
AuditRuleActor = Literal['manual', 'agent']

_RULE_SCHEMA_VERSION = 'agent-trace-studio.audit-rules.v1'
_DEFAULT_PROPOSAL_TTL_SECONDS = 300.0
_MAX_RULES = 200
_MAX_PROPOSALS = 32


class AuditEventMatcher(BaseModel):
    """A bounded exact/substring matcher over one normalized trace event."""

    model_config = ConfigDict(extra='forbid', frozen=True)

    category: str = Field(default='', max_length=80)
    kind: str = Field(default='', max_length=120)
    role: str = Field(default='', max_length=80)
    phase: str = Field(default='', max_length=120)
    tool_name: str = Field(default='', max_length=200)
    status: str = Field(default='', max_length=80)
    contains: str = Field(default='', max_length=500)

    @field_validator('category', 'kind', 'role', 'phase', 'tool_name', 'status', 'contains', mode='before')
    @classmethod
    def _strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode='after')
    def _require_constraint(self) -> AuditEventMatcher:
        if not any(
            (
                self.category,
                self.kind,
                self.role,
                self.phase,
                self.tool_name,
                self.status,
                self.contains,
            )
        ):
            raise ValueError('an audit event matcher must contain at least one constraint')
        return self


class AuditRuleAction(BaseModel):
    """One bounded action armed by live monitoring for new rule violations."""

    model_config = ConfigDict(extra='forbid', frozen=True)

    type: Literal['send_session_message'] = 'send_session_message'
    message: str = Field(min_length=1, max_length=1_000)

    @field_validator('message', mode='before')
    @classmethod
    def _strip_message(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AuditRuleDraft(BaseModel):
    """Editable deterministic audit rule without persistence metadata."""

    model_config = ConfigDict(extra='forbid')

    id: str = Field(pattern=r'^[a-z][a-z0-9-]{2,63}$')
    title: str = Field(min_length=1, max_length=160)
    expectation: str = Field(min_length=1, max_length=2_000)
    severity: AuditSeverity = 'medium'
    enabled: bool = True
    rule_type: AuditRuleType
    event: AuditEventMatcher
    required_event: AuditEventMatcher | None = None
    same_turn: bool = False
    automatic_action: AuditRuleAction | None = None

    @field_validator('id', 'title', 'expectation', mode='before')
    @classmethod
    def _strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode='after')
    def _validate_relation(self) -> AuditRuleDraft:
        relation = self.rule_type in {'require_before', 'require_after'}
        if relation and self.required_event is None:
            raise ValueError(f'{self.rule_type} requires required_event')
        if not relation and self.required_event is not None:
            raise ValueError(f'{self.rule_type} does not accept required_event')
        if not relation and self.same_turn:
            raise ValueError('same_turn is only valid for before/after rules')
        return self


class AuditRule(AuditRuleDraft):
    """One persisted rule revision."""

    version: int = Field(ge=1)
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    updated_by: AuditRuleActor


class AuditRuleSet(BaseModel):
    """Current authoritative local rule set."""

    model_config = ConfigDict(extra='forbid')

    schema_version: Literal['agent-trace-studio.audit-rules.v1'] = _RULE_SCHEMA_VERSION
    revision: int = Field(default=0, ge=0)
    rules: list[AuditRule] = Field(default_factory=list, max_length=_MAX_RULES)


class AuditRuleProposalContent(BaseModel):
    """Structured content produced by a read-only rule-authoring agent."""

    model_config = ConfigDict(extra='forbid')

    summary: str = Field(min_length=1, max_length=1_000)
    rule: AuditRuleDraft

    @field_validator('summary', mode='before')
    @classmethod
    def _strip_summary(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class _PendingRuleProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str
    token_digest: str
    client_session_nonce: str
    instruction: str
    content: AuditRuleProposalContent
    base_rule_version: int | None
    issued_at: float
    expires_at: float
    harness: str
    model: str
    provider: str


class AuditRuleStore:
    """Thread-safe, atomically persisted audit-rule registry and proposal gate."""

    def __init__(
        self,
        path: Path,
        *,
        proposal_ttl_seconds: float = _DEFAULT_PROPOSAL_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if proposal_ttl_seconds <= 0:
            raise ValueError('audit rule proposal TTL must be positive')
        self.path = path.expanduser().resolve()
        self.history_path = self.path.with_name(f'{self.path.stem}-history.jsonl')
        self._clock = clock
        self._proposal_ttl_seconds = proposal_ttl_seconds
        self._lock = threading.RLock()
        self._history_error: str | None = None
        self._rules = self._load()
        self._proposals: dict[str, _PendingRuleProposal] = {}
        self._consumed_proposals: set[str] = set()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            rules = self._rules.model_copy(deep=True)
            history_error = self._history_error
        snapshot: dict[str, object] = {
            **rules.model_dump(mode='json'),
            'available': True,
            'active_count': sum(rule.enabled for rule in rules.rules),
            'storage': 'local versioned rule store',
        }
        if history_error:
            snapshot['history_error'] = history_error
        return snapshot

    def rules(self) -> tuple[AuditRule, ...]:
        with self._lock:
            return tuple(rule.model_copy(deep=True) for rule in self._rules.rules)

    def upsert(
        self,
        draft: AuditRuleDraft,
        *,
        expected_version: int | None,
        actor: AuditRuleActor = 'manual',
    ) -> AuditRule:
        with self._lock:
            return self._upsert_locked(draft, expected_version=expected_version, actor=actor)

    def archive(self, rule_id: str, *, expected_version: int) -> AuditRule:
        with self._lock:
            existing = self._rule_locked(rule_id)
            if existing is None:
                raise ValueError(f'audit rule not found: {rule_id}')
            draft = AuditRuleDraft.model_validate(
                {
                    **existing.model_dump(
                        mode='json',
                        exclude={'version', 'created_at', 'updated_at', 'updated_by'},
                    ),
                    'enabled': False,
                }
            )
            return self._upsert_locked(
                draft,
                expected_version=expected_version,
                actor='manual',
                action='archive',
            )

    def prepare_agent_proposal(
        self,
        content: AuditRuleProposalContent,
        *,
        instruction: str,
        client_session_nonce: str,
        base_rule_version: int | None,
        harness: str,
        model: str,
        provider: str,
    ) -> dict[str, object]:
        normalized_instruction = instruction.strip()
        if not normalized_instruction:
            raise ValueError('audit rule instruction is required')
        if len(normalized_instruction) > 4_000:
            raise ValueError('audit rule instruction must be 4000 characters or fewer')
        _validate_client_nonce(client_session_nonce)
        with self._lock:
            self._purge_expired_proposals_locked()
            existing = self._rule_locked(content.rule.id)
            if base_rule_version is None and existing is not None:
                raise ValueError('audit rule changed while the agent was drafting; request a new proposal')
            if base_rule_version is not None and (existing is None or existing.version != base_rule_version):
                raise ValueError('audit rule changed while the agent was drafting; request a new proposal')
            if existing is not None and _draft_payload(content.rule) == _draft_payload(existing):
                raise ValueError('the agent proposal does not change the current audit rule')
            if len(self._proposals) >= _MAX_PROPOSALS:
                oldest = min(self._proposals.values(), key=lambda proposal: proposal.issued_at)
                self._proposals.pop(oldest.id, None)
            issued_at = self._clock()
            proposal_id = secrets.token_urlsafe(18)
            token = secrets.token_urlsafe(32)
            proposal = _PendingRuleProposal(
                id=proposal_id,
                token_digest=_token_digest(token),
                client_session_nonce=client_session_nonce,
                instruction=normalized_instruction,
                content=content,
                base_rule_version=base_rule_version,
                issued_at=issued_at,
                expires_at=issued_at + self._proposal_ttl_seconds,
                harness=harness,
                model=model,
                provider=provider,
            )
            self._proposals[proposal.id] = proposal
            return _public_proposal(proposal, token=token)

    def approve_agent_proposal(
        self,
        proposal_id: str,
        *,
        token: str,
        client_session_nonce: str,
    ) -> AuditRule:
        with self._lock:
            proposal = self._proposal_locked(
                proposal_id,
                token=token,
                client_session_nonce=client_session_nonce,
            )
            rule = self._upsert_locked(
                proposal.content.rule,
                expected_version=proposal.base_rule_version,
                actor='agent',
            )
            self._proposals.pop(proposal_id, None)
            self._consumed_proposals.add(proposal_id)
            return rule

    def cancel_agent_proposal(
        self,
        proposal_id: str,
        *,
        token: str,
        client_session_nonce: str,
    ) -> None:
        with self._lock:
            self._proposal_locked(
                proposal_id,
                token=token,
                client_session_nonce=client_session_nonce,
            )
            self._proposals.pop(proposal_id, None)
            self._consumed_proposals.add(proposal_id)

    def _proposal_locked(
        self,
        proposal_id: str,
        *,
        token: str,
        client_session_nonce: str,
    ) -> _PendingRuleProposal:
        self._purge_expired_proposals_locked()
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            if proposal_id in self._consumed_proposals:
                raise ValueError('audit rule proposal was already approved or cancelled')
            raise ValueError('audit rule proposal was not found or expired')
        if not hmac.compare_digest(proposal.token_digest, _token_digest(token)):
            raise ValueError('audit rule proposal token is invalid')
        if not hmac.compare_digest(proposal.client_session_nonce, client_session_nonce):
            raise ValueError('audit rule proposal belongs to a different client session')
        return proposal

    def _purge_expired_proposals_locked(self) -> None:
        now = self._clock()
        expired = [proposal_id for proposal_id, proposal in self._proposals.items() if proposal.expires_at <= now]
        for proposal_id in expired:
            self._proposals.pop(proposal_id, None)

    def _upsert_locked(
        self,
        draft: AuditRuleDraft,
        *,
        expected_version: int | None,
        actor: AuditRuleActor,
        action: Literal['upsert', 'archive'] = 'upsert',
    ) -> AuditRule:
        existing = self._rule_locked(draft.id)
        if existing is None:
            if expected_version not in {None, 0}:
                raise ValueError('audit rule changed before this request was saved')
            if len(self._rules.rules) >= _MAX_RULES:
                raise ValueError(f'audit rule set cannot contain more than {_MAX_RULES} rules')
            version = 1
            created_at = _timestamp(self._clock())
        else:
            if expected_version != existing.version:
                raise ValueError('audit rule changed before this request was saved')
            version = existing.version + 1
            created_at = existing.created_at
        updated_at = _timestamp(self._clock())
        rule = AuditRule(
            **draft.model_dump(mode='python'),
            version=version,
            created_at=created_at,
            updated_at=updated_at,
            updated_by=actor,
        )
        rules = [candidate for candidate in self._rules.rules if candidate.id != rule.id]
        rules.append(rule)
        rules.sort(key=lambda candidate: candidate.id)
        updated = AuditRuleSet(revision=self._rules.revision + 1, rules=rules)
        self._persist_locked(updated, action=action, rule=rule)
        self._rules = updated
        return rule.model_copy(deep=True)

    def _rule_locked(self, rule_id: str) -> AuditRule | None:
        return next((rule for rule in self._rules.rules if rule.id == rule_id), None)

    def _load(self) -> AuditRuleSet:
        if not self.path.is_file():
            return AuditRuleSet()
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            return AuditRuleSet.model_validate(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f'invalid audit rule store {self.path.name}: {exc}') from exc

    def _persist_locked(self, value: AuditRuleSet, *, action: str, rule: AuditRule) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f'.{self.path.name}.{secrets.token_hex(6)}.tmp')
        try:
            temporary.write_text(
                json.dumps(value.model_dump(mode='json'), indent=2, sort_keys=True) + '\n',
                encoding='utf-8',
            )
            temporary.replace(self.path)
            history = {
                'schema_version': _RULE_SCHEMA_VERSION,
                'set_revision': value.revision,
                'recorded_at': rule.updated_at,
                'action': action,
                'rule': rule.model_dump(mode='json'),
            }
            try:
                with self.history_path.open('a', encoding='utf-8', newline='\n') as stream:
                    stream.write(json.dumps(history, sort_keys=True, separators=(',', ':')) + '\n')
            except OSError as exc:
                detail = exc.strerror or type(exc).__name__
                self._history_error = f'Audit rule history {self.history_path.name} could not be updated: {detail}'
            else:
                self._history_error = None
        finally:
            temporary.unlink(missing_ok=True)


def evaluate_audit_rules(
    result: AnalysisResult,
    rules: Iterable[AuditRule],
    *,
    session_id: str,
    session_open: bool = False,
) -> dict[str, object]:
    """Evaluate enabled rules against one normalized session trace."""

    trace = next((candidate for candidate in result.traces if candidate.session_id == session_id), None)
    if trace is None:
        raise ValueError('the selected trace is no longer loaded')
    summary = next((candidate for candidate in result.sessions if candidate.session_id == session_id), None)
    session_complete = bool(summary and summary.turns_total and summary.incomplete_turns == 0 and not session_open)
    enabled_rules = [rule for rule in rules if rule.enabled]
    contracts = [_evaluate_rule(rule, trace.events, session_complete=session_complete) for rule in enabled_rules]
    return {
        'enabled': True,
        'mode': 'rules',
        'title': 'Session audit',
        'summary': (
            f'{len(enabled_rules)} active audit rule{"" if len(enabled_rules) == 1 else "s"} evaluated against '
            f'{trace.events_total} normalized event{"" if trace.events_total == 1 else "s"}.'
        ),
        'session_id': session_id,
        'evaluated_at': _timestamp(time.time()),
        'contracts': contracts,
        'replay_steps': [],
    }


def audit_rule_agent_schema() -> str:
    """Return the authoritative JSON schema shared by external agent harnesses."""

    return json.dumps(
        AuditRuleProposalContent.model_json_schema(),
        ensure_ascii=True,
        separators=(',', ':'),
        sort_keys=True,
    )


def _evaluate_rule(
    rule: AuditRule,
    events: tuple[TraceEvent, ...],
    *,
    session_complete: bool,
) -> dict[str, object]:
    matched = [event for event in events if _matches(rule.event, event)]
    required = [event for event in events if rule.required_event is not None and _matches(rule.required_event, event)]
    evidence: list[dict[str, object]] = []
    violation_sequences: list[int] = []

    if rule.rule_type == 'forbid_event':
        status = 'violated' if matched else 'satisfied'
        observation = (
            f'Found {len(matched)} forbidden matching event{"" if len(matched) == 1 else "s"}.'
            if matched
            else 'No forbidden matching event was observed.'
        )
        evidence = [_event_evidence(event, 'Forbidden event') for event in matched[:8]]
        violation_sequences = [event.sequence for event in matched]
    elif rule.rule_type == 'require_event':
        status = 'satisfied' if matched else ('violated' if session_complete else 'pending')
        observation = (
            f'Found {len(matched)} required matching event{"" if len(matched) == 1 else "s"}.'
            if matched
            else (
                'The session ended without the required event.'
                if session_complete
                else 'The required event has not been observed yet.'
            )
        )
        evidence = [_event_evidence(event, 'Required event') for event in matched[:8]]
        if not evidence and events:
            evidence = [_event_evidence(events[-1], 'Latest observed event')]
        if status == 'violated':
            violation_sequences = [0]
    elif rule.rule_type == 'require_before':
        missing, pairs = _relation_matches(matched, required, before=True, same_turn=rule.same_turn)
        status = 'watching' if not matched else ('violated' if missing else 'satisfied')
        observation = (
            'No triggering event has been observed.'
            if not matched
            else (
                f'{len(missing)} triggering event{"" if len(missing) == 1 else "s"} occurred without the required '
                'earlier event.'
                if missing
                else 'Every triggering event had the required earlier event.'
            )
        )
        evidence = _relation_evidence(pairs, missing, requirement_label='Required earlier event')
        violation_sequences = [event.sequence for event in missing]
    else:
        missing, pairs = _relation_matches(matched, required, before=False, same_turn=rule.same_turn)
        status = (
            'watching'
            if not matched
            else ('violated' if missing and session_complete else 'pending' if missing else 'satisfied')
        )
        observation = (
            'No triggering event has been observed.'
            if not matched
            else (
                f'{len(missing)} triggering event{"" if len(missing) == 1 else "s"} still lack the required later '
                f'event; the session is {"complete" if session_complete else "still open"}.'
                if missing
                else 'Every triggering event had the required later event.'
            )
        )
        evidence = _relation_evidence(pairs, missing, requirement_label='Required later event')
        if status == 'violated':
            violation_sequences = [event.sequence for event in missing]

    return {
        'id': rule.id,
        'version': str(rule.version),
        'title': rule.title,
        'severity': rule.severity,
        'status': status,
        'expectation': rule.expectation,
        'observation': observation,
        'evidence': evidence[:8],
        'evaluator': 'deterministic',
        'rule_type': rule.rule_type,
        'updated_at': rule.updated_at,
        'evaluation_fingerprint': _evaluation_fingerprint(rule, status, violation_sequences),
    }


def _evaluation_fingerprint(rule: AuditRule, status: str, sequences: list[int]) -> str:
    value = json.dumps(
        {'rule_id': rule.id, 'version': rule.version, 'status': status, 'sequences': sequences},
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _relation_matches(
    triggers: list[TraceEvent],
    requirements: list[TraceEvent],
    *,
    before: bool,
    same_turn: bool,
) -> tuple[list[TraceEvent], list[tuple[TraceEvent, TraceEvent]]]:
    missing: list[TraceEvent] = []
    pairs: list[tuple[TraceEvent, TraceEvent]] = []
    for trigger in triggers:
        candidates = [
            requirement
            for requirement in requirements
            if (not same_turn or requirement.turn_id == trigger.turn_id)
            and (requirement.sequence < trigger.sequence if before else requirement.sequence > trigger.sequence)
        ]
        if not candidates:
            missing.append(trigger)
            continue
        requirement = candidates[-1] if before else candidates[0]
        pairs.append((trigger, requirement))
    return missing, pairs


def _relation_evidence(
    pairs: list[tuple[TraceEvent, TraceEvent]],
    missing: list[TraceEvent],
    *,
    requirement_label: str,
) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    for trigger, requirement in pairs[:4]:
        evidence.append(_event_evidence(requirement, requirement_label))
        evidence.append(_event_evidence(trigger, 'Trigger event'))
    for trigger in missing[: max(8 - len(evidence), 0)]:
        evidence.append(_event_evidence(trigger, 'Trigger without requirement'))
    return evidence


def _matches(matcher: AuditEventMatcher, event: TraceEvent) -> bool:
    exact_fields = ('category', 'kind', 'role', 'phase', 'tool_name', 'status')
    for field in exact_fields:
        expected = getattr(matcher, field)
        if not expected:
            continue
        actual = str(getattr(event, field))
        if actual.casefold() != expected.casefold():
            return False
    if matcher.contains:
        haystack = '\n'.join((event.title, event.text, event.input_text, event.output_text))
        return matcher.contains.casefold() in haystack.casefold()
    return True


def _event_evidence(event: TraceEvent, label: str) -> dict[str, object]:
    detail = event.title
    if event.tool_name and event.tool_name not in detail:
        detail = f'{detail} ({event.tool_name})'
    if event.status:
        detail = f'{detail} · {event.status}'
    return {
        'label': label,
        'session_id': event.session_id,
        'turn_id': event.turn_id,
        'event_sequence': event.sequence,
        'line_number': event.line_number,
        'output_line_number': event.output_line_number,
        'event_title': event.title,
        'summary': detail,
    }


def _draft_payload(value: AuditRuleDraft | AuditRule) -> dict[str, object]:
    return value.model_dump(
        mode='json',
        exclude={'version', 'created_at', 'updated_at', 'updated_by'},
    )


def _public_proposal(proposal: _PendingRuleProposal, *, token: str) -> dict[str, object]:
    return {
        'id': proposal.id,
        'token': token,
        'operation': 'update' if proposal.base_rule_version is not None else 'create',
        'instruction': proposal.instruction,
        'summary': proposal.content.summary,
        'rule': proposal.content.rule.model_dump(mode='json'),
        'base_rule_version': proposal.base_rule_version,
        'expires_at': _timestamp(proposal.expires_at),
        'expires_in_seconds': max(int(proposal.expires_at - proposal.issued_at), 1),
        'harness': proposal.harness,
        'model': proposal.model,
        'provider': proposal.provider,
    }


def _validate_client_nonce(value: str) -> None:
    if not 24 <= len(value) <= 160 or not all(character.isalnum() or character in '_-' for character in value):
        raise ValueError('client session nonce is invalid')


def _token_digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat()
