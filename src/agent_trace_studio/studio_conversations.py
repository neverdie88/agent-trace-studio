"""Durable, harness-scoped Studio conversation state."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

# The removed backend remains recognizable only for saved-state cleanup.
# It is not exposed by the runnable agent catalog or selected by new requests.
_KNOWN_HARNESSES = ('opencode', 'pydantic', 'codex-sdk', 'google-adk', 'claude-agent-sdk')


class StudioConversationStore:
    """Persist only the state needed to resume one selected agent harness."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self._lock = threading.RLock()

    def native_session_id(self, *, conversation_id: str, session_id: str, harness: str) -> str | None:
        with self._lock:
            payload = self._read_metadata(self._branch(conversation_id, session_id, harness))
        value = payload.get('native_session_id')
        return value if isinstance(value, str) and value else None

    def save_native_session_id(
        self,
        *,
        conversation_id: str,
        session_id: str,
        harness: str,
        native_session_id: str,
    ) -> None:
        if not native_session_id:
            return
        branch = self._branch(conversation_id, session_id, harness)
        with self._lock:
            payload = self._read_metadata(branch)
            payload.update(
                {
                    'version': 1,
                    'conversation_digest': self._conversation_digest(conversation_id),
                    'session_id': session_id,
                    'harness': harness,
                    'native_session_id': native_session_id,
                }
            )
            self._write_json(branch / 'metadata.json', payload)

    def load_messages(self, *, conversation_id: str, session_id: str, harness: str) -> list[ModelMessage]:
        path = self._branch(conversation_id, session_id, harness) / 'messages.json'
        with self._lock:
            if not path.is_file():
                return []
            try:
                return list(ModelMessagesTypeAdapter.validate_json(path.read_bytes()))
            except (OSError, ValueError):
                return []

    def save_messages(
        self,
        *,
        conversation_id: str,
        session_id: str,
        harness: str,
        messages: list[ModelMessage],
    ) -> None:
        branch = self._branch(conversation_id, session_id, harness)
        with self._lock:
            metadata = self._read_metadata(branch)
            metadata.update(
                {
                    'version': 1,
                    'conversation_digest': self._conversation_digest(conversation_id),
                    'session_id': session_id,
                    'harness': harness,
                }
            )
            self._write_json(branch / 'metadata.json', metadata)
            self._write_bytes(branch / 'messages.json', ModelMessagesTypeAdapter.dump_json(messages))

    def clear(self, *, conversation_id: str, session_id: str) -> int:
        removed = 0
        with self._lock:
            for harness in _KNOWN_HARNESSES:
                branch = self._branch(conversation_id, session_id, harness)
                if not branch.exists():
                    continue
                shutil.rmtree(branch)
                removed += 1
        return removed

    def _branch(self, conversation_id: str, session_id: str, harness: str) -> Path:
        if harness not in _KNOWN_HARNESSES:
            raise ValueError('unknown Studio conversation harness')
        key = f'{conversation_id}\0{session_id}\0{harness}'.encode()
        return self.root / hashlib.sha256(key).hexdigest()

    @staticmethod
    def _conversation_digest(conversation_id: str) -> str:
        return hashlib.sha256(conversation_id.encode()).hexdigest()

    @staticmethod
    def _read_metadata(branch: Path) -> dict[str, object]:
        path = branch / 'metadata.json'
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _write_json(path: Path, value: dict[str, object]) -> None:
        StudioConversationStore._write_bytes(
            path,
            json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode(),
        )

    @staticmethod
    def _write_bytes(path: Path, value: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        temporary.write_bytes(value)
        temporary.chmod(0o600)
        temporary.replace(path)
