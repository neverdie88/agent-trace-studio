"""Shared structured results and immutable-workspace gates for selected agents."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ValidationError

from agent_trace_studio.agent_backend import AgentProgress, AgentProgressCallback, AgentRole, AgentTransportError
from agent_trace_studio.qa import QAUnavailableError, StudioAgentCancelled, TraceQAAgentResult
from agent_trace_studio.workspace import clone_workspace, snapshot_workspace

_RESULT_CORRECTIONS = 2


def structured_prompt(instructions: str, evidence: str, output_type: type[BaseModel]) -> str:
    schema = json.dumps(output_type.model_json_schema(), separators=(',', ':'), sort_keys=True)
    return (
        f'{instructions}\n\n'
        'Return exactly one JSON object matching this output schema. Do not wrap it in Markdown.\n'
        f'<output_schema>{schema}</output_schema>\n\n'
        f'{evidence}'
    )


def run_structured_role[Output: BaseModel](
    *,
    prompt: str,
    output_type: type[Output],
    run_turn: Callable[[str], TraceQAAgentResult],
    role: AgentRole,
    backend_label: str,
    progress: AgentProgressCallback | None = None,
) -> Output:
    """Validate native-agent results without exposing rejected model text in errors."""

    current_prompt = prompt
    for correction in range(_RESULT_CORRECTIONS + 1):
        if progress is not None:
            progress(AgentProgress('Agent', f'{backend_label} is running the {role} step.'))
        try:
            raw = run_turn(current_prompt)
        except StudioAgentCancelled:
            raise
        except QAUnavailableError as exc:
            raise AgentTransportError(kind='harness_error', retries=0, role=role) from exc
        value = raw.answer.strip()
        if value.startswith('```') and value.endswith('```'):
            value = value.partition('\n')[2].rsplit('```', 1)[0].strip()
        try:
            result = output_type.model_validate(json.loads(value))
        except json.JSONDecodeError:
            issues = 'The response was not a single valid JSON object.'
        except ValidationError as exc:
            issues = '; '.join(
                f'{".".join(str(part) for part in item["loc"])}: {item["type"]}'
                for item in exc.errors(include_input=False, include_url=False)
            )
        else:
            if progress is not None:
                progress(AgentProgress('Agent', f'{backend_label} completed the {role} step.'))
            return result
        if correction == _RESULT_CORRECTIONS:
            raise AgentTransportError(kind='invalid_response', retries=correction, role=role)
        if progress is not None:
            progress(AgentProgress('Validation', f'{backend_label} is correcting the structured {role} result.'))
        # Retain the original evidence/schema. Never echo untrusted invalid
        # output (which could contain private content) into status or prompts.
        current_prompt = f'{prompt}\n\nThe previous response did not validate. Correct these fields: {issues}'
    raise AssertionError('unreachable structured-output loop')


def _review_fingerprint(workspace: Path) -> tuple[object, tuple[tuple[str, str, int], ...]]:
    source = snapshot_workspace(workspace)
    control = workspace / '.agent-trace-studio'
    if control.is_symlink():
        raise QAUnavailableError('Review artifacts must not be a symbolic link.')
    artifacts = []
    for path in sorted(control.rglob('*')):
        if path.is_symlink():
            raise QAUnavailableError('Review artifacts must not contain symbolic links.')
        if path.is_dir():
            continue
        if not path.is_file():
            raise QAUnavailableError('Review artifacts must be regular files.')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        artifacts.append((path.relative_to(control).as_posix(), digest, stat.S_IMODE(path.stat().st_mode)))
    return source.files, tuple(artifacts)


@contextmanager
def immutable_review_workspace(workspace: Path) -> Iterator[None]:
    """Reject a reviewer that changes either source or host-confirmed artifacts."""

    before = _review_fingerprint(workspace)
    try:
        yield
    finally:
        if _review_fingerprint(workspace) != before:
            raise QAUnavailableError('Read-only review changed the workspace or verification artifacts; refusing it.')


@contextmanager
def isolated_review_workspace(workspace: Path) -> Iterator[Path]:
    """Keep runtimes with Git-root-expanded reads away from the live checkout."""

    for path in workspace.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise QAUnavailableError('Native review requires regular files without symbolic links.')
    with tempfile.TemporaryDirectory(prefix='agent-trace-review-copy-') as directory:
        mirror = Path(directory).resolve() / 'source'
        # OpenCode permits reads in an enclosing Git worktree even when its
        # external_directory permission is denied. Do not rely on cwd alone.
        if any((parent / '.git').exists() for parent in mirror.parents):
            raise QAUnavailableError('Cannot isolate the native review outside the Git workspace.')
        clone_workspace(workspace, mirror)
        control = workspace / '.agent-trace-studio'
        if control.exists():
            shutil.copytree(control, mirror / '.agent-trace-studio')
        if _review_fingerprint(mirror) != _review_fingerprint(workspace):
            raise QAUnavailableError('The isolated review copy does not match the candidate.')
        with immutable_review_workspace(mirror):
            yield mirror


def opencode_managed_paths() -> tuple[Path, ...]:
    """Policy locations used by the supported stable OpenCode release."""

    if sys.platform == 'darwin':
        import pwd

        username = pwd.getpwuid(os.getuid()).pw_name
        root = Path('/Library/Application Support/opencode')
        preferences = Path('/Library/Managed Preferences')
        extra = (
            preferences / username / 'ai.opencode.managed.plist',
            preferences / 'ai.opencode.managed.plist',
        )
    elif sys.platform == 'win32':
        # The review subprocess does not inherit ProgramData overrides.
        root = Path('C:/ProgramData/opencode')
        extra = ()
    else:
        root = Path('/etc/opencode')
        extra = ()
    return (root / 'opencode.json', root / 'opencode.jsonc', *extra)


def opencode_review_preflight(*, native_home: Path, data_dir: Path, managed_paths: Sequence[Path]) -> None:
    """Refuse config that could change remotely between inspection and a turn.

    Only local type/account-presence metadata is inspected. Credential values
    are never returned, logged, copied, or added to subprocess environments.
    """

    if any(path.exists() or path.is_symlink() for path in managed_paths):
        raise QAUnavailableError('OpenCode managed configuration cannot be isolated for read-only review.')
    legacy = native_home / '.opencode'
    caches = {'bin', 'node_modules', 'package.json', 'package-lock.json', 'bun.lock', 'bun.lockb', '.gitignore'}
    if legacy.is_symlink() or (
        legacy.is_dir() and any(child.is_symlink() or child.name not in caches for child in legacy.iterdir())
    ):
        raise QAUnavailableError('OpenCode read-only review cannot isolate home-directory custom configuration.')
    auth = data_dir / 'auth.json'
    if auth.is_symlink() or (auth.exists() and not auth.is_file()):
        raise QAUnavailableError('OpenCode authentication metadata cannot be safely checked for review.')
    if auth.exists():
        try:
            entries = json.loads(auth.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise QAUnavailableError('OpenCode authentication metadata cannot be checked for review.') from exc
        if not isinstance(entries, dict) or any(
            not isinstance(entry, dict) or entry.get('type') not in {'api', 'oauth'} for entry in entries.values()
        ):
            raise QAUnavailableError('OpenCode remote or unknown configuration cannot be isolated for review.')
    # The stable release uses opencode.db. Refuse unexpected/channel databases
    # so an alternate account store cannot bypass the account-presence gate.
    databases = list(data_dir.glob('opencode*.db'))
    if any(path.name != 'opencode.db' for path in databases):
        raise QAUnavailableError('OpenCode channel-specific account configuration cannot be isolated for review.')
    for database in databases:
        if database.is_symlink() or not database.is_file():
            raise QAUnavailableError('OpenCode account metadata cannot be safely checked for review.')
        connection = None
        try:
            connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
            connection.execute('PRAGMA query_only=ON')
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('account', 'control_account', 'account_state')"
                )
            }
            if not tables.intersection({'account', 'control_account'}):
                raise QAUnavailableError('OpenCode account metadata schema is unavailable for safe review.')
            for table in ('account', 'control_account'):
                if table in tables and connection.execute(f'SELECT EXISTS(SELECT 1 FROM {table})').fetchone()[0]:
                    raise QAUnavailableError(
                        'OpenCode account or organization configuration cannot be isolated for review.'
                    )
            if (
                'account_state' in tables
                and connection.execute(
                    'SELECT EXISTS(SELECT 1 FROM account_state '
                    'WHERE active_account_id IS NOT NULL OR active_org_id IS NOT NULL)'
                ).fetchone()[0]
            ):
                raise QAUnavailableError('OpenCode active account configuration cannot be isolated for review.')
        except sqlite3.Error as exc:
            raise QAUnavailableError('OpenCode account metadata cannot be checked for safe review.') from exc
        finally:
            if connection is not None:
                connection.close()
