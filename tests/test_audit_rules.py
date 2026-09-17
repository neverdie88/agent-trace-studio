from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_trace_studio.assurance_demo import DEMO_SESSION_ID, write_assurance_demo_journal
from agent_trace_studio.audit_rules import (
    AuditRuleDraft,
    AuditRuleProposalContent,
    AuditRuleStore,
    audit_rule_agent_schema,
    evaluate_audit_rules,
)
from agent_trace_studio.parser import analyze_journals


def _rule(**changes: object) -> AuditRuleDraft:
    payload: dict[str, object] = {
        'id': 'approval-before-production',
        'title': 'Require approval before production',
        'expectation': 'A recorded approval must precede production deployment.',
        'severity': 'critical',
        'enabled': True,
        'rule_type': 'require_before',
        'event': {'tool_name': 'deploy_release'},
        'required_event': {'tool_name': 'record_approval'},
        'same_turn': True,
    }
    payload.update(changes)
    return AuditRuleDraft.model_validate(payload)


class AuditRuleStoreTest(unittest.TestCase):
    def test_rule_schema_rejects_relation_fields_on_single_event_rules(self) -> None:
        with self.assertRaisesRegex(ValueError, 'does not accept required_event'):
            _rule(rule_type='forbid_event')
        with self.assertRaisesRegex(ValueError, 'same_turn is only valid'):
            _rule(rule_type='require_event', required_event=None)

        schema = json.loads(audit_rule_agent_schema())
        self.assertIn('$defs', schema)
        self.assertIn('rule', schema['properties'])
        self.assertIn('automatic_action', schema['$defs']['AuditRuleDraft']['properties'])

    def test_rule_accepts_only_bounded_session_message_actions(self) -> None:
        rule = _rule(automatic_action={'type': 'send_session_message', 'message': '  Approval recorded.  '})
        self.assertEqual(rule.automatic_action.message, 'Approval recorded.')
        with self.assertRaises(ValueError):
            _rule(automatic_action={'type': 'run_command', 'message': 'unsafe'})
        with self.assertRaises(ValueError):
            _rule(automatic_action={'type': 'send_session_message', 'message': ' '})

    def test_persists_versioned_rules_and_rejects_stale_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audit-rules.json'
            store = AuditRuleStore(path)
            first = store.upsert(_rule(), expected_version=None)
            second = store.upsert(
                _rule(expectation='Human approval must precede every production deployment.'),
                expected_version=first.version,
            )
            with self.assertRaisesRegex(ValueError, 'changed before'):
                store.upsert(_rule(title='Stale title'), expected_version=first.version)
            reloaded = AuditRuleStore(path)
            history = [json.loads(line) for line in reloaded.history_path.read_text(encoding='utf-8').splitlines()]

        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)
        self.assertEqual(reloaded.snapshot()['revision'], 2)
        self.assertEqual(reloaded.rules()[0].expectation, 'Human approval must precede every production deployment.')
        self.assertEqual([entry['rule']['version'] for entry in history], [1, 2])

    def test_agent_proposal_requires_one_time_bound_approval(self) -> None:
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            store = AuditRuleStore(
                Path(directory) / 'audit-rules.json',
                proposal_ttl_seconds=30,
                clock=lambda: now[0],
            )
            content = AuditRuleProposalContent(summary='Add the production approval rule.', rule=_rule())
            proposal = store.prepare_agent_proposal(
                content,
                instruction='Create a rule requiring approval before production.',
                client_session_nonce='client-session-nonce-000001',
                base_rule_version=None,
                harness='Test agent',
                model='test-model',
                provider='test-provider',
            )
            with self.assertRaisesRegex(ValueError, 'different client session'):
                store.approve_agent_proposal(
                    str(proposal['id']),
                    token=str(proposal['token']),
                    client_session_nonce='client-session-nonce-000002',
                )
            approved = store.approve_agent_proposal(
                str(proposal['id']),
                token=str(proposal['token']),
                client_session_nonce='client-session-nonce-000001',
            )
            with self.assertRaisesRegex(ValueError, 'already approved or cancelled'):
                store.approve_agent_proposal(
                    str(proposal['id']),
                    token=str(proposal['token']),
                    client_session_nonce='client-session-nonce-000001',
                )

        self.assertEqual(approved.updated_by, 'agent')
        self.assertEqual(approved.version, 1)

    def test_agent_proposal_expires_and_cannot_overwrite_a_newer_rule(self) -> None:
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            store = AuditRuleStore(
                Path(directory) / 'audit-rules.json',
                proposal_ttl_seconds=30,
                clock=lambda: now[0],
            )
            existing = store.upsert(_rule(), expected_version=None)
            proposal = store.prepare_agent_proposal(
                AuditRuleProposalContent(summary='Revise the title.', rule=_rule(title='Revised approval rule')),
                instruction='Update the approval rule title.',
                client_session_nonce='client-session-nonce-000001',
                base_rule_version=existing.version,
                harness='Test agent',
                model='test-model',
                provider='test-provider',
            )
            store.upsert(_rule(expectation='A newer manual revision.'), expected_version=existing.version)
            with self.assertRaisesRegex(ValueError, 'changed before'):
                store.approve_agent_proposal(
                    str(proposal['id']),
                    token=str(proposal['token']),
                    client_session_nonce='client-session-nonce-000001',
                )
            expiring = store.prepare_agent_proposal(
                AuditRuleProposalContent(summary='Add another rule.', rule=_rule(id='second-production-rule')),
                instruction='Add another production rule.',
                client_session_nonce='client-session-nonce-000001',
                base_rule_version=None,
                harness='Test agent',
                model='test-model',
                provider='test-provider',
            )
            now[0] = 131.0
            with self.assertRaisesRegex(ValueError, 'not found or expired'):
                store.approve_agent_proposal(
                    str(expiring['id']),
                    token=str(expiring['token']),
                    client_session_nonce='client-session-nonce-000001',
                )

    def test_agent_proposal_is_bound_to_the_version_visible_before_drafting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AuditRuleStore(Path(directory) / 'audit-rules.json')
            visible = store.upsert(_rule(), expected_version=None)
            store.upsert(_rule(expectation='A newer manual revision.'), expected_version=visible.version)

            with self.assertRaisesRegex(ValueError, 'changed while the agent was drafting'):
                store.prepare_agent_proposal(
                    AuditRuleProposalContent(summary='Revise the title.', rule=_rule(title='Agent revision')),
                    instruction='Update the approval rule title.',
                    client_session_nonce='client-session-nonce-000001',
                    base_rule_version=visible.version,
                    harness='Test agent',
                    model='test-model',
                    provider='test-provider',
                )

    def test_archive_creates_a_disabled_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AuditRuleStore(Path(directory) / 'audit-rules.json')
            first = store.upsert(_rule(), expected_version=None)
            archived = store.archive(first.id, expected_version=first.version)
            history = [json.loads(line) for line in store.history_path.read_text(encoding='utf-8').splitlines()]

        self.assertFalse(archived.enabled)
        self.assertEqual(archived.version, 2)
        self.assertEqual(store.snapshot()['active_count'], 0)
        self.assertEqual(history[-1]['action'], 'archive')

    def test_history_failure_does_not_split_memory_from_authoritative_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audit-rules.json'
            store = AuditRuleStore(path)
            store.history_path.mkdir()

            saved = store.upsert(_rule(), expected_version=None)
            snapshot = store.snapshot()
            reloaded = AuditRuleStore(path)

        self.assertEqual(saved.version, 1)
        self.assertEqual(snapshot['revision'], 1)
        self.assertIn('history_error', snapshot)
        self.assertNotIn(directory, str(snapshot['history_error']))
        self.assertEqual(reloaded.rules()[0].id, saved.id)


