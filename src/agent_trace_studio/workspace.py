"""Shadow workspace and transactional local-source updates."""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

_IGNORED_ROOTS = frozenset(
    {
        '.agent-trace-studio',
        '.git',
        '.mypy_cache',
        '.pytest_cache',
        '.ruff_cache',
        '.venv',
        'build',
        'dashboard',
        'dist',
    }
)
_IGNORED_PARTS = frozenset({'__pycache__'})
_IGNORED_SUFFIXES = frozenset({'.pyc', '.pyo'})
_IGNORED_DIRECTORY_SUFFIXES = ('.egg-info',)
_MAX_CHANGED_FILES = 80
_MAX_CHANGED_BYTES = 10 * 1024 * 1024
_PROTECTED_AGENT_PATHS = frozenset(
    {
        'AGENTS.md',
        'src/agent_trace_studio/__init__.py',
        'src/agent_trace_studio/__main__.py',
        'src/agent_trace_studio/agent_control.py',
        'src/agent_trace_studio/agent_backend.py',
        'src/agent_trace_studio/adk_backend.py',
        'src/agent_trace_studio/assets/codex_turn.mjs',
        'src/agent_trace_studio/assets/codex_review.mjs',
        'src/agent_trace_studio/cli.py',
        'src/agent_trace_studio/credentials.py',
        'src/agent_trace_studio/harness_backend.py',
        'src/agent_trace_studio/qa.py',
        'src/agent_trace_studio/repair.py',
        'src/agent_trace_studio/server.py',
        'src/agent_trace_studio/supervisor.py',
        'src/agent_trace_studio/workspace.py',
        'src/agent_trace_studio/workflow_roles.py',
    }
)


@dataclass(frozen=True)
class FileRecord:
    digest: str
    size: int
    mode: int
    kind: str = 'file'


@dataclass(frozen=True)
class WorkspaceSnapshot:
    root: Path
    files: dict[str, FileRecord]


@dataclass(frozen=True)
class WorkspaceDelta:
    added: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]
    digest: str
    changed_bytes: int

    @property
    def changed_files(self) -> tuple[str, ...]:
        return (*self.added, *self.modified, *self.deleted)

    @property
    def empty(self) -> bool:
        return not self.changed_files


