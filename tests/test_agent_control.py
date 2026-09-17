from __future__ import annotations

import unittest

from agent_trace_studio.agent_control import (
    VERIFIED_RUNTIME_ACTIVATION_EFFECT,
    VERIFIED_RUNTIME_ACTIVATION_MODE,
    SourceActionAuthorizer,
    classify_source_action_request,
    source_change_authorized,
)


class AgentControlPolicyTest(unittest.TestCase):
    def test_requires_an_explicit_current_parser_repair_instruction(self) -> None:
        self.assertTrue(classify_source_action_request('Fix this parser.', 'repair_parser').eligible)
        self.assertTrue(classify_source_action_request('Can you fix this parser?', 'repair_parser').eligible)
        self.assertTrue(classify_source_action_request('Please repair the duration parsing.', 'repair_parser').eligible)
        self.assertEqual(
            classify_source_action_request('What would fixing this parser do?', 'repair_parser').blocker,
            'question',
        )
        self.assertEqual(
            classify_source_action_request(
                'What happened when the user said "fix this parser"?',
                'repair_parser',
            ).blocker,
            'reported_speech',
        )
        self.assertEqual(classify_source_action_request('Do not fix this parser.', 'repair_parser').blocker, 'negated')
        self.assertEqual(
            classify_source_action_request('Fix this issue.', 'repair_parser').blocker,
            'missing_parser_scope',
        )
        self.assertTrue(
            classify_source_action_request(
                'Fix this issue.',
                'repair_parser',
                actionable_audit=True,
            ).eligible
        )
        self.assertEqual(
            classify_source_action_request('Fix this parser only in theory.', 'repair_parser').blocker,
            'hypothetical',
        )
        self.assertEqual(
            classify_source_action_request('Fix neither the parser nor the dashboard.', 'repair_parser').blocker,
            'negated',
        )
        self.assertEqual(
            classify_source_action_request(
                'Fix this parser is what the previous user requested.',
                'repair_parser',
            ).blocker,
            'reported_speech',
        )
        self.assertTrue(
            classify_source_action_request('Fix the parser, but do not change the dashboard.', 'repair_parser').eligible
        )
        self.assertTrue(
            classify_source_action_request(
                'After checking the evidence, please fix the parser.',
                'repair_parser',
            ).eligible
        )

    def test_requires_an_explicit_dashboard_customization_instruction(self) -> None:
        for message in (
            'Make the turns panel wider.',
            'Remove the tool summary.',
            'turn the theme into red',
            'turn this into blue scheme',
            'recolor this blue',
            'Can you set the dashboard theme to red?',
            'make it blue',
            'make this denser',
            'use a calmer theme',
            'match the selected panel styling',
            'Update the parser documentation.',
        ):
            with self.subTest(message=message):
                self.assertTrue(classify_source_action_request(message, 'customize_dashboard').eligible)
        self.assertEqual(
            classify_source_action_request('How would you make the panel wider?', 'customize_dashboard').blocker,
            'question',
        )
        self.assertEqual(
            classify_source_action_request('How could you turn the theme red?', 'customize_dashboard').blocker,
            'question',
        )
        self.assertEqual(
            classify_source_action_request('What if we made it blue?', 'customize_dashboard').blocker,
            'hypothetical',
        )
        self.assertEqual(
            classify_source_action_request('Can the agent make it blue?', 'customize_dashboard').blocker,
            'question',
        )
        self.assertEqual(
            classify_source_action_request('Do not change the dashboard.', 'customize_dashboard').blocker,
            'negated',
        )
        self.assertEqual(
            classify_source_action_request('The previous user said make it blue.', 'customize_dashboard').blocker,
            'reported_speech',
        )
        self.assertEqual(
            classify_source_action_request('The trace says make it blue.', 'customize_dashboard').blocker,
            'trace_reference',
        )

    def test_unknown_source_actions_fail_closed(self) -> None:
        decision = classify_source_action_request('Fix this parser.', 'unknown_write_action')
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.blocker, 'unsupported_action')
        self.assertFalse(source_change_authorized('Fix this parser.', 'unknown_write_action'))

    def test_one_time_authorization_is_bound_and_consumed(self) -> None:
        nonce = 'client-session-nonce-000001'
        grant = SourceActionAuthorizer(ttl_seconds=60).issue(
            action='repair_parser',
            message='Fix this parser.',
            dashboard_state={'source': {'session_id': 'session-1'}, 'selection': {'turn_id': 'turn-1'}},
            source_workspace='/workspace/agent-trace-studio',
            baseline_digest='a' * 64,
            activation_mode=VERIFIED_RUNTIME_ACTIVATION_MODE,
            client_session_nonce=nonce,
            audit_run_id='auditrun1',
        )

        self.assertNotIn('token_digest', grant)
        self.assertEqual(grant['action'], 'repair_parser')
        self.assertEqual(grant['audit_run_id'], 'auditrun1')
        self.assertEqual(grant['activation_mode'], VERIFIED_RUNTIME_ACTIVATION_MODE)
        self.assertEqual(grant['activation_effect'], VERIFIED_RUNTIME_ACTIVATION_EFFECT)

        authorizer = SourceActionAuthorizer(ttl_seconds=60)
        grant = authorizer.issue(
            action='repair_parser',
            message='Fix this parser.',
            dashboard_state={'source': {'session_id': 'session-1'}, 'selection': {'turn_id': 'turn-1'}},
            source_workspace='/workspace/agent-trace-studio',
            baseline_digest='a' * 64,
            activation_mode=VERIFIED_RUNTIME_ACTIVATION_MODE,
            client_session_nonce=nonce,
        )
        consumed = authorizer.consume(
            authorization_id=str(grant['id']),
            token=str(grant['token']),
            client_session_nonce=nonce,
        )
        self.assertEqual(consumed.request_hash, grant['request_hash'])
        self.assertEqual(consumed.selection_hash, grant['selection_hash'])
        self.assertEqual(consumed.activation_mode, VERIFIED_RUNTIME_ACTIVATION_MODE)

        with self.assertRaisesRegex(ValueError, 'already used'):
            authorizer.consume(
                authorization_id=str(grant['id']),
                token=str(grant['token']),
                client_session_nonce=nonce,
            )

    def test_authorization_rejects_wrong_token_nonce_expiry_and_unknown_action(self) -> None:
        clock_value = 1000.0

        def clock() -> float:
            return clock_value

        authorizer = SourceActionAuthorizer(ttl_seconds=10, clock=clock)
        with self.assertRaisesRegex(ValueError, 'unknown'):
            authorizer.issue(
                action='future_mutation',
                message='Change local source.',
                dashboard_state={'source': {'session_id': 'session-1'}},
                source_workspace='/workspace/agent-trace-studio',
                baseline_digest='a' * 64,
                activation_mode=VERIFIED_RUNTIME_ACTIVATION_MODE,
                client_session_nonce='client-session-nonce-000001',
            )

        with self.assertRaisesRegex(ValueError, 'activation mode'):
            authorizer.issue(
                action='customize_dashboard',
                message='Turn the dashboard theme red.',
                dashboard_state={'source': {'session_id': 'session-1'}},
                source_workspace='/workspace/agent-trace-studio',
                baseline_digest='b' * 64,
                activation_mode='restart_without_verification',
                client_session_nonce='client-session-nonce-000001',
            )

        grant = authorizer.issue(
            action='customize_dashboard',
            message='Turn the dashboard theme red.',
            dashboard_state={'source': {'session_id': 'session-1'}},
            source_workspace='/workspace/agent-trace-studio',
            baseline_digest='b' * 64,
            activation_mode=VERIFIED_RUNTIME_ACTIVATION_MODE,
            client_session_nonce='client-session-nonce-000001',
        )
        with self.assertRaisesRegex(ValueError, 'token'):
            authorizer.consume(
                authorization_id=str(grant['id']),
                token='wrong-token',
                client_session_nonce='client-session-nonce-000001',
            )
        with self.assertRaisesRegex(ValueError, 'different client session'):
            authorizer.consume(
                authorization_id=str(grant['id']),
                token=str(grant['token']),
                client_session_nonce='client-session-nonce-000002',
            )

        clock_value = 1011.0
        with self.assertRaisesRegex(ValueError, 'not found|expired|already used'):
            authorizer.consume(
                authorization_id=str(grant['id']),
                token=str(grant['token']),
                client_session_nonce='client-session-nonce-000001',
            )

    def test_runtime_activation_approval_is_bound_to_run_and_change_digest(self) -> None:
        authorizer = SourceActionAuthorizer(ttl_seconds=60)
        nonce = 'client-session-nonce-000001'
        grant = authorizer.issue(
            action='activate_run',
            message='Activate this exact verified source revision.',
            dashboard_state={'source': {'session_id': 'session-1'}},
            source_workspace='/workspace/agent-trace-studio',
            baseline_digest='a' * 64,
            activation_mode=VERIFIED_RUNTIME_ACTIVATION_MODE,
            client_session_nonce=nonce,
            run_id='repairrun1',
            change_digest='b' * 64,
        )

        consumed = authorizer.consume(
            authorization_id=str(grant['id']),
            token=str(grant['token']),
            client_session_nonce=nonce,
        )

        self.assertEqual(consumed.run_id, 'repairrun1')
        self.assertEqual(consumed.change_digest, 'b' * 64)
        with self.assertRaisesRegex(ValueError, 'already used'):
            authorizer.consume(
                authorization_id=str(grant['id']),
                token=str(grant['token']),
                client_session_nonce=nonce,
            )
        with self.assertRaisesRegex(ValueError, 'change digest'):
            authorizer.issue(
                action='activate_run',
                message='Activate verified source.',
                dashboard_state={'source': {'session_id': 'session-1'}},
                source_workspace='/workspace/agent-trace-studio',
                baseline_digest='a' * 64,
                activation_mode=VERIFIED_RUNTIME_ACTIVATION_MODE,
                client_session_nonce=nonce,
                run_id='repairrun2',
            )


if __name__ == '__main__':
    unittest.main()
