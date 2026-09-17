from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_trace_studio.cli import main
from helpers import write_journal


class CliTest(unittest.TestCase):
    def test_builds_report_from_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            journal_dir = root / 'journals'
            journal_dir.mkdir()
            write_journal(journal_dir / 'session.jsonl')
            output = root / 'report'
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main([str(journal_dir), '--output', str(output), '--title', 'Test Dashboard'])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['sessions'], 1)
        self.assertEqual(payload['turns'], 2)
        self.assertEqual(Path(payload['index']), output / 'index.html')

    def test_builds_report_from_exact_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / 'codex-home'
            session_id = '00000000-0000-4000-8000-000000000001'
            journal = codex_home / 'sessions' / '2026' / '08' / f'rollout-{session_id}.jsonl'
            journal.parent.mkdir(parents=True)
            write_journal(journal, session_id=session_id)
            write_journal(journal.with_name('rollout-unrelated-session.jsonl'), session_id='unrelated-session')
            output = root / 'report'
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        '--session-id',
                        session_id,
                        '--codex-home',
                        str(codex_home),
                        '--include-trace',
                        '--output',
                        str(output),
                    ]
                )

            payload = json.loads(stdout.getvalue())
            html = (output / 'index.html').read_text(encoding='utf-8')

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload['session_ids'], [session_id])
        self.assertGreater(payload['trace_events'], 0)
        self.assertEqual(payload['source_paths'], [str(journal.resolve())])
        self.assertIn('<h1 id="dashboard-title">Agent Trace Studio</h1>', html)
        self.assertIn('Agent execution trace', html)

    def test_builds_report_from_json_session_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jsonl_path = root / 'session.jsonl'
            write_journal(jsonl_path)
            session_file = root / 'session.json'
            session_file.write_text(
                json.dumps({'content_text': jsonl_path.read_text(encoding='utf-8')}),
                encoding='utf-8',
            )
            output = root / 'report'
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = main(['--session-file', str(session_file), '--output', str(output)])

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload['source_paths'], [str(session_file.resolve())])
        self.assertEqual(payload['sessions'], 1)

    def test_serve_mode_enables_trace_and_passes_server_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / 'session.jsonl'
            write_journal(journal)
            codex_home = root / 'codex-home'
            codex_home.mkdir()
            output = root / 'report'

            with mock.patch('agent_trace_studio.cli._serve', return_value=0) as serve:
                exit_code = main(
                    [
                        '--session-file',
                        str(journal),
                        '--serve',
                        '--no-supervise',
                        '--port',
                        '9123',
                        '--qa-model',
                        'test-model',
                        '--codex-home',
                        str(codex_home),
                        '--output',
                        str(output),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertTrue(serve.call_args.kwargs['result'].traces)
        self.assertEqual(serve.call_args.kwargs['port'], 9123)
        self.assertEqual(serve.call_args.kwargs['model'], 'test-model')
        self.assertEqual(serve.call_args.kwargs['codex_home'], codex_home)

    def test_live_mode_can_start_without_an_existing_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report'
            with mock.patch('agent_trace_studio.cli._serve', return_value=0) as serve:
                exit_code = main(
                    [
                        '--live',
                        '--no-supervise',
                        '--live-poll-ms',
                        '125',
                        '--output',
                        str(output),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertTrue(serve.call_args.kwargs['live'])
        self.assertEqual(serve.call_args.kwargs['live_poll_interval'], 0.125)
        self.assertEqual(serve.call_args.kwargs['result'].source_paths, ())

    def test_supervise_mode_delegates_to_cross_platform_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            (source / 'src/agent_trace_studio').mkdir(parents=True)
            (source / 'tests').mkdir()
            (source / 'pyproject.toml').write_text('[project]\nname = "test"\n', encoding='utf-8')
            (source / 'AGENTS.md').write_text('# Test\n', encoding='utf-8')
            (source / 'src/agent_trace_studio/parser.py').write_text('VALUE = 1\n', encoding='utf-8')
            output = root / 'dashboard'
            with mock.patch('agent_trace_studio.supervisor.run_supervisor', return_value=0) as supervisor:
                exit_code = main(
                    [
                        '--supervise',
                        '--live',
                        '--port',
                        '9125',
                        '--output',
                        str(output),
                        '--source-workspace',
                        str(source),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(supervisor.call_args.kwargs['public_port'], 9125)
        self.assertEqual(supervisor.call_args.kwargs['output_dir'], output)
        self.assertEqual(supervisor.call_args.kwargs['source_workspace'], source.resolve())
        self.assertIn('--supervise', supervisor.call_args.kwargs['child_args'])

    def test_server_mode_uses_cross_platform_supervisor_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            (source / 'src/agent_trace_studio').mkdir(parents=True)
            (source / 'tests').mkdir()
            (source / 'pyproject.toml').write_text('[project]\nname = "test"\n', encoding='utf-8')
            (source / 'AGENTS.md').write_text('# Test\n', encoding='utf-8')
            (source / 'src/agent_trace_studio/parser.py').write_text('VALUE = 1\n', encoding='utf-8')
            journal = root / 'session.jsonl'
            write_journal(journal)
            with mock.patch('agent_trace_studio.supervisor.run_supervisor', return_value=0) as supervisor:
                exit_code = main(
                    [
                        '--serve',
                        '--session-file',
                        str(journal),
                        '--source-workspace',
                        str(source),
                    ]
                )

        self.assertEqual(exit_code, 0)
        supervisor.assert_called_once()

    def test_assurance_demo_generates_synthetic_trace_and_starts_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'assurance-demo'
            with mock.patch('agent_trace_studio.cli._serve', return_value=0) as serve:
                exit_code = main(['--assurance-demo', '--no-supervise', '--output', str(output), '--port', '9124'])

        self.assertEqual(exit_code, 0)
        self.assertEqual(serve.call_args.kwargs['port'], 9124)
        self.assertEqual(serve.call_args.kwargs['title'], 'Agent Assurance Demo')
        self.assertTrue(serve.call_args.kwargs['assurance']['enabled'])
        self.assertEqual(serve.call_args.kwargs['result'].sessions[0].session_id, 'assurance-demo-release-agent')


if __name__ == '__main__':
    unittest.main()