class AuditRuleEvaluationTest(unittest.TestCase):
    def test_evaluates_relations_and_forbidden_events_with_trace_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = write_assurance_demo_journal(root / 'release.jsonl')
            result = analyze_journals([journal], include_trace=True)
            store = AuditRuleStore(root / 'audit-rules.json')
            rules = [
                _rule(),
                _rule(
                    id='validate-after-configuration',
                    title='Validate configuration changes',
                    expectation='Validation must follow configuration changes.',
                    severity='high',
                    rule_type='require_after',
                    event={'tool_name': 'write_configuration'},
                    required_event={'tool_name': 'run_validation', 'status': 'completed'},
                ),
                _rule(
                    id='forbid-production-deploy',
                    title='Forbid production deployment',
                    expectation='No production deployment tool may run.',
                    rule_type='forbid_event',
                    event={'tool_name': 'deploy_release'},
                    required_event=None,
                    same_turn=False,
                ),
            ]
            for draft in rules:
                store.upsert(draft, expected_version=None)
            assurance = evaluate_audit_rules(result, store.rules(), session_id=DEMO_SESSION_ID)

        contracts = {contract['id']: contract for contract in assurance['contracts']}
        self.assertEqual(contracts['approval-before-production']['status'], 'violated')
        self.assertEqual(contracts['validate-after-configuration']['status'], 'satisfied')
        self.assertEqual(contracts['forbid-production-deploy']['status'], 'violated')
        self.assertEqual(contracts['approval-before-production']['evidence'][0]['event_title'], 'Tool: deploy_release')

    def test_missing_later_obligation_is_pending_while_session_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = analyze_journals([write_assurance_demo_journal(root / 'release.jsonl')], include_trace=True)
            store = AuditRuleStore(root / 'audit-rules.json')
            store.upsert(
                _rule(
                    id='handoff-after-deploy',
                    title='Record a safe handoff',
                    expectation='A handoff must follow deployment attempts.',
                    rule_type='require_after',
                    event={'tool_name': 'deploy_release'},
                    required_event={'tool_name': 'record_handoff'},
                ),
                expected_version=None,
            )
            open_result = evaluate_audit_rules(
                result,
                store.rules(),
                session_id=DEMO_SESSION_ID,
                session_open=True,
            )
            closed_result = evaluate_audit_rules(result, store.rules(), session_id=DEMO_SESSION_ID)

        self.assertEqual(open_result['contracts'][0]['status'], 'pending')
        self.assertEqual(closed_result['contracts'][0]['status'], 'violated')


if __name__ == '__main__':
    unittest.main()
