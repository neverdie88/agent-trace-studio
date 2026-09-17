"""Cross-platform helpers for live trace registration and ingestion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock


@dataclass(frozen=True)
class FileFingerprint:
    exists: bool
    size: int
    modified_ns: int
    file_id: int


def fingerprint(path: Path) -> FileFingerprint:
    try:
        stat = path.stat()
    except OSError:
        return FileFingerprint(False, 0, 0, 0)
    return FileFingerprint(True, stat.st_size, stat.st_mtime_ns, getattr(stat, 'st_ino', 0))


def default_live_server_path() -> Path:
    configured = os.environ.get('AGENT_TRACE_STUDIO_SERVER_FILE', '').strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / '.agent-trace-studio' / 'live-server.json'


def write_server_descriptor(path: Path, *, url: str, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(
        json.dumps({'url': url.rstrip('/'), 'token': token, 'pid': os.getpid()}, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    with suppress(OSError):
        temporary.chmod(0o600)
    temporary.replace(path)


def remove_server_descriptor(path: Path, *, token: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(payload, dict) and payload.get('token') == token:
        path.unlink(missing_ok=True)


class CanonicalJournalStore:
    """Append normalized streaming events to replayable local JSONL journals."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._active_turns: dict[Path, str] = {}

    def append_event(
        self,
        *,
        adapter: str,
        session_id: str,
        event: dict[str, object],
        cwd: str = '',
        model: str = '',
        turn_id: str = '',
    ) -> Path:
        path = self.path_for(adapter, session_id)
        timestamp = _timestamp(event.get('timestamp'))
        active_turn = turn_id or _text(event.get('turn_id')) or 'run'
        with self._lock:
            is_new = not path.is_file() or path.stat().st_size == 0
            if is_new:
                self._append_rows(
                    path,
                    [
                        {
                            'timestamp': timestamp,
                            'type': 'session_meta',
                            'payload': {
                                'id': session_id,
                                'timestamp': timestamp,
                                'cwd': cwd,
                                'originator': adapter,
                                'source': adapter,
                                'model': model,
                            },
                        },
                        {
                            'timestamp': timestamp,
                            'type': 'turn_context',
                            'payload': {'turn_id': active_turn, 'cwd': cwd, 'model': model},
                        },
                        {
                            'timestamp': timestamp,
                            'type': 'event_msg',
                            'payload': {'type': 'task_started', 'turn_id': active_turn},
                        },
                    ],
                )
            else:
                previous_turn = self._active_turns.get(path)
                if previous_turn is None:
                    previous_turn = self._open_turn(path)
                if previous_turn != active_turn:
                    transition_rows: list[dict[str, object]] = []
                    if previous_turn:
                        transition_rows.append(
                            {
                                'timestamp': timestamp,
                                'type': 'event_msg',
                                'payload': {'type': 'task_complete', 'turn_id': previous_turn},
                            }
                        )
                    transition_rows.extend(
                        [
                            {
                                'timestamp': timestamp,
                                'type': 'turn_context',
                                'payload': {'turn_id': active_turn, 'cwd': cwd, 'model': model},
                            },
                            {
                                'timestamp': timestamp,
                                'type': 'event_msg',
                                'payload': {'type': 'task_started', 'turn_id': active_turn},
                            },
                        ]
                    )
                    self._append_rows(path, transition_rows)
            self._active_turns[path] = active_turn
            normalized = dict(event)
            normalized.pop('timestamp', None)
            normalized.setdefault('turn_id', active_turn)
            self._append_rows(
                path,
                [{'timestamp': timestamp, 'type': 'agent_trace_event', 'payload': normalized}],
            )
        return path

    def finish(
        self,
        *,
        adapter: str,
        session_id: str,
        turn_id: str = '',
        failed: bool = False,
        reason: str = '',
    ) -> Path:
        path = self.path_for(adapter, session_id)
        timestamp = _timestamp(None)
        with self._lock:
            if not path.is_file() or path.stat().st_size == 0:
                return path
            active_turn = turn_id or self._active_turns.pop(path, '') or self._open_turn(path) or 'run'
            self._append_rows(
                path,
                [
                    {
                        'timestamp': timestamp,
                        'type': 'event_msg',
                        'payload': {
                            'type': 'task_aborted' if failed else 'task_complete',
                            'turn_id': active_turn,
                            'reason': reason,
                        },
                    }
                ],
            )
        return path

    def path_for(self, adapter: str, session_id: str) -> Path:
        safe_adapter = re.sub(r'[^A-Za-z0-9_.-]+', '-', adapter).strip('-') or 'agent'
        digest = hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:16]
        return self.root / f'{safe_adapter}-{digest}.jsonl'

    @staticmethod
    def _append_rows(path: Path, rows: list[dict[str, object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as stream:
            for row in rows:
                stream.write(json.dumps(row, separators=(',', ':'), ensure_ascii=True) + '\n')
            stream.flush()

    @staticmethod
    def _open_turn(path: Path) -> str:
        active_turn = ''
        try:
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict) or row.get('type') != 'event_msg':
                        continue
                    payload = row.get('payload')
                    if not isinstance(payload, dict):
                        continue
                    event_type = _text(payload.get('type'))
                    if event_type == 'task_started':
                        active_turn = _text(payload.get('turn_id')) or active_turn
                    elif event_type in {'task_complete', 'task_aborted'}:
                        active_turn = ''
        except OSError:
            return ''
        return active_turn


def send_live_hook_event(payload: dict[str, object], *, descriptor_path: Path | None = None) -> bool:
    """Forward a Codex or Claude lifecycle hook without affecting the agent run."""

    event_name = _text(payload.get('hook_event_name'))
    route = '/api/live/end' if event_name == 'SessionEnd' else '/api/live/register'
    return _post_live_payload(route, payload, descriptor_path)


def publish_live_event(
    *,
    adapter: str,
    session_id: str,
    event: dict[str, object],
    cwd: str = '',
    model: str = '',
    turn_id: str = '',
    descriptor_path: Path | None = None,
) -> bool:
    payload = {
        'adapter': adapter,
        'session_id': session_id,
        'event': event,
        'cwd': cwd,
        'model': model,
        'turn_id': turn_id,
    }
    return _post_live_payload('/api/live/events', payload, descriptor_path)


def finish_live_session(
    *,
    adapter: str,
    session_id: str,
    turn_id: str = '',
    failed: bool = False,
    reason: str = '',
    descriptor_path: Path | None = None,
) -> bool:
    return _post_live_payload(
        '/api/live/end',
        {
            'adapter': adapter,
            'session_id': session_id,
            'turn_id': turn_id,
            'failed': failed,
            'reason': reason,
        },
        descriptor_path,
    )


def _post_live_payload(route: str, payload: dict[str, object], descriptor_path: Path | None) -> bool:
    descriptor = descriptor_path or default_live_server_path()
    try:
        server = json.loads(descriptor.read_text(encoding='utf-8'))
        if not isinstance(server, dict):
            return False
        url = _text(server.get('url')).rstrip('/')
        token = _text(server.get('token'))
        parsed_url = urllib.parse.urlsplit(url)
        if parsed_url.scheme != 'http' or parsed_url.hostname not in {'127.0.0.1', '::1', 'localhost'} or not token:
            return False
        request = urllib.request.Request(
            f'{url}{route}',
            data=json.dumps(payload, separators=(',', ':'), ensure_ascii=True).encode('utf-8'),
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _timestamp(value: object) -> str:
    text = _text(value)
    return text or datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ''
