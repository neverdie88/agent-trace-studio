"""Cross-platform local supervisor for verified Agent Trace Studio updates."""

from __future__ import annotations

import hmac
import http.client
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import BinaryIO, cast

from agent_trace_studio.workspace import FileRecord, restore_workspace_backup

SUPERVISOR_URL_ENV = 'AGENT_TRACE_STUDIO_SUPERVISOR_URL'
SUPERVISOR_TOKEN_ENV = 'AGENT_TRACE_STUDIO_SUPERVISOR_TOKEN'
SUPERVISOR_GENERATION_ENV = 'AGENT_TRACE_STUDIO_DEPLOYMENT_GENERATION'
SUPERVISOR_PUBLIC_URL_ENV = 'AGENT_TRACE_STUDIO_PUBLIC_URL'

_STATUS_PATH = '/_agent_trace_supervisor/status'
_ACTIVATE_PATH = '/_agent_trace_supervisor/activate'
_PUBLISH_PATH = '/_agent_trace_supervisor/publish'
_HOP_HEADERS = frozenset(
    {
        'connection',
        'keep-alive',
        'proxy-authenticate',
        'proxy-authorization',
        'te',
        'trailer',
        'transfer-encoding',
        'upgrade',
    }
)
_MAX_CONTROL_BYTES = 1024 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat()


def child_deployment_context() -> dict[str, object]:
    """Return non-secret deployment identity for child health responses."""

    supervisor_url = os.environ.get(SUPERVISOR_URL_ENV, '').strip()
    return {
        'supervised': bool(supervisor_url),
        'generation': os.environ.get(SUPERVISOR_GENERATION_ENV, '').strip(),
        'public_url': os.environ.get(SUPERVISOR_PUBLIC_URL_ENV, '').strip(),
        'runtime_source_root': str(Path(__file__).resolve().parents[2]),
    }


