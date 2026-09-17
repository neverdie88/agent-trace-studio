from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from agent_trace_studio.supervisor import (
    ChildRuntime,
    DashboardSupervisor,
    SupervisorLaunchSpec,
    SupervisorProxyHandler,
    _child_prefix,
    _free_loopback_port,
    _rewrite_child_arguments,
)
from agent_trace_studio.workspace import snapshot_workspace
from helpers import write_journal


class SupervisorTest(unittest.TestCase):
    def test_child_page_probe_reads_a_bounded_prefix_from_large_dashboard(self) -> None:
        payload = b'Agent Trace Studio' + (b'x' * 4096)
        with mock.patch('urllib.request.urlopen', return_value=io.BytesIO(payload)):
            result = _child_prefix(43000, '/', timeout=1, limit=64)

        self.assertEqual(len(result), 64)
        self.assertIn(b'Agent Trace Studio', result)

    def test_child_arguments_replace_public_runtime_coordinates(self) -> None:
        rewritten = _rewrite_child_arguments(
            (
                '--session-file',
                '/tmp/session.jsonl',
                '--supervise',
                '--serve',
                '--port=8766',
                '-o',
                '/tmp/public-dashboard',
            ),
            port=43123,
            output_dir=Path('/tmp/generation/dashboard'),
            agent_state_dir=Path('/tmp/shared-agent-state'),
            live_state_dir=Path('/tmp/shared-live-state'),
        )

        self.assertNotIn('--supervise', rewritten)
        self.assertIn('--supervised-child', rewritten)
        self.assertEqual(rewritten[rewritten.index('--port') + 1], '43123')
        self.assertEqual(Path(rewritten[rewritten.index('--output') + 1]), Path('/tmp/generation/dashboard'))
        self.assertEqual(Path(rewritten[rewritten.index('--agent-state-dir') + 1]), Path('/tmp/shared-agent-state'))
        self.assertEqual(Path(rewritten[rewritten.index('--live-state-dir') + 1]), Path('/tmp/shared-live-state'))

    def test_verified_candidate_is_promoted_and_previous_runtime_becomes_standby(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            agent_state = root / 'agent-state'
            backup = agent_state / 'backups/run1/attempt-1'
            backup.mkdir(parents=True)
            supervisor = DashboardSupervisor(
                SupervisorLaunchSpec(
                    child_args=(),
                    output_dir=root / 'dashboard',
                    state_dir=root / 'supervisor',
                    agent_state_dir=agent_state,
                    live_state_dir=root / 'live-state',
                    source_workspace=source,
                    cwd=root,
                ),
                public_port=8766,
            )
            previous = _runtime('previous', root)
            candidate = _runtime('candidate', root)
            supervisor._active = previous
            with (
                mock.patch.object(supervisor, '_start_child', return_value=candidate),
                mock.patch.object(supervisor, '_wait_for_health', return_value={'passed': True}),
                mock.patch.object(supervisor, '_publish_output'),
            ):
                deployment = supervisor.activate(_activation_payload(backup))

        self.assertEqual(deployment['status'], 'promoted')
        self.assertEqual(supervisor._active, candidate)
        self.assertEqual(supervisor._standby, previous)

    def test_failed_candidate_restores_source_and_keeps_previous_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            agent_state = root / 'agent-state'
            backup = agent_state / 'backups/run1/attempt-1'
            backup.mkdir(parents=True)
            supervisor = DashboardSupervisor(
                SupervisorLaunchSpec(
                    child_args=(),
                    output_dir=root / 'dashboard',
                    state_dir=root / 'supervisor',
                    agent_state_dir=agent_state,
                    live_state_dir=root / 'live-state',
                    source_workspace=source,
                    cwd=root,
                ),
                public_port=8766,
            )
            previous = _runtime('previous', root)
            candidate = _runtime('candidate', root)
            supervisor._active = previous
            with (
                mock.patch.object(supervisor, '_start_child', return_value=candidate),
                mock.patch.object(
                    supervisor,
                    '_wait_for_health',
                    return_value={'passed': False, 'message': 'synthetic startup failure'},
                ),
                mock.patch.object(supervisor, '_restore_source', return_value={'status': 'restored'}),
            ):
                deployment = supervisor.activate(_activation_payload(backup))

        self.assertEqual(deployment['status'], 'failed')
        self.assertEqual(deployment['rollback']['status'], 'restored')
        self.assertEqual(supervisor._active, previous)

    @unittest.skipUnless(
        os.environ.get('AGENT_TRACE_STUDIO_RUN_SUPERVISOR_INTEGRATION') == '1',
        'set AGENT_TRACE_STUDIO_RUN_SUPERVISOR_INTEGRATION=1 for process integration',
    )
    def test_process_promotion_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = Path(__file__).resolve().parents[1]
            source = root / 'source'
            package = source / 'src/agent_trace_studio'
            package.parent.mkdir(parents=True)
            shutil.copytree(repository / 'src/agent_trace_studio', package)
            shutil.copy2(repository / 'pyproject.toml', source / 'pyproject.toml')
            shutil.copy2(repository / 'AGENTS.md', source / 'AGENTS.md')
            (source / 'tests').mkdir()
            journal = root / 'session.jsonl'
            write_journal(journal, session_id='supervisor-integration')
            agent_state = root / 'agent-state'
            port = _free_loopback_port()
            supervisor = DashboardSupervisor(
                SupervisorLaunchSpec(
                    child_args=(
                        '--session-file',
                        str(journal),
                        '--include-trace',
                        '--serve',
                        '--source-workspace',
                        str(source),
                    ),
                    output_dir=root / 'dashboard',
                    state_dir=root / 'supervisor',
                    agent_state_dir=agent_state,
                    live_state_dir=root / 'live-state',
                    source_workspace=source,
                    cwd=source,
                    python=Path(sys.executable).absolute(),
                    environment=(('PYTHONPATH', str(source / 'src')),),
                ),
                public_port=port,
                health_timeout=30,
            )
            server = ThreadingHTTPServer(
                ('127.0.0.1', port),
                partial(SupervisorProxyHandler, supervisor=supervisor),
            )
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                initial = supervisor.start_initial()
                parser = source / 'src/agent_trace_studio/parser.py'
                original = parser.read_bytes()
                backup = agent_state / 'backups/integrationrun1/attempt-1'
                backup_parser = backup / 'src/agent_trace_studio/parser.py'
                backup_parser.parent.mkdir(parents=True)
                backup_parser.write_bytes(original)
                parser.write_bytes(original + b'\n# supervised candidate\n')
                applied = snapshot_workspace(source).files['src/agent_trace_studio/parser.py']
                activation_payload = {
                    'run_id': 'integrationrun1',
                    'change_digest': 'integration-change',
                    'rollback_manifest': {
                        'backup_path': str(backup),
                        'source_delta': {
                            'added': [],
                            'modified': ['src/agent_trace_studio/parser.py'],
                            'deleted': [],
                        },
                        'applied_records': {
                            'src/agent_trace_studio/parser.py': {
                                'digest': applied.digest,
                                'size': applied.size,
                                'mode': applied.mode,
                                'kind': applied.kind,
                            }
                        },
                    },
                }
                deployment = supervisor.activate(activation_payload)
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=5) as response:
                    promoted_status = json.loads(response.read())
                supervisor._rollback_active('Synthetic post-promotion failure.')
                replayed_activation = supervisor.activate(activation_payload)
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=5) as response:
                    rolled_back_status = json.loads(response.read())
                restored = parser.read_bytes()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                supervisor.close()

        self.assertEqual(deployment['status'], 'promoted')
        self.assertNotEqual(deployment['generation'], initial['generation'])
        self.assertEqual(promoted_status['deployment']['generation'], deployment['generation'])
        self.assertEqual(rolled_back_status['deployment']['generation'], initial['generation'])
        self.assertEqual(replayed_activation['status'], 'rolled_back')
        self.assertEqual(replayed_activation['previous_generation'], initial['generation'])
        self.assertEqual(restored, original)


def _runtime(generation: str, root: Path) -> ChildRuntime:
    process = mock.Mock()
    process.poll.return_value = None
    process.pid = 123
    return ChildRuntime(
        generation=generation,
        port=43000,
        output_dir=root,
        log_path=root / f'{generation}.log',
        process=process,
        log_stream=io.BytesIO(),
        started_at='2026-08-25T00:00:00+00:00',
    )


def _activation_payload(backup: Path) -> dict[str, object]:
    return {
        'run_id': 'run1',
        'change_digest': 'abc123',
        'rollback_manifest': {
            'backup_path': str(backup),
            'source_delta': {
                'added': [],
                'modified': ['src/agent_trace_studio/parser.py'],
                'deleted': [],
            },
            'applied_records': {
                'src/agent_trace_studio/parser.py': {
                    'digest': 'abc123',
                    'size': 10,
                    'mode': 420,
                    'kind': 'file',
                }
            },
        },
    }


if __name__ == '__main__':
    unittest.main()
