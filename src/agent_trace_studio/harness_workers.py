"""Long-lived local worker processes for stateful Studio agent harnesses."""

from __future__ import annotations

import base64
import json
import os
import queue
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from agent_trace_studio.agent_backend import AgentProgress, AgentProgressCallback


class PersistentHarnessError(RuntimeError):
    """A persistent harness worker failed or returned an invalid response."""


class PersistentHarnessCancelled(PersistentHarnessError):
    """A persistent harness turn was cancelled by the Studio user."""


class CodexThreadWorker:
    """Own one Codex SDK Thread object and execute repeated turns on it."""

    def __init__(
        self,
        *,
        entry: Path,
        helper: Path,
        workspace: Path | None,
        thread_id: str | None,
        model: str,
        environment: dict[str, str],
        startup_timeout: float = 15,
    ) -> None:
        self._lock = threading.RLock()
        self._responses: queue.Queue[dict[str, object]] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=100)
        self._closed = False
        self.thread_id = thread_id
        self._temporary = tempfile.TemporaryDirectory(prefix='agent-trace-codex-') if workspace is None else None
        active_workspace = Path(self._temporary.name).resolve() if self._temporary is not None else workspace.resolve()
        active_workspace.chmod(0o700)
        self.process = subprocess.Popen(
            ['node', str(helper.resolve())],
            cwd=active_workspace,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            bufsize=1,
            **_process_group_options(),
        )
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        try:
            self._write(
                {
                    'type': 'init',
                    'entry': str(entry.resolve()),
                    'workspace': str(active_workspace),
                    'threadId': thread_id,
                    'model': model or None,
                    'readOnly': True,
                }
            )
            response = self._wait_for_response(timeout=startup_timeout, cancel_event=None)
            if response.get('type') != 'ready':
                raise PersistentHarnessError('Codex SDK thread worker failed to initialize.')
        except BaseException:
            self.close()
            raise

    @property
    def alive(self) -> bool:
        return not self._closed and self.process.poll() is None

    def run(
        self,
        prompt: str,
        *,
        timeout: float,
        cancel_event: threading.Event | None,
        progress: AgentProgressCallback | None = None,
    ) -> dict[str, object]:
        with self._lock:
            if cancel_event is not None and cancel_event.is_set():
                raise PersistentHarnessCancelled('Dashboard agent stopped.')
            if not self.alive:
                raise PersistentHarnessError('Codex SDK thread worker is not running.')
            request_id = secrets.token_hex(12)
            self._write({'type': 'run', 'id': request_id, 'prompt': prompt})
            try:
                response = self._wait_for_response(
                    timeout=timeout,
                    cancel_event=cancel_event,
                    request_id=request_id,
                    progress=progress,
                )
            except (PersistentHarnessCancelled, subprocess.TimeoutExpired):
                self.close()
                raise
            if response.get('type') != 'result' or response.get('id') != request_id:
                self.close()
                raise PersistentHarnessError('Codex SDK thread worker returned an invalid response.')
            returned_thread_id = response.get('threadId')
            if isinstance(returned_thread_id, str) and returned_thread_id:
                self.thread_id = returned_thread_id
            return response

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self.process.stdin is not None:
                with suppress(OSError):
                    self.process.stdin.close()
            if self.process.poll() is None:
                _terminate_process_tree(self.process)
            self._stdout_thread.join(timeout=0.5)
            self._stderr_thread.join(timeout=0.5)
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    with suppress(OSError):
                        stream.close()
            if self._temporary is not None:
                self._temporary.cleanup()

    def _write(self, value: dict[str, object]) -> None:
        if self.process.stdin is None:
            raise PersistentHarnessError('Codex SDK thread worker input is unavailable.')
        try:
            self.process.stdin.write(json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n')
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise PersistentHarnessError('Codex SDK thread worker stopped unexpectedly.') from exc

    def _wait_for_response(
        self,
        *,
        timeout: float,
        cancel_event: threading.Event | None,
        request_id: str | None = None,
        progress: AgentProgressCallback | None = None,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise PersistentHarnessCancelled('Dashboard agent stopped.')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired('Codex SDK persistent thread turn', timeout)
            try:
                response = self._responses.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                if self.process.poll() is not None:
                    raise PersistentHarnessError('Codex SDK thread worker stopped unexpectedly.') from None
                continue
            if response.get('type') in {'error', 'fatal', 'eof', 'invalid'}:
                if request_id is None:
                    # Only fixed diagnostic codes may leave the worker boundary.
                    # SDK messages and stderr can contain private paths or keys.
                    code = response.get('code')
                    known_codes = (
                        'ENOENT',
                        'EACCES',
                        'EPERM',
                        'ERR_MODULE_NOT_FOUND',
                        'ERR_UNSUPPORTED_ESM_URL_SCHEME',
                    )
                    detail = f' ({code})' if code in known_codes else ''
                    raise PersistentHarnessError(f'Codex SDK thread worker failed to initialize{detail}.')
                raise PersistentHarnessError('Codex SDK thread worker failed to complete the turn.')
            if response.get('type') == 'activity':
                if response.get('id') == request_id and progress is not None:
                    phase = response.get('phase')
                    message = response.get('message')
                    if isinstance(phase, str) and isinstance(message, str):
                        progress(AgentProgress(phase, message))
                continue
            return response

    def _read_stdout(self) -> None:
        if self.process.stdout is None:
            return
        for line in self.process.stdout:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                self._responses.put({'type': 'invalid'})
                continue
            self._responses.put(value if isinstance(value, dict) else {'type': 'invalid'})
        self._responses.put({'type': 'eof'})

    def _read_stderr(self) -> None:
        if self.process.stderr is None:
            return
        for line in self.process.stderr:
            self._stderr.append(line.rstrip())


class OpenCodeServerWorker:
    """Own one loopback-only OpenCode server used by repeated session turns."""

    def __init__(
        self,
        *,
        binary: Path,
        environment: dict[str, str],
        skills_root: Path,
        startup_timeout: float = 15,
    ) -> None:
        self._lock = threading.RLock()
        self._temporary = tempfile.TemporaryDirectory(prefix='agent-trace-opencode-')
        self.workspace = Path(self._temporary.name).resolve()
        self.workspace.chmod(0o700)
        skill_target = self.workspace / '.agents' / 'skills'
        skill_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skills_root, skill_target)
        self.username = 'agent-trace-studio'
        self.password = secrets.token_urlsafe(32)
        self.port = _free_loopback_port()
        self.url = f'http://127.0.0.1:{self.port}'
        server_environment = dict(environment)
        server_environment.update(
            {
                'OPENCODE_SERVER_USERNAME': self.username,
                'OPENCODE_SERVER_PASSWORD': self.password,
            }
        )
        self._log: deque[str] = deque(maxlen=100)
        self._closed = False
        self.process = subprocess.Popen(
            [str(binary), 'serve', '--pure', '--hostname', '127.0.0.1', '--port', str(self.port)],
            cwd=self.workspace,
            env=server_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **_process_group_options(),
        )
        self._log_thread = threading.Thread(target=self._read_log, daemon=True)
        self._log_thread.start()
        try:
            self._wait_until_ready(startup_timeout)
        except BaseException:
            self.close()
            raise

    @property
    def alive(self) -> bool:
        return not self._closed and self.process.poll() is None

    def run(
        self,
        prompt: str,
        *,
        instruction: str,
        model: str,
        session_id: str | None,
        timeout: float,
        cancel_event: threading.Event | None,
        progress: AgentProgressCallback | None = None,
    ) -> dict[str, object]:
        """Send one turn directly to the persistent OpenCode server/session."""

        with self._lock:
            if cancel_event is not None and cancel_event.is_set():
                raise PersistentHarnessCancelled('Dashboard agent stopped.')
            if not self.alive:
                raise PersistentHarnessError('OpenCode persistent server is not running.')
            active_session_id = session_id
            if not active_session_id:
                created = self._request_json(
                    '/session',
                    payload={'title': 'Agent Trace Studio'},
                    timeout=min(timeout, 30),
                    cancel_event=cancel_event,
                )
                candidate = created.get('id')
                if not isinstance(candidate, str) or not candidate.startswith('ses'):
                    raise PersistentHarnessError('OpenCode failed to create a persistent session.')
                active_session_id = candidate
            observed: set[str] = set()

            def publish(key: str, activity: AgentProgress | None) -> None:
                if progress is None or activity is None or key in observed:
                    return
                observed.add(key)
                progress(activity)

            stop_stream, stream_thread, stream_response = self._subscribe_to_session_events(
                active_session_id,
                publish=publish,
            )
            request_payload: dict[str, object] = {
                'system': instruction,
                'parts': [{'type': 'text', 'text': prompt}],
            }
            provider_id, separator, model_id = model.partition('/')
            if separator and provider_id and model_id:
                request_payload['model'] = {'providerID': provider_id, 'modelID': model_id}
            encoded_session = urllib.parse.quote(active_session_id, safe='')
            publish('turn-started', AgentProgress('Turn', 'OpenCode turn started.'))
            try:
                response = self._request_json(
                    f'/session/{encoded_session}/message',
                    payload=request_payload,
                    timeout=timeout,
                    cancel_event=cancel_event,
                )
            finally:
                stop_stream.set()
                if stream_response:
                    with suppress(OSError):
                        stream_response[0].close()
                stream_thread.join(timeout=0.2)
            info = response.get('info')
            if not isinstance(info, dict) or info.get('error'):
                raise PersistentHarnessError('OpenCode failed to complete the persistent session turn.')
            parts = response.get('parts')
            if not isinstance(parts, list):
                raise PersistentHarnessError('OpenCode returned an invalid persistent session response.')
            answer = ''.join(
                str(part.get('text') or '') for part in parts if isinstance(part, dict) and part.get('type') == 'text'
            ).strip()
            if not answer:
                raise PersistentHarnessError('OpenCode failed to return an answer.')
            for part in parts:
                if not isinstance(part, dict):
                    continue
                key, activity = _opencode_part_activity(part)
                publish(key, activity)
            returned_session_id = info.get('sessionID')
            return {
                'answer': answer,
                'session_id': (
                    returned_session_id
                    if isinstance(returned_session_id, str) and returned_session_id
                    else active_session_id
                ),
                'usage': _opencode_usage(parts),
            }

    def _subscribe_to_session_events(
        self,
        session_id: str,
        *,
        publish: Callable[[str, AgentProgress | None], None],
    ) -> tuple[threading.Event, threading.Thread, list[object]]:
        """Observe the loopback SSE stream; failure is non-fatal because the response remains authoritative."""

        stop = threading.Event()
        connected = threading.Event()
        response_holder: list[object] = []
        query = urllib.parse.urlencode({'directory': str(self.workspace)})
        request = urllib.request.Request(
            f'{self.url}/event?{query}',
            headers={
                'Authorization': f'Basic {self._authorization_token()}',
                'Accept': 'text/event-stream',
            },
        )

        def receive() -> None:
            try:
                response = urllib.request.urlopen(request, timeout=30)
                response_holder.append(response)
                connected.set()
                with response:
                    for raw_line in response:
                        if stop.is_set():
                            break
                        line = raw_line.decode('utf-8', errors='replace').strip()
                        if not line.startswith('data:'):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        normalized = _normalized_opencode_event(event)
                        if normalized is None or not _opencode_event_matches_session(normalized, session_id):
                            continue
                        key, activity = _opencode_event_activity(normalized)
                        publish(key, activity)
            except (OSError, ValueError, urllib.error.URLError):
                pass
            finally:
                connected.set()

        thread = threading.Thread(target=receive, daemon=True)
        thread.start()
        connected.wait(0.5)
        return stop, thread, response_holder

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self.process.poll() is None:
                _terminate_process_tree(self.process)
            self._log_thread.join(timeout=0.5)
            if self.process.stdout is not None:
                with suppress(OSError):
                    self.process.stdout.close()
            self._temporary.cleanup()

    def _request_json(
        self,
        route: str,
        *,
        payload: dict[str, object],
        timeout: float,
        cancel_event: threading.Event | None,
    ) -> dict[str, object]:
        query = urllib.parse.urlencode({'directory': str(self.workspace)})
        request = urllib.request.Request(
            f'{self.url}{route}?{query}',
            data=json.dumps(payload, ensure_ascii=True, separators=(',', ':')).encode(),
            headers={
                'Authorization': f'Basic {self._authorization_token()}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        responses: queue.Queue[tuple[dict[str, object] | None, BaseException | None]] = queue.Queue(maxsize=1)

        def send() -> None:
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    value = json.loads(response.read())
                if not isinstance(value, dict):
                    raise PersistentHarnessError('OpenCode returned an invalid server response.')
                responses.put((value, None))
            except BaseException as exc:
                responses.put((None, exc))

        request_thread = threading.Thread(target=send, daemon=True)
        request_thread.start()
        deadline = time.monotonic() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self.close()
                raise PersistentHarnessCancelled('Dashboard agent stopped.')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise subprocess.TimeoutExpired('OpenCode persistent session turn', timeout)
            try:
                value, error = responses.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                if self.process.poll() is not None:
                    raise PersistentHarnessError('OpenCode persistent server stopped unexpectedly.') from None
                continue
            if error is not None:
                raise PersistentHarnessError('OpenCode persistent server request failed.') from error
            if value is None:
                raise PersistentHarnessError('OpenCode returned an empty server response.')
            return value

    def _wait_until_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        request = urllib.request.Request(f'{self.url}/global/health')
        request.add_header('Authorization', f'Basic {self._authorization_token()}')
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise PersistentHarnessError('OpenCode persistent server stopped during startup.')
            try:
                with urllib.request.urlopen(request, timeout=0.5) as response:
                    payload = json.loads(response.read())
                if isinstance(payload, dict) and payload.get('healthy') is True:
                    return
            except (OSError, ValueError, urllib.error.URLError):
                time.sleep(0.05)
        raise PersistentHarnessError('OpenCode persistent server did not become ready.')

    def _authorization_token(self) -> str:
        return base64.b64encode(f'{self.username}:{self.password}'.encode()).decode()

    def _read_log(self) -> None:
        if self.process.stdout is None:
            return
        for line in self.process.stdout:
            self._log.append(line.rstrip())


def _normalized_opencode_event(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    payload = value.get('payload')
    if isinstance(payload, dict) and isinstance(payload.get('type'), str):
        return payload
    return value if isinstance(value.get('type'), str) else None


def _opencode_event_matches_session(event: dict[str, object], session_id: str) -> bool:
    properties = event.get('properties')
    if not isinstance(properties, dict):
        return True
    candidates: list[object] = [properties.get('sessionID'), properties.get('session_id')]
    for key in ('part', 'info'):
        nested = properties.get(key)
        if isinstance(nested, dict):
            candidates.extend((nested.get('sessionID'), nested.get('session_id')))
    observed = next((item for item in candidates if isinstance(item, str) and item), None)
    return observed is None or observed == session_id


def _opencode_event_activity(event: dict[str, object]) -> tuple[str, AgentProgress | None]:
    event_type = str(event.get('type') or '')
    properties = event.get('properties')
    props = properties if isinstance(properties, dict) else {}
    if event_type == 'message.part.updated':
        part = props.get('part')
        return _opencode_part_activity(part) if isinstance(part, dict) else ('part-invalid', None)
    if event_type == 'session.status':
        raw_status = props.get('status')
        status = raw_status if isinstance(raw_status, dict) else {}
        status_type = _safe_activity_name(status.get('type'), fallback='updated')
        if status_type == 'retry':
            attempt = _nonnegative_int(status.get('attempt'))
            return (
                f'session-retry:{attempt}',
                AgentProgress('Retry', f'OpenCode scheduled model retry {attempt or 1}.'),
            )
        if status_type == 'busy':
            return ('session-busy', AgentProgress('Status', 'OpenCode session is working.'))
        if status_type == 'idle':
            return ('session-idle', AgentProgress('Status', 'OpenCode session returned to idle.'))
        return (f'session-status:{status_type}', AgentProgress('Status', f'OpenCode session status: {status_type}.'))
    if event_type == 'session.error':
        return ('session-error', AgentProgress('Error', 'OpenCode reported a session error.'))
    if event_type == 'message.updated':
        # The host classifies the completed assistant message after parsing it.
        # It may be a context-action request rather than the final response.
        return ('assistant-message-updated', None)
    if event_type in {'file.edited', 'file.watcher.updated'}:
        return (
            f'safety:{event_type}',
            AgentProgress('Safety', 'OpenCode reported file activity in the read-only Studio workspace.'),
        )
    if event_type == 'todo.updated':
        todos = props.get('todos')
        count = len(todos) if isinstance(todos, list) else 0
        return ('todo-updated', AgentProgress('Plan', f'OpenCode updated a {count}-item plan.'))
    if event_type == 'permission.asked':
        return ('permission-asked', AgentProgress('Safety', 'OpenCode requested permission in the read-only session.'))
    return (f'ignored:{event_type}', None)


def _opencode_part_activity(part: dict[str, object]) -> tuple[str, AgentProgress | None]:
    part_type = str(part.get('type') or '')
    identifier = str(part.get('id') or part.get('callID') or '')
    if part_type == 'reasoning':
        time_value = part.get('time')
        completed = isinstance(time_value, dict) and time_value.get('end') is not None
        state = 'completed' if completed else 'started'
        return (
            f'reasoning:{identifier}:{state}',
            AgentProgress('Reasoning', f'OpenCode {state} an analysis step.'),
        )
    if part_type == 'text':
        # Text-part lifecycle events do not reveal whether the model returned a
        # context request or a final answer. The host emits one classified event.
        return (f'text:{identifier}', None)
    if part_type == 'tool':
        state_value = part.get('state')
        state = state_value if isinstance(state_value, dict) else {}
        status = _safe_activity_name(state.get('status'), fallback='updated')
        tool_name = _safe_activity_name(part.get('tool'), fallback='tool')
        return (
            f'tool:{identifier}:{status}',
            AgentProgress('Tool', f'OpenCode tool {tool_name} {status}.'),
        )
    if part_type == 'step-start':
        return (f'step:{identifier}:started', AgentProgress('Model', 'OpenCode started a model step.'))
    if part_type == 'step-finish':
        tokens = part.get('tokens')
        token_counts = tokens if isinstance(tokens, dict) else {}
        cache = token_counts.get('cache')
        cache_counts = cache if isinstance(cache, dict) else {}
        input_tokens = _nonnegative_int(token_counts.get('input'))
        output_tokens = _nonnegative_int(token_counts.get('output'))
        cached_tokens = _nonnegative_int(cache_counts.get('read'))
        return (
            f'step:{identifier}:completed',
            AgentProgress(
                'Usage',
                f'OpenCode usage · {input_tokens} input tokens · {cached_tokens} cached · {output_tokens} output.',
            ),
        )
    if part_type == 'compaction':
        return (f'compaction:{identifier}', AgentProgress('Context', 'OpenCode compacted its session context.'))
    if part_type == 'retry':
        return (f'retry:{identifier}', AgentProgress('Retry', 'OpenCode retried the model request.'))
    if part_type in {'file', 'patch'}:
        return (
            f'safety:{part_type}:{identifier}',
            AgentProgress('Safety', 'OpenCode reported file activity in the read-only Studio workspace.'),
        )
    return (f'ignored-part:{part_type}:{identifier}', None)


def _opencode_usage(parts: list[object]) -> dict[str, int]:
    usage = {'input_tokens': 0, 'output_tokens': 0, 'requests': 0, 'total_tokens': 0}
    for part in parts:
        if not isinstance(part, dict) or part.get('type') != 'step-finish':
            continue
        tokens = part.get('tokens')
        token_counts = tokens if isinstance(tokens, dict) else {}
        usage['input_tokens'] += _nonnegative_int(token_counts.get('input'))
        usage['output_tokens'] += _nonnegative_int(token_counts.get('output'))
        usage['requests'] += 1
    if usage['requests'] == 0:
        usage['requests'] = 1
    usage['total_tokens'] = usage['input_tokens'] + usage['output_tokens']
    return usage


def _safe_activity_name(value: object, *, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = ''.join(character for character in value if character.isalnum() or character in '_.:-')[:80]
    return normalized or fallback


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        return int(listener.getsockname()[1])


def _process_group_options() -> dict[str, object]:
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == 'nt':
        process.terminate()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            process.kill()
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