class SupervisorClient:
    """Authenticated child-to-parent control client."""

    def __init__(self, *, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip('/')
        self._token = token

    @classmethod
    def from_environment(cls) -> SupervisorClient | None:
        base_url = os.environ.get(SUPERVISOR_URL_ENV, '').strip()
        token = os.environ.get(SUPERVISOR_TOKEN_ENV, '')
        if not base_url or not token:
            return None
        return cls(base_url=base_url, token=token)

    def authorize(self, authorization: str | None) -> bool:
        if not authorization:
            return False
        scheme, _, supplied = authorization.partition(' ')
        return scheme.lower() == 'bearer' and hmac.compare_digest(supplied, self._token)

    def status(self) -> dict[str, object]:
        request = urllib.request.Request(f'{self.base_url}{_STATUS_PATH}')
        return _request_json(request, timeout=2)

    def activate(self, payload: dict[str, object]) -> dict[str, object]:
        request = urllib.request.Request(
            f'{self.base_url}{_ACTIVATE_PATH}',
            data=json.dumps(payload, separators=(',', ':')).encode('utf-8'),
            headers={
                'Authorization': f'Bearer {self._token}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        return _request_json(request, timeout=90)

    def publish(self) -> dict[str, object]:
        request = urllib.request.Request(
            f'{self.base_url}{_PUBLISH_PATH}',
            data=b'{}',
            headers={
                'Authorization': f'Bearer {self._token}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        return _request_json(request, timeout=10)


@dataclass(frozen=True)
class SupervisorLaunchSpec:
    child_args: tuple[str, ...]
    output_dir: Path
    state_dir: Path
    agent_state_dir: Path
    live_state_dir: Path
    source_workspace: Path | None
    cwd: Path
    python: Path = field(default_factory=lambda: Path(sys.executable).absolute())
    environment: tuple[tuple[str, str], ...] = ()

    def command(self, *, generation: str, port: int, output_dir: Path) -> list[str]:
        arguments = _rewrite_child_arguments(
            self.child_args,
            port=port,
            output_dir=output_dir,
            agent_state_dir=self.agent_state_dir,
            live_state_dir=self.live_state_dir,
        )
        return [str(self.python), '-m', 'agent_trace_studio', *arguments]


@dataclass
class ChildRuntime:
    generation: str
    port: int
    output_dir: Path
    log_path: Path
    process: subprocess.Popen[bytes]
    log_stream: BinaryIO
    started_at: str
    run_id: str = ''
    activation_id: str = ''
    rollback_manifest: dict[str, object] | None = None

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def public_status(self) -> dict[str, object]:
        return {
            'generation': self.generation,
            'pid': self.process.pid,
            'port': self.port,
            'alive': self.alive,
            'started_at': self.started_at,
            'run_id': self.run_id or None,
            'activation_id': self.activation_id or None,
        }


class DashboardSupervisor:
    """Own the public endpoint and promote only healthy child runtimes."""

    def __init__(
        self,
        spec: SupervisorLaunchSpec,
        *,
        public_port: int,
        health_timeout: float = 45,
    ) -> None:
        self.spec = spec
        self.public_port = public_port
        self.public_url = f'http://127.0.0.1:{public_port}'
        self.health_timeout = max(health_timeout, 5)
        self.token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._deployment_lock = threading.Lock()
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None
        self._active: ChildRuntime | None = None
        self._standby: ChildRuntime | None = None
        self._last_deployment: dict[str, object] | None = None
        self._activations: dict[str, dict[str, object]] = {}
        self.spec.state_dir.mkdir(parents=True, exist_ok=True)
        (self.spec.state_dir / 'generations').mkdir(exist_ok=True)
        (self.spec.state_dir / 'logs').mkdir(exist_ok=True)

    def start_initial(self) -> dict[str, object]:
        runtime = self._start_child(run_id='initial')
        health = self._wait_for_health(runtime)
        if not health.get('passed'):
            self._stop_child(runtime)
            raise RuntimeError(str(health.get('message') or 'initial dashboard failed health checks'))
        with self._lock:
            self._active = runtime
            self._last_deployment = {
                'status': 'promoted',
                'run_id': 'initial',
                'generation': runtime.generation,
                'message': 'Initial dashboard runtime passed health checks.',
                'health': health,
                'updated_at': _now(),
            }
        self._publish_output(runtime)
        self._watchdog = threading.Thread(target=self._watchdog_loop, name='agent-trace-watchdog', daemon=True)
        self._watchdog.start()
        return cast('dict[str, object]', self._last_deployment)

    def close(self) -> None:
        self._stop.set()
        if self._watchdog is not None:
            self._watchdog.join(timeout=3)
        with self._lock:
            runtimes = tuple(item for item in (self._active, self._standby) if item is not None)
            self._active = None
            self._standby = None
        for runtime in runtimes:
            self._stop_child(runtime)

    def active_target(self) -> tuple[str, int] | None:
        with self._lock:
            active = self._active
        if active is None:
            return None
        if not active.alive:
            self._rollback_active('The active dashboard process exited unexpectedly.')
            with self._lock:
                active = self._active
        return ('127.0.0.1', active.port) if active is not None and active.alive else None

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                'supervised': True,
                'status': 'healthy' if self._active is not None and self._active.alive else 'unavailable',
                'public_url': f'{self.public_url}/',
                'active': self._active.public_status() if self._active is not None else None,
                'standby': self._standby.public_status() if self._standby is not None else None,
                'last_deployment': dict(self._last_deployment) if self._last_deployment is not None else None,
            }

    def activate(self, payload: dict[str, object]) -> dict[str, object]:
        run_id = _validated_run_id(payload.get('run_id'))
        change_digest = _required_text(payload, 'change_digest', limit=128)
        activation_id = f'{run_id}:{change_digest}'
        with self._deployment_lock:
            existing = self._activations.get(activation_id)
            if existing is not None and existing.get('status') == 'promoted':
                with self._lock:
                    active = self._active
                if (
                    active is not None
                    and active.alive
                    and active.generation == existing.get('generation')
                    and active.activation_id == activation_id
                ):
                    return dict(existing)
                self._activations.pop(activation_id, None)
            elif existing is not None and existing.get('status') in {'failed', 'rolled_back'}:
                return dict(existing)
            manifest = self._validated_rollback_manifest(payload)
            with self._lock:
                stale_standby = self._standby
                self._standby = None
                previous = self._active
            if stale_standby is not None:
                self._stop_child(stale_standby)
            candidate = self._start_child(
                run_id=run_id,
                activation_id=activation_id,
                rollback_manifest=manifest,
            )
            health = self._wait_for_health(candidate)
            if not health.get('passed'):
                self._stop_child(candidate)
                rollback = self._restore_source(manifest)
                deployment = {
                    'status': 'failed',
                    'run_id': run_id,
                    'generation': candidate.generation,
                    'previous_generation': previous.generation if previous is not None else None,
                    'message': str(health.get('message') or 'Candidate runtime failed health checks.'),
                    'health': health,
                    'rollback': rollback,
                    'updated_at': _now(),
                }
                with self._lock:
                    self._last_deployment = deployment
                    self._activations[activation_id] = deployment
                return dict(deployment)

            with self._lock:
                self._active = candidate
                self._standby = previous
                deployment = {
                    'status': 'promoted',
                    'run_id': run_id,
                    'generation': candidate.generation,
                    'previous_generation': previous.generation if previous is not None else None,
                    'message': 'Candidate runtime passed health checks and is now active.',
                    'health': health,
                    'rollback': {'status': 'available' if previous is not None else 'unavailable'},
                    'updated_at': _now(),
                }
                self._last_deployment = deployment
                self._activations[activation_id] = deployment
            self._publish_output(candidate)
            return dict(deployment)

    def publish_active(self) -> dict[str, object]:
        with self._lock:
            active = self._active
        if active is None or not active.alive:
            raise RuntimeError('active dashboard runtime is unavailable')
        self._publish_output(active)
        return {
            'status': 'published',
            'generation': active.generation,
            'message': 'Active generated dashboard copied to the configured output directory.',
        }

    def _start_child(
        self,
        *,
        run_id: str,
        activation_id: str = '',
        rollback_manifest: dict[str, object] | None = None,
    ) -> ChildRuntime:
        generation = f'{int(time.time())}-{secrets.token_hex(4)}'
        port = _free_loopback_port()
        output_dir = self.spec.state_dir / 'generations' / generation / 'dashboard'
        output_dir.mkdir(parents=True, exist_ok=False)
        log_path = self.spec.state_dir / 'logs' / f'{generation}.log'
        log_stream = log_path.open('ab', buffering=0)
        environment = os.environ.copy()
        environment.update(dict(self.spec.environment))
        environment.update(
            {
                SUPERVISOR_URL_ENV: self.public_url,
                SUPERVISOR_TOKEN_ENV: self.token,
                SUPERVISOR_GENERATION_ENV: generation,
                SUPERVISOR_PUBLIC_URL_ENV: f'{self.public_url}/',
                'PYTHONUNBUFFERED': '1',
            }
        )
        command = self.spec.command(generation=generation, port=port, output_dir=output_dir)
        options: dict[str, object] = {
            'cwd': self.spec.cwd,
            'env': environment,
            'stdin': subprocess.DEVNULL,
            'stdout': log_stream,
            'stderr': subprocess.STDOUT,
        }
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            options['start_new_session'] = True
        try:
            process = subprocess.Popen(command, **options)  # type: ignore[arg-type]
        except BaseException:
            log_stream.close()
            raise
        return ChildRuntime(
            generation=generation,
            port=port,
            output_dir=output_dir,
            log_path=log_path,
            process=process,
            log_stream=log_stream,
            started_at=_now(),
            run_id=run_id,
            activation_id=activation_id,
            rollback_manifest=rollback_manifest,
        )

    def _wait_for_health(self, runtime: ChildRuntime) -> dict[str, object]:
        deadline = time.monotonic() + self.health_timeout
        latest_error = 'Candidate did not answer health checks.'
        while time.monotonic() < deadline:
            if not runtime.alive:
                latest_error = f'Candidate exited with code {runtime.process.returncode}.'
                break
            try:
                status = _child_json(runtime.port, '/api/status', timeout=2)
                deployment = status.get('deployment')
                context = deployment if isinstance(deployment, dict) else {}
                if context.get('generation') != runtime.generation:
                    raise RuntimeError('candidate generation identity did not match')
                if self.spec.source_workspace is not None:
                    runtime_root = Path(str(context.get('runtime_source_root') or '')).resolve()
                    if runtime_root != self.spec.source_workspace.resolve():
                        raise RuntimeError('candidate did not load the configured source workspace')
                payload = _child_json(runtime.port, '/api/payload', timeout=3)
                if not isinstance(payload.get('traces'), list):
                    raise RuntimeError('candidate payload did not contain traces')
                dashboard_prefix = _child_prefix(runtime.port, '/', timeout=3, limit=64 * 1024)
                if b'Agent Trace Studio' not in dashboard_prefix:
                    raise RuntimeError('candidate dashboard did not contain the product shell')
                return {
                    'passed': True,
                    'message': 'Process, status, parser payload, and dashboard checks passed.',
                    'sessions': status.get('sessions'),
                    'turns': status.get('turns'),
                }
            except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
                latest_error = str(exc)
                time.sleep(0.25)
        return {
            'passed': False,
            'message': latest_error,
            'log_tail': _log_tail(runtime.log_path),
        }

    def _watchdog_loop(self) -> None:
        failures = 0
        generation = ''
        while not self._stop.wait(2):
            with self._lock:
                active = self._active
            if active is None:
                continue
            if active.generation != generation:
                generation = active.generation
                failures = 0
            try:
                if not active.alive:
                    raise RuntimeError('active process exited')
                status = _child_json(active.port, '/api/status', timeout=2)
                deployment = status.get('deployment')
                context = deployment if isinstance(deployment, dict) else {}
                if context.get('generation') != active.generation:
                    raise RuntimeError('active generation identity changed')
            except (OSError, ValueError, RuntimeError, urllib.error.URLError):
                failures += 1
            else:
                failures = 0
            if failures >= 3:
                self._rollback_active('The promoted runtime failed three consecutive health checks.')
                failures = 0

    def _rollback_active(self, reason: str) -> None:
        with self._deployment_lock:
            with self._lock:
                failed = self._active
                standby = self._standby
            if failed is None or standby is None or not standby.alive:
                return
            self._stop_child(failed)
            rollback = self._restore_source(failed.rollback_manifest)
            deployment = {
                'status': 'rolled_back',
                'run_id': failed.run_id,
                'generation': failed.generation,
                'previous_generation': standby.generation,
                'message': reason,
                'rollback': rollback,
                'updated_at': _now(),
            }
            with self._lock:
                self._active = standby
                self._standby = None
                self._last_deployment = deployment
                if failed.activation_id:
                    self._activations[failed.activation_id] = deployment
            self._notify_deployment(standby, failed.run_id, deployment)

    def _validated_rollback_manifest(self, payload: dict[str, object]) -> dict[str, object]:
        if self.spec.source_workspace is None:
            raise ValueError('supervisor has no source workspace for verified activation')
        manifest = payload.get('rollback_manifest')
        if not isinstance(manifest, dict):
            raise ValueError('rollback_manifest is required')
        backup_path = Path(_required_text(manifest, 'backup_path', limit=4096)).expanduser().resolve()
        backup_root = (self.spec.agent_state_dir / 'backups').resolve()
        if not backup_path.is_relative_to(backup_root):
            raise ValueError('rollback backup is outside the supervisor state boundary')
        delta = manifest.get('source_delta')
        records = manifest.get('applied_records')
        if not isinstance(delta, dict) or not isinstance(records, dict):
            raise ValueError('rollback manifest is incomplete')
        normalized_delta = {key: _validated_relative_paths(delta.get(key)) for key in ('added', 'modified', 'deleted')}
        changed = set().union(*normalized_delta.values())
        if not changed:
            raise ValueError('rollback manifest has no changed files')
        normalized_records: dict[str, dict[str, object]] = {}
        for key in (*normalized_delta['added'], *normalized_delta['modified']):
            value = records.get(key)
            if not isinstance(value, dict):
                raise ValueError(f'rollback manifest is missing applied record: {key}')
            normalized_records[key] = {
                'digest': _required_text(value, 'digest', limit=128),
                'size': _required_nonnegative_int(value.get('size'), 'size'),
                'mode': _required_nonnegative_int(value.get('mode'), 'mode'),
                'kind': _required_text(value, 'kind', limit=20),
            }
        return {
            'backup_path': str(backup_path),
            'source_delta': {key: list(value) for key, value in normalized_delta.items()},
            'applied_records': normalized_records,
        }

    def _restore_source(self, manifest: dict[str, object] | None) -> dict[str, object]:
        if manifest is None or self.spec.source_workspace is None:
            return {'status': 'unavailable', 'message': 'No rollback manifest was available.'}
        delta = cast('dict[str, list[str]]', manifest['source_delta'])
        raw_records = cast('dict[str, dict[str, object]]', manifest['applied_records'])
        records = {
            key: FileRecord(
                digest=str(value['digest']),
                size=int(value['size']),
                mode=int(value['mode']),
                kind=str(value['kind']),
            )
            for key, value in raw_records.items()
        }
        try:
            restore_workspace_backup(
                target=self.spec.source_workspace,
                backup_dir=Path(str(manifest['backup_path'])),
                added=tuple(delta['added']),
                modified=tuple(delta['modified']),
                deleted=tuple(delta['deleted']),
                applied_records=records,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return {'status': 'blocked', 'message': str(exc)}
        return {'status': 'restored', 'message': 'Previous source snapshot restored from verified backup.'}

    def _notify_deployment(
        self,
        runtime: ChildRuntime,
        run_id: str,
        deployment: dict[str, object],
    ) -> None:
        if not runtime.alive or not run_id:
            return
        request = urllib.request.Request(
            f'http://127.0.0.1:{runtime.port}/api/runtime/deployment',
            data=json.dumps({'run_id': run_id, 'deployment': deployment}).encode('utf-8'),
            headers={
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        with suppress(OSError, ValueError, urllib.error.URLError):
            _request_json(request, timeout=5)

    def _publish_output(self, runtime: ChildRuntime) -> None:
        self.spec.output_dir.mkdir(parents=True, exist_ok=True)
        for source in runtime.output_dir.iterdir():
            if source.is_file() and not source.is_symlink():
                shutil.copy2(source, self.spec.output_dir / source.name)

    @staticmethod
    def _stop_child(runtime: ChildRuntime) -> None:
        if runtime.alive:
            runtime.process.terminate()
            try:
                runtime.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runtime.process.kill()
                runtime.process.wait(timeout=5)
        runtime.log_stream.close()


class SupervisorProxyHandler(BaseHTTPRequestHandler):
    """Stable loopback reverse proxy and authenticated supervisor control surface."""

    server_version = 'AgentTraceSupervisor/1.0'

    def __init__(self, *args: object, supervisor: DashboardSupervisor, **kwargs: object) -> None:
        self.supervisor = supervisor
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        if self.path.split('?', 1)[0] == _STATUS_PATH:
            self._send_json(HTTPStatus.OK, self.supervisor.status())
            return
        self._proxy()

    def do_HEAD(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        route = self.path.split('?', 1)[0]
        if route in {_ACTIVATE_PATH, _PUBLISH_PATH}:
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {'error': 'supervisor authorization failed'})
                return
            try:
                if route == _ACTIVATE_PATH:
                    payload = self._read_json(_MAX_CONTROL_BYTES)
                    response = self.supervisor.activate(payload)
                else:
                    self._read_json(_MAX_CONTROL_BYTES)
                    response = self.supervisor.publish_active()
            except (OSError, RuntimeError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {'error': str(exc)})
                return
            self._send_json(HTTPStatus.OK, response)
            return
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def _proxy(self) -> None:
        target = self.supervisor.active_target()
        if target is None:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {'error': 'dashboard runtime is unavailable'})
            return
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_HEADERS and key.lower() not in {'host', 'content-length'}
        }
        if body is not None:
            headers['Content-Length'] = str(len(body))
        headers['Host'] = f'{target[0]}:{target[1]}'
        headers['X-Forwarded-Host'] = self.headers.get('Host', '')
        connection = http.client.HTTPConnection(*target, timeout=120)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            content_type = response.getheader('Content-Type', '')
            for key, value in response.getheaders():
                if key.lower() not in _HOP_HEADERS:
                    self.send_header(key, value)
            self.end_headers()
            if self.command == 'HEAD':
                return
            if content_type.startswith('text/event-stream'):
                while True:
                    line = response.fp.readline() if response.fp is not None else b''
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
            else:
                while chunk := response.read(64 * 1024):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return
        except OSError as exc:
            if not self.wfile.closed:
                with suppress(OSError):
                    self._send_json(HTTPStatus.BAD_GATEWAY, {'error': f'dashboard proxy failed: {exc}'})
        finally:
            connection.close()

    def _authorized(self) -> bool:
        authorization = self.headers.get('Authorization')
        if not authorization:
            return False
        scheme, _, supplied = authorization.partition(' ')
        return scheme.lower() == 'bearer' and hmac.compare_digest(supplied, self.supervisor.token)

    def _read_json(self, limit: int) -> dict[str, object]:
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0 or length > limit:
            raise ValueError('invalid supervisor request size')
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError('supervisor request must be a JSON object')
        return cast('dict[str, object]', payload)

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args


def run_supervisor(
    *,
    child_args: tuple[str, ...],
    public_port: int,
    output_dir: Path,
    source_workspace: Path | None,
    health_timeout: float = 45,
) -> int:
    """Run the stable proxy and supervised dashboard child lifecycle."""

    resolved_output = output_dir.expanduser().resolve()
    state_dir = resolved_output.parent / f'.{resolved_output.name}-supervisor'
    spec = SupervisorLaunchSpec(
        child_args=child_args,
        output_dir=resolved_output,
        state_dir=state_dir,
        agent_state_dir=resolved_output.parent / f'.{resolved_output.name}-agent-state',
        live_state_dir=resolved_output.parent / f'.{resolved_output.name}-live-state',
        source_workspace=source_workspace,
        cwd=Path.cwd().resolve(),
    )
    supervisor = DashboardSupervisor(spec, public_port=public_port, health_timeout=health_timeout)
    server = ThreadingHTTPServer(
        ('127.0.0.1', public_port),
        partial(SupervisorProxyHandler, supervisor=supervisor),
    )
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, name='agent-trace-proxy', daemon=True)
    server_thread.start()
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    previous_handlers: dict[int, object] = {}
    handled_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, 'SIGHUP'):
        handled_signals.append(signal.SIGHUP)
    for signum in handled_signals:
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)
    try:
        initial = supervisor.start_initial()
        print(
            json.dumps(
                {
                    'status': 'supervising',
                    'url': f'{supervisor.public_url}/',
                    'generation': initial.get('generation'),
                    'supervisor_state': str(state_dir),
                },
                indent=2,
                sort_keys=True,
            ),
            flush=True,
        )
        while not stop.wait(0.5):
            if not server_thread.is_alive():
                return 1
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        supervisor.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0


def _rewrite_child_arguments(
    arguments: tuple[str, ...],
    *,
    port: int,
    output_dir: Path,
    agent_state_dir: Path,
    live_state_dir: Path,
) -> tuple[str, ...]:
    value_options = {'--port', '--output', '-o', '--agent-state-dir', '--live-state-dir'}
    flag_options = {'--supervise', '--no-supervise', '--supervised-child'}
    rewritten: list[str] = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value in flag_options:
            index += 1
            continue
        if value in value_options:
            index += 2
            continue
        if any(value.startswith(f'{option}=') for option in value_options if option.startswith('--')):
            index += 1
            continue
        rewritten.append(value)
        index += 1
    if '--serve' not in rewritten:
        rewritten.append('--serve')
    rewritten.extend(
        (
            '--supervised-child',
            '--port',
            str(port),
            '--output',
            str(output_dir),
            '--agent-state-dir',
            str(agent_state_dir),
            '--live-state-dir',
            str(live_state_dir),
        )
    )
    return tuple(rewritten)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _child_json(port: int, path: str, *, timeout: float) -> dict[str, object]:
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}')
    return _request_json(request, timeout=timeout)


def _child_prefix(port: int, path: str, *, timeout: float, limit: int) -> bytes:
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(limit)


def _request_json(request: urllib.request.Request, *, timeout: float) -> dict[str, object]:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise ValueError('expected a JSON object')
    return cast('dict[str, object]', payload)


def _log_tail(path: Path, limit: int = 4_000) -> str:
    try:
        content = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    return content[-limit:]


def _validated_run_id(value: object) -> str:
    text = str(value or '')
    if not text or len(text) > 64 or not text.isalnum():
        raise ValueError('invalid deployment run ID')
    return text


def _required_text(payload: dict[str, object], key: str, *, limit: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{key} is required')
    return value.strip()


def _required_nonnegative_int(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'{key} must be a non-negative integer')
    return value


def _validated_relative_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 80:
        raise ValueError('rollback paths must be a bounded list')
    paths: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError('rollback path must be text')
        path = PurePosixPath(item.replace('\\', '/'))
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise ValueError(f'invalid rollback path: {item}')
        paths.append(path.as_posix())
    return tuple(paths)
