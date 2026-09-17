from __future__ import annotations

import json
import unittest

from agent_trace_studio.activity import normalize_activity_details


def call(**updates: object) -> dict[str, object]:
    return {
        'kind': 'context_action',
        'id': 'synthetic-call-1',
        'tool': 'read_dashboard_resource',
        'status': 'completed',
        'arguments': {'resource': 'audit_rules', 'limit': 20},
        **updates,
    }


class ActivityDetailsTests(unittest.TestCase):
    def test_keeps_allowed_metadata_and_marks_preview_truncation(self) -> None:
        result = normalize_activity_details(
            call(output='a' * 6_000, output_chars=6_000, duration_ms=12.6, unknown_field='PRIVATE')
        )
        assert result is not None
        self.assertEqual(result['arguments'], {'resource': 'audit_rules', 'limit': 20})
        self.assertEqual(result['output_chars'], 6_000)
        self.assertEqual(len(result['output']), 4_000)
        self.assertTrue(result['preview_truncated'])
        self.assertEqual(result['duration_ms'], 13)
        self.assertNotIn('unknown_field', result)
        self.assertEqual(normalize_activity_details(result), result)

    def test_removes_private_fields_raw_records_and_local_paths(self) -> None:
        output = 'DASHBOARD RESOURCE: audit_rules\nclassification: bounded state\n' + json.dumps(
            {
                'title': 'Require validation',
                'api_key': 'SYNTHETIC_API_KEY',
                'encrypted_reasoning': 'SYNTHETIC_ENCRYPTED',
                'nested': {'password': 'SYNTHETIC_PASSWORD'},
                'source': '/custom/data/synthetic.jsonl',
                'records': [
                    {'type': 'response_item', 'payload': {'content': 'SYNTHETIC_RAW_ROW'}},
                    {'type': {'unknown': 'shape'}, 'value': 'supported unknown shape'},
                ],
            }
        )
        result = normalize_activity_details(
            call(output=output, arguments={'resource': 'audit_rules', 'secret': 'DROP'})
        )
        assert result is not None
        serialized = json.dumps(result)
        for secret in (
            'SYNTHETIC_API_KEY',
            'SYNTHETIC_ENCRYPTED',
            'SYNTHETIC_PASSWORD',
            'SYNTHETIC_RAW_ROW',
            '/custom/',
        ):
            self.assertNotIn(secret, serialized)
        self.assertIn('Require validation', serialized)
        self.assertIn('raw journal record omitted', serialized)
        self.assertNotIn('secret', result['arguments'])
        self.assertTrue(result['redacted'])

    def test_redacts_text_credentials_before_truncation(self) -> None:
        output = (
            'password=PRIVATE_PASSWORD\nAuthorization: Bearer PRIVATE_BEARER\n'
            'sk-synthetic_1234567890 CURRENT_PROVIDER_KEY /tmp/private/session.json\n' + 'z' * 5_000
        )
        result = normalize_activity_details(call(output=output), private_values=('CURRENT_PROVIDER_KEY',))
        assert result is not None
        for private in ('PRIVATE_PASSWORD', 'PRIVATE_BEARER', 'sk-synthetic', 'CURRENT_PROVIDER_KEY', '/tmp/private'):
            self.assertNotIn(private, result['output'])
        self.assertTrue(result['preview_truncated'])
        self.assertTrue(result['redacted'])

    def test_rejects_unknown_and_malformed_activity_shapes(self) -> None:
        for value in (
            None,
            [],
            call(kind='native_output'),
            call(tool='exec_command'),
            call(id='../file'),
            call(status=[]),
        ):
            self.assertIsNone(normalize_activity_details(value))

    def test_omits_embedded_and_truncated_raw_records(self) -> None:
        for output in (
            'tool output: {"type":"event_msg","payload":{"text":"PRIVATE_ROW"}}',
            '{"type":"response_item","payload":{"content":"PRIVATE_ROW',
            '{"encrypted_reasoning":"PRIVATE_REASONING',
        ):
            result = normalize_activity_details(call(output=output))
            assert result is not None
            self.assertNotIn('PRIVATE_ROW', result['output'])
            self.assertNotIn('PRIVATE_REASONING', result['output'])

    def test_preserves_truncation_and_redaction_markers_on_normalization(self) -> None:
        result = normalize_activity_details(
            call(output='retained', output_chars=10_000, context_truncated=True, preview_truncated=True, redacted=True)
        )
        self.assertEqual(normalize_activity_details(result), result)

    def test_omits_multiline_private_keys_even_when_truncated(self) -> None:
        for ending in ('\n-----END PRIVATE KEY-----', ''):
            output = (
                '[turn synthetic-turn, line 4]\n-----BEGIN PRIVATE KEY-----\n'
                'SYNTHETIC_PRIVATE_KEY_BODY\nSECOND_PRIVATE_KEY_LINE'
                + ending
                + '\n[turn synthetic-turn, line 5]\nNext normalized event.'
            )
            with self.subTest(complete=bool(ending)):
                result = normalize_activity_details(call(output=output))
                assert result is not None
                self.assertNotIn('SYNTHETIC_PRIVATE_KEY_BODY', result['output'])
                self.assertNotIn('SECOND_PRIVATE_KEY_LINE', result['output'])
                self.assertIn('[private key omitted]', result['output'])
                self.assertTrue(result['redacted'])
                self.assertEqual(normalize_activity_details(result), result)

    def test_omits_multiline_malformed_journal_blocks_before_line_processing(self) -> None:
        for block in (
            '{\n  "type": "response_item",\n  "payload": {\n    "content": "SYNTHETIC_PRIVATE_ROW',
            '{\n  "raw_journal": [\n    {"text": "SYNTHETIC_PRIVATE_ROW',
        ):
            output = '[turn synthetic-turn, line 4]\n' + block + '\n[turn synthetic-turn, line 5]\nMore text.'
            with self.subTest(block=block):
                result = normalize_activity_details(call(output=output))
                assert result is not None
                self.assertNotIn('SYNTHETIC_PRIVATE_ROW', result['output'])
                self.assertTrue(result['redacted'])

    def test_redacts_json_keys_as_well_as_values(self) -> None:
        output = json.dumps(
            {
                'SYNTHETIC_PROVIDER_SENTINEL': 'An ordinary value',
                'nested': {'/custom/private/synthetic.jsonl': 'Another ordinary value'},
            }
        )
        result = normalize_activity_details(call(output=output), private_values=('SYNTHETIC_PROVIDER_SENTINEL',))
        assert result is not None
        self.assertNotIn('SYNTHETIC_PROVIDER_SENTINEL', result['output'])
        self.assertNotIn('/custom/private', result['output'])
        self.assertNotIn('synthetic.jsonl', result['output'])
        self.assertIn('An ordinary value', result['output'])
        self.assertTrue(result['redacted'])

    def test_preserves_returned_count_when_redaction_and_formatting_expand_preview(self) -> None:
        output = json.dumps({'source': '/a', 'items': [1, 2]})
        result = normalize_activity_details(call(output=output))
        assert result is not None
        self.assertGreater(len(result['output']), len(output))
        self.assertEqual(result['output_chars'], len(output))
        self.assertEqual(normalize_activity_details(result), result)
