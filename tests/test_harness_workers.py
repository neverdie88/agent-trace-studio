from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from agent_trace_studio.harness_backend import _safe_subprocess_environment
from agent_trace_studio.harness_workers import CodexThreadWorker, OpenCodeServerWorker, PersistentHarnessError


class CodexThreadWorkerTests(unittest.TestCase):
    def test_startup_errors_close_the_child_and_only_report_known_codes(self) -> None:
        helper = Path(__file__).resolve().parents[1] / 'src/agent_trace_studio/assets/codex_thread_worker.mjs'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entry = root / 'broken-sdk.mjs'
            for code in ('ENOENT', 'PRIVATE_KEY_SHOULD_NOT_APPEAR'):
                with self.subTest(code=code):
                    entry.write_text(
                        f'throw Object.assign(new Error("PRIVATE_PATH_AND_TOKEN"), {{code: {json.dumps(code)}}});',
                        encoding='utf-8',
                    )
                    workers = []
                    close = CodexThreadWorker.close

                    def capture_close(worker, captured=workers, original_close=close):
                        captured.append(worker)
                        original_close(worker)

                    with (
                        mock.patch.object(CodexThreadWorker, 'close', capture_close),
                        self.assertRaises(PersistentHarnessError) as raised,
                    ):
                        CodexThreadWorker(
                            entry=entry,
                            helper=helper,
                            workspace=root,
                            thread_id=None,
                            model='',
                            environment=_safe_subprocess_environment(),
                        )
                    self.assertIn('failed to initialize', str(raised.exception))
                    self.assertNotIn('PRIVATE', str(raised.exception))
                    if code == 'ENOENT':
                        self.assertIn('(ENOENT)', str(raised.exception))
                    self.assertEqual(len(workers), 1)
                    self.assertIsNotNone(workers[0].process.poll())
                    self.assertFalse(workers[0].alive)

    def test_windows_environment_reaches_sdk_without_provider_keys(self) -> None:
        helper = Path(__file__).resolve().parents[1] / 'src/agent_trace_studio/assets/codex_thread_worker.mjs'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entry = root / 'environment-sdk.mjs'
            environment = _safe_subprocess_environment()
            expected = {name: environment.get(name, str(root)) for name in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP')}
            entry.write_text(
                'import assert from "node:assert/strict"; export class Codex { constructor({env}) {'
                f'for (const [key, value] of Object.entries({json.dumps(expected)})) assert.equal(env[key], value);'
                'assert.ok(!Object.hasOwn(env, "OPENAI_API_KEY"));'
                'assert.ok(!Object.hasOwn(env, "UNKNOWN_SECRET"));'
                '} startThread() { return {id: "synthetic-thread"}; }}',
                encoding='utf-8',
            )
            worker = CodexThreadWorker(
                entry=entry,
                helper=helper,
                workspace=root,
                thread_id=None,
                model='',
                environment={
                    **environment,
                    **expected,
                    'OPENAI_API_KEY': 'DO_NOT_FORWARD',
                    'UNKNOWN_SECRET': 'PRIVATE',
                },
            )
            try:
                self.assertTrue(worker.alive)
            finally:
                worker.close()

    def test_reuses_one_sdk_thread_object_for_repeated_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entry = root / 'fake-codex-sdk.mjs'
            entry.write_text(
                """
class FakeThread {
  constructor(id) {
    this.id = id;
    this.turn = 0;
  }

  async runStreamed(prompt) {
    this.turn += 1;
    if (!this.id) this.id = 'thread-live-1';
    const finalResponse = `${prompt}:turn-${this.turn}`;
    async function* events() {
      yield { type: 'turn.started' };
      yield { type: 'item.started', item: { type: 'reasoning', text: 'PRIVATE_REASONING' } };
      yield { type: 'item.completed', item: { type: 'agent_message', text: finalResponse } };
      yield { type: 'turn.completed', usage: { input_tokens: 10, cached_input_tokens: 4, output_tokens: 2 } };
    }
    return { events: events() };
  }
}

export class Codex {
  startThread() {
    return new FakeThread(null);
  }

  resumeThread(id) {
    return new FakeThread(id);
  }
}
""".strip(),
                encoding='utf-8',
            )
            workspace = root / 'workspace'
            workspace.mkdir()
            helper = (
                Path(__file__).resolve().parents[1]
                / 'src'
                / 'agent_trace_studio'
                / 'assets'
                / 'codex_thread_worker.mjs'
            )
            environment = _safe_subprocess_environment()
            worker = CodexThreadWorker(
                entry=entry,
                helper=helper,
                workspace=workspace,
                thread_id=None,
                model='',
                environment=environment,
            )
            try:
                activity = []
                first = worker.run('first', timeout=3, cancel_event=None, progress=activity.append)
                second = worker.run('second', timeout=3, cancel_event=None)
            finally:
                worker.close()

        self.assertEqual(first['finalResponse'], 'first:turn-1')
        self.assertEqual(second['finalResponse'], 'second:turn-2')
        self.assertEqual(first['threadId'], 'thread-live-1')
        self.assertEqual(second['threadId'], 'thread-live-1')
        serialized = ' '.join(item.message for item in activity)
        self.assertIn('Codex turn started.', serialized)
        self.assertIn('10 input tokens', serialized)
        self.assertIn('Codex usage', serialized)
        self.assertNotIn('turn completed', serialized)
        self.assertNotIn('prepared the response', serialized)
        self.assertNotIn('Response', [item.phase for item in activity])
        self.assertNotIn('PRIVATE_REASONING', serialized)

    def test_cancellation_closes_the_persistent_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entry = root / 'fake-codex-sdk.mjs'
            entry.write_text(
                """
class FakeThread {
  constructor() { this.id = 'thread-live-1'; }
  async runStreamed() {
    return { events: { async *[Symbol.asyncIterator]() { await new Promise(() => {}); } } };
  }
}
export class Codex {
  startThread() { return new FakeThread(); }
  resumeThread() { return new FakeThread(); }
}
""".strip(),
                encoding='utf-8',
            )
            workspace = root / 'workspace'
            workspace.mkdir()
            helper = (
                Path(__file__).resolve().parents[1]
                / 'src'
                / 'agent_trace_studio'
                / 'assets'
                / 'codex_thread_worker.mjs'
            )
            environment = _safe_subprocess_environment()
            worker = CodexThreadWorker(
                entry=entry,
                helper=helper,
                workspace=workspace,
                thread_id=None,
                model='',
                environment=environment,
            )
            cancel = threading.Event()
            timer = threading.Timer(0.1, cancel.set)
            timer.start()
            try:
                with self.assertRaisesRegex(RuntimeError, 'stopped'):
                    worker.run('wait', timeout=3, cancel_event=cancel)
            finally:
                timer.cancel()
                worker.close()

        self.assertFalse(worker.alive)


class OpenCodeServerWorkerTests(unittest.TestCase):
    def test_reuses_one_native_session_through_the_direct_server_api(self) -> None:
        requests: list[tuple[str, dict[str, object]]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                event = {
                    'type': 'message.part.updated',
                    'properties': {
                        'part': {
                            'id': 'tool-1',
                            'sessionID': 'ses_live',
                            'type': 'tool',
                            'tool': 'search_trace',
                            'state': {'status': 'running', 'input': {'secret': 'PRIVATE_ARGUMENT'}},
                        }
                    },
                }
                body = f'data: {json.dumps(event)}\n\n'.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get('Content-Length') or 0)
                payload = json.loads(self.rfile.read(length))
                requests.append((self.path, payload))
                if self.path.startswith('/session?'):
                    response: dict[str, object] = {'id': 'ses_live'}
                else:
                    response = {
                        'info': {'sessionID': 'ses_live'},
                        'parts': [
                            {'type': 'text', 'text': f'answer-{len(requests)}'},
                            {
                                'id': f'step-{len(requests)}',
                                'type': 'step-finish',
                                'tokens': {'input': 12, 'output': 3, 'cache': {'read': 5}},
                            },
                        ],
                    }
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                pass

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        http_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                worker = OpenCodeServerWorker.__new__(OpenCodeServerWorker)
                worker._lock = threading.RLock()
                worker._closed = False
                worker.workspace = Path(directory)
                worker.username = 'studio-user'
                worker.password = 'ephemeral-password'
                worker.url = f'http://127.0.0.1:{httpd.server_port}'
                worker.process = mock.Mock()
                worker.process.poll.return_value = None

                activity = []
                first = worker.run(
                    'first prompt',
                    instruction='read only',
                    model='openai/gpt-test',
                    session_id=None,
                    timeout=3,
                    cancel_event=None,
                    progress=activity.append,
                )
                second = worker.run(
                    'second prompt',
                    instruction='read only',
                    model='openai/gpt-test',
                    session_id=first['session_id'],
                    timeout=3,
                    cancel_event=None,
                )
        finally:
            httpd.shutdown()
            httpd.server_close()
            http_thread.join(timeout=3)

        self.assertEqual(first['answer'], 'answer-2')
        self.assertEqual(first['session_id'], 'ses_live')
        self.assertEqual(first['usage']['input_tokens'], 12)
        self.assertEqual(second['answer'], 'answer-3')
        self.assertEqual(second['session_id'], 'ses_live')
        self.assertEqual(sum(path.startswith('/session?') for path, _payload in requests), 1)
        self.assertEqual(sum('/message?' in path for path, _payload in requests), 2)
        first_message = requests[1][1]
        second_message = requests[2][1]
        self.assertEqual(first_message['parts'], [{'type': 'text', 'text': 'first prompt'}])
        self.assertEqual(second_message['parts'], [{'type': 'text', 'text': 'second prompt'}])
        self.assertEqual(first_message['model'], {'providerID': 'openai', 'modelID': 'gpt-test'})
        serialized = ' '.join(item.message for item in activity)
        self.assertIn('OpenCode tool search_trace running.', serialized)
        self.assertIn('12 input tokens', serialized)
        self.assertIn('OpenCode usage', serialized)
        self.assertNotIn('model step completed', serialized)
        self.assertNotIn('prepared the response', serialized)
        self.assertNotIn('Response', [item.phase for item in activity])
        self.assertNotIn('PRIVATE_ARGUMENT', serialized)


if __name__ == '__main__':
    unittest.main()