class WorkspacePolicyViolation(ValueError):
    """A candidate delta that the host must reject before running project code."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        changed_files: tuple[str, ...],
        blocked_files: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.category = category
        self.changed_files = changed_files
        self.blocked_files = blocked_files


def validate_source_workspace(path: Path) -> Path:
    """Require the expected Agent Trace Studio source boundary."""

    root = path.expanduser().resolve()
    required = (
        root / 'pyproject.toml',
        root / 'src/agent_trace_studio/parser.py',
        root / 'tests',
        root / 'AGENTS.md',
    )
    if not root.is_dir() or not all(item.exists() for item in required):
        raise ValueError('source workspace must be an Agent Trace Studio checkout')
    return root


def clone_workspace(source: Path, destination: Path) -> None:
    """Copy tracked-like source files without runtime and generated directories."""

    if destination.exists():
        raise ValueError(f'shadow workspace already exists: {destination}')
    shutil.copytree(source, destination, symlinks=True, ignore=_copy_ignore)


def snapshot_workspace(root: Path) -> WorkspaceSnapshot:
    resolved = root.resolve()
    files: dict[str, FileRecord] = {}
    for path in sorted(resolved.rglob('*')):
        relative = path.relative_to(resolved)
        if _ignored(relative) or path.is_dir():
            continue
        key = relative.as_posix()
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            target = str(path.readlink())
            files[key] = FileRecord(
                digest=hashlib.sha256(target.encode('utf-8')).hexdigest(),
                size=len(target),
                mode=mode,
                kind='symlink',
            )
            continue
        content = path.read_bytes()
        files[key] = FileRecord(
            digest=hashlib.sha256(content).hexdigest(),
            size=len(content),
            mode=mode,
        )
    return WorkspaceSnapshot(root=resolved, files=files)


def compare_workspaces(baseline: WorkspaceSnapshot, candidate: WorkspaceSnapshot) -> WorkspaceDelta:
    baseline_keys = set(baseline.files)
    candidate_keys = set(candidate.files)
    added = tuple(sorted(candidate_keys - baseline_keys))
    deleted = tuple(sorted(baseline_keys - candidate_keys))
    modified = tuple(
        sorted(key for key in baseline_keys & candidate_keys if baseline.files[key] != candidate.files[key])
    )
    changed_files = (*added, *modified, *deleted)
    protected = sorted(set(changed_files) & _PROTECTED_AGENT_PATHS)
    if protected:
        raise WorkspacePolicyViolation(
            f'agent changes protected runtime file: {protected[0]}',
            category='protected_runtime_file',
            changed_files=changed_files,
            blocked_files=tuple(protected),
        )
    for key in changed_files:
        record = candidate.files.get(key) or baseline.files[key]
        if record.kind != 'file':
            raise WorkspacePolicyViolation(
                f'symbolic-link changes are not allowed: {key}',
                category='symbolic_link_change',
                changed_files=changed_files,
                blocked_files=(key,),
            )
    changed_bytes = sum(candidate.files[key].size for key in (*added, *modified))
    if len(changed_files) > _MAX_CHANGED_FILES:
        raise WorkspacePolicyViolation(
            f'repair changes {len(changed_files)} files; maximum is {_MAX_CHANGED_FILES}',
            category='changed_file_limit',
            changed_files=changed_files,
        )
    if changed_bytes > _MAX_CHANGED_BYTES:
        raise WorkspacePolicyViolation(
            'repair changes more than 10 MB of source files',
            category='changed_byte_limit',
            changed_files=changed_files,
        )
    digest_source = '\n'.join(
        f'{key}:{(candidate.files.get(key) or baseline.files[key]).digest}' for key in changed_files
    )
    return WorkspaceDelta(
        added=added,
        modified=modified,
        deleted=deleted,
        digest=hashlib.sha256(digest_source.encode('utf-8')).hexdigest(),
        changed_bytes=changed_bytes,
    )


def render_unified_diff(
    baseline: WorkspaceSnapshot,
    candidate: WorkspaceSnapshot,
    delta: WorkspaceDelta,
) -> str:
    """Render the complete text patch supplied to the read-only verifier."""

    sections: list[str] = []
    for key in delta.changed_files:
        before = _read_text_file(baseline.root / key) if key not in delta.added else []
        after = _read_text_file(candidate.root / key) if key not in delta.deleted else []
        if before is None or after is None:
            sections.append(f'Binary file changed: {key}\n')
            continue
        sections.extend(
            difflib.unified_diff(
                before,
                after,
                fromfile=f'a/{key}',
                tofile=f'b/{key}',
                lineterm='',
            )
        )
    return '\n'.join(sections) + ('\n' if sections else '')


class LocalWorkspaceTransaction:
    """Apply a verified delta atomically per file, with conflict checks and rollback."""

    def __init__(
        self,
        *,
        target: Path,
        candidate: WorkspaceSnapshot,
        baseline: WorkspaceSnapshot,
        backup_dir: Path,
    ) -> None:
        self.target = target.resolve()
        self.candidate = candidate
        self.baseline = baseline
        self.backup_dir = backup_dir.resolve()
        self._applied = False
        self._added: tuple[str, ...] = ()

    def apply(self, delta: WorkspaceDelta) -> None:
        current = snapshot_workspace(self.target)
        for key in delta.modified + delta.deleted:
            if current.files.get(key) != self.baseline.files.get(key):
                raise RuntimeError(f'local source changed during verification: {key}')
        for key in delta.added:
            if key in current.files or (self.target / key).exists():
                raise RuntimeError(f'new repair path now exists in local source: {key}')
        self.backup_dir.mkdir(parents=True, exist_ok=False)
        for key in delta.modified + delta.deleted:
            source = self.target / key
            backup = self.backup_dir / key
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup, follow_symlinks=False)
        self._added = delta.added
        try:
            for key in delta.added + delta.modified:
                source = self.candidate.root / key
                destination = self.target / key
                _assert_regular_source(source, self.candidate.root)
                _assert_within(destination, self.target)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _atomic_copy(source, destination)
            for key in delta.deleted:
                destination = self.target / key
                _assert_within(destination, self.target)
                destination.unlink()
            self._applied = True
        except BaseException:
            self._applied = True
            self.rollback()
            raise

    def rollback(self) -> None:
        if not self._applied:
            return
        for key in self._added:
            path = self.target / key
            if path.exists() and not path.is_symlink():
                path.unlink()
        for backup in sorted(self.backup_dir.rglob('*')):
            if not backup.is_file() or backup.is_symlink():
                continue
            relative = backup.relative_to(self.backup_dir)
            destination = self.target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            _atomic_copy(backup, destination)
        self._applied = False

    def commit(self) -> None:
        if not self._applied:
            raise RuntimeError('workspace transaction has not been applied')
        self._applied = False


def restore_workspace_backup(
    *,
    target: Path,
    backup_dir: Path,
    added: tuple[str, ...],
    modified: tuple[str, ...],
    deleted: tuple[str, ...],
    applied_records: dict[str, FileRecord],
) -> None:
    """Restore a committed transaction only when its applied files are unchanged."""

    resolved_target = target.expanduser().resolve()
    resolved_backup = backup_dir.expanduser().resolve()
    current = snapshot_workspace(resolved_target)
    for key in (*added, *modified):
        if current.files.get(key) != applied_records.get(key):
            raise RuntimeError(f'local source changed after deployment apply: {key}')
    for key in deleted:
        if key in current.files or (resolved_target / key).exists():
            raise RuntimeError(f'deleted source path changed after deployment apply: {key}')
    for key in (*modified, *deleted):
        backup = resolved_backup / key
        _assert_regular_source(backup, resolved_backup)
    for key in added:
        path = resolved_target / key
        _assert_within(path, resolved_target)
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f'applied source is not a regular file: {key}')
            path.unlink()
    for key in (*modified, *deleted):
        destination = resolved_target / key
        _assert_within(destination, resolved_target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy(resolved_backup / key, destination)


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    _ = directory
    return {name for name in names if _ignored_name(name)}


def _ignored(relative: Path) -> bool:
    return (
        not relative.parts
        or any(_ignored_name(part) for part in relative.parts)
        or relative.suffix in _IGNORED_SUFFIXES
    )


def _ignored_name(name: str) -> bool:
    return (
        name in _IGNORED_PARTS
        or name in _IGNORED_ROOTS
        or name.endswith(_IGNORED_DIRECTORY_SUFFIXES)
        or (name.startswith('.') and name.endswith('-agent-state'))
        or (name.startswith('.') and name.endswith('-supervisor'))
        or name.endswith(tuple(_IGNORED_SUFFIXES))
    )


def _read_text_file(path: Path) -> list[str] | None:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding='utf-8').splitlines()
    except UnicodeDecodeError:
        return None


def _assert_regular_source(path: Path, root: Path) -> None:
    _assert_within(path, root)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f'repair source is not a regular file: {path.relative_to(root)}')


def _assert_within(path: Path, root: Path) -> None:
    resolved_parent = path.parent.resolve()
    if not resolved_parent.is_relative_to(root.resolve()):
        raise RuntimeError('repair path escapes the source workspace')


def _atomic_copy(source: Path, destination: Path) -> None:
    handle, temporary_name = tempfile.mkstemp(prefix=f'.{destination.name}.', dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(stat.S_IMODE(source.stat().st_mode))
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
