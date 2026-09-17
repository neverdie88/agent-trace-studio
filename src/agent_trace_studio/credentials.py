"""Cross-platform persistent storage for model API credentials."""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

import keyring
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError, PasswordDeleteError

CredentialMode = Literal['system', 'encrypted_vault']

_CONFIG_DIRECTORY_ENV = 'AGENT_TRACE_STUDIO_CONFIG_DIR'
_VAULT_PASSWORD_ENV = 'AGENT_TRACE_STUDIO_VAULT_PASSWORD'
_CONFIG_FILENAME = 'model-api.json'
_CONFIG_VERSION = 2
_KEYRING_SERVICE = 'Agent Trace Studio model API'
_KEYRING_ACCOUNT = 'active-provider'
_VAULT_LABEL = 'encrypted local vault'
_VAULT_PASSWORD_MIN_CHARS = 12
_VAULT_PASSWORD_MAX_CHARS = 1_024
_VAULT_SALT_BYTES = 16
_VAULT_NONCE_BYTES = 12
_VAULT_KDF_LENGTH = 32
_VAULT_KDF_N = 2**15
_VAULT_KDF_R = 8
_VAULT_KDF_P = 1
_NATIVE_BACKEND_MARKERS = ('macos', 'windows', 'winvault', 'secretservice', 'kwallet', 'libsecret')


class CredentialPersistenceError(RuntimeError):
    """Raised when remembered API settings cannot be read or updated safely."""


@dataclass(frozen=True)
class SavedAPIConfiguration:
    provider: str
    model: str
    base_url: str
    api_key: str
    credential_mode: CredentialMode
    locked: bool


class SecretStore(Protocol):
    """Minimal native secret-store contract used by APIConfigStore and tests."""

    @property
    def available(self) -> bool: ...

    @property
    def label(self) -> str: ...

    def get(self) -> str | None: ...

    def set(self, value: str) -> None: ...

    def delete(self) -> None: ...


class SystemSecretStore:
    """Use only recognized operating-system-backed keyring implementations."""

    @property
    def available(self) -> bool:
        return self._backend() is not None

    @property
    def label(self) -> str:
        backend = self._backend()
        return _native_backend_label(backend) if backend is not None else 'system credential store'

    def get(self) -> str | None:
        backend = self._backend()
        if backend is None:
            return None
        try:
            return backend.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except KeyringError as exc:
            raise CredentialPersistenceError(f'Could not read the API key from {self.label}: {exc}') from exc

    def set(self, value: str) -> None:
        backend = self._backend()
        if backend is None:
            raise CredentialPersistenceError('No supported system credential store is available')
        try:
            backend.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, value)
        except KeyringError as exc:
            raise CredentialPersistenceError(f'Could not save the API key in {self.label}: {exc}') from exc

    def delete(self) -> None:
        backend = self._backend()
        if backend is None:
            return
        try:
            if backend.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT) is not None:
                backend.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except PasswordDeleteError:
            return
        except KeyringError as exc:
            raise CredentialPersistenceError(f'Could not remove the API key from {self.label}: {exc}') from exc

    @staticmethod
    def _backend() -> KeyringBackend | None:
        try:
            return _supported_native_backend(keyring.get_keyring())
        except (KeyringError, RuntimeError, TypeError, ValueError):
            return None


class APIConfigStore:
    """Use a native keyring or an authenticated encrypted-file vault."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        secret_store: SecretStore | None = None,
        vault_password: str | None = None,
    ) -> None:
        self.path = (path or default_api_config_path()).expanduser()
        self.secret_store = secret_store if secret_store is not None else SystemSecretStore()
        self._environment_vault_password = os.environ.get(_VAULT_PASSWORD_ENV) or ''
        self._session_vault_password = vault_password or self._environment_vault_password
        self._loaded_mode: CredentialMode | None = None
        self._locked = False

    @property
    def available(self) -> bool:
        return True

    @property
    def native_available(self) -> bool:
        return self.secret_store.available

    @property
    def credential_mode(self) -> CredentialMode:
        return self._loaded_mode or self.save_credential_mode

    @property
    def save_credential_mode(self) -> CredentialMode:
        return 'system' if self.native_available else 'encrypted_vault'

    @property
    def label(self) -> str:
        if self.credential_mode == 'encrypted_vault':
            return _VAULT_LABEL
        if self.native_available:
            return self.secret_store.label
        return 'system credential store (unavailable)'

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def vault_password_required(self) -> bool:
        return self.credential_mode == 'encrypted_vault' and not self._session_vault_password

    @property
    def vault_password_from_environment(self) -> bool:
        return bool(self._environment_vault_password)

    def load(self, *, vault_password: str | None = None) -> SavedAPIConfiguration | None:
        metadata = self._read_metadata()
        if metadata is None:
            self._loaded_mode = None
            self._locked = False
            return None
        credential = cast('dict[str, str]', metadata['credential'])
        mode = cast('CredentialMode', credential['kind'])
        self._loaded_mode = mode
        if mode == 'system':
            api_key = self.secret_store.get() or '' if self.native_available else ''
            self._locked = not bool(api_key)
        else:
            password = self._resolve_vault_password(vault_password)
            if not password:
                api_key = ''
                self._locked = True
            else:
                api_key = _decrypt_api_key(metadata, credential, password)
                self._session_vault_password = password
                self._locked = False
        return SavedAPIConfiguration(
            provider=cast('str', metadata['provider']),
            model=cast('str', metadata['model']),
            base_url=cast('str', metadata['base_url']),
            api_key=api_key,
            credential_mode=mode,
            locked=self._locked,
        )

    def save(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        vault_password: str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError('api_key is required')
        if self.native_available:
            self._save_system(provider=provider, model=model, base_url=base_url, api_key=api_key)
        else:
            self._save_vault(
                provider=provider,
                model=model,
                base_url=base_url,
                api_key=api_key,
                vault_password=vault_password,
            )

    def clear(self) -> None:
        if self.native_available:
            self.secret_store.delete()
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialPersistenceError(f'Could not remove saved API settings: {exc}') from exc
        self._loaded_mode = None
        self._locked = False
        self._session_vault_password = self._environment_vault_password

    def _save_system(self, *, provider: str, model: str, base_url: str, api_key: str) -> None:
        previous_key = self.secret_store.get()
        previous_metadata = self.path.read_bytes() if self.path.is_file() else None
        self.secret_store.set(api_key)
        try:
            self._write_metadata(
                provider=provider,
                model=model,
                base_url=base_url,
                credential={'kind': 'system'},
            )
        except OSError as exc:
            self._restore_secret(previous_key)
            self._restore_metadata(previous_metadata)
            raise CredentialPersistenceError(f'Could not save API settings: {exc}') from exc
        self._loaded_mode = 'system'
        self._locked = False

    def _save_vault(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        vault_password: str | None,
    ) -> None:
        password = self._resolve_vault_password(vault_password)
        if not password:
            raise CredentialPersistenceError(
                'A vault password is required because no native system credential store is available'
            )
        credential = _encrypt_api_key(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            vault_password=password,
        )
        try:
            self._write_metadata(
                provider=provider,
                model=model,
                base_url=base_url,
                credential=credential,
            )
        except OSError as exc:
            raise CredentialPersistenceError(f'Could not save encrypted API settings: {exc}') from exc
        self._session_vault_password = password
        self._loaded_mode = 'encrypted_vault'
        self._locked = False

    def _resolve_vault_password(self, supplied: str | None) -> str:
        password = supplied if supplied is not None else self._session_vault_password
        if not password:
            return ''
        if len(password) < _VAULT_PASSWORD_MIN_CHARS:
            raise CredentialPersistenceError(f'Vault password must be at least {_VAULT_PASSWORD_MIN_CHARS} characters')
        if len(password) > _VAULT_PASSWORD_MAX_CHARS:
            raise CredentialPersistenceError(f'Vault password must be {_VAULT_PASSWORD_MAX_CHARS} characters or fewer')
        return password

    def _read_metadata(self) -> dict[str, object] | None:
        if not self.path.is_file():
            return None
        try:
            parsed = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CredentialPersistenceError(f'Could not read saved API settings: {exc}') from exc
        if not isinstance(parsed, dict) or parsed.get('version') not in {1, _CONFIG_VERSION}:
            raise CredentialPersistenceError('Saved API settings use an unsupported format')
        metadata: dict[str, object] = {
            'provider': _metadata_text(parsed, 'provider'),
            'model': _metadata_text(parsed, 'model'),
            'base_url': _metadata_text(parsed, 'base_url'),
        }
        if parsed['version'] == 1:
            metadata['credential'] = {'kind': 'system'}
            return metadata
        credential = parsed.get('credential')
        if not isinstance(credential, dict):
            raise CredentialPersistenceError('Saved API settings do not identify a credential store')
        kind = credential.get('kind')
        if kind == 'system':
            metadata['credential'] = {'kind': 'system'}
            return metadata
        if kind != 'encrypted_vault':
            raise CredentialPersistenceError('Saved API settings identify an unsupported credential store')
        metadata['credential'] = {
            'kind': 'encrypted_vault',
            'kdf': _metadata_text(credential, 'kdf'),
            'salt': _metadata_text(credential, 'salt'),
            'nonce': _metadata_text(credential, 'nonce'),
            'ciphertext': _metadata_text(credential, 'ciphertext'),
        }
        if cast('dict[str, str]', metadata['credential'])['kdf'] != 'scrypt-v1':
            raise CredentialPersistenceError('Saved API settings use an unsupported vault key derivation')
        return metadata

    def _write_metadata(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        credential: dict[str, str],
    ) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_name(f'.{self.path.name}.{uuid.uuid4().hex}.tmp')
        payload = {
            'version': _CONFIG_VERSION,
            'provider': provider,
            'model': model,
            'base_url': base_url,
            'credential': credential,
        }
        try:
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            temporary.chmod(0o600)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _restore_secret(self, previous_key: str | None) -> None:
        try:
            if previous_key:
                self.secret_store.set(previous_key)
            else:
                self.secret_store.delete()
        except CredentialPersistenceError:
            pass

    def _restore_metadata(self, previous_metadata: bytes | None) -> None:
        try:
            if previous_metadata is None:
                self.path.unlink(missing_ok=True)
            else:
                self.path.write_bytes(previous_metadata)
                self.path.chmod(0o600)
        except OSError:
            pass


def default_api_config_path() -> Path:
    """Return the per-user settings path for macOS, Windows, or Linux."""

    override = os.environ.get(_CONFIG_DIRECTORY_ENV)
    if override:
        return Path(override).expanduser() / _CONFIG_FILENAME
    if sys.platform == 'darwin':
        directory = Path.home() / 'Library' / 'Application Support' / 'Agent Trace Studio'
    elif sys.platform == 'win32':
        directory = Path(os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or Path.home())
        directory /= 'Agent Trace Studio'
    else:
        directory = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'agent-trace-studio'
    return directory / _CONFIG_FILENAME


def _supported_native_backend(backend: KeyringBackend) -> KeyringBackend | None:
    identity = f'{type(backend).__module__}.{type(backend).__name__}'.lower()
    if any(marker in identity for marker in _NATIVE_BACKEND_MARKERS):
        return backend
    chained = getattr(backend, 'backends', ())
    if isinstance(chained, list | tuple):
        for candidate in chained:
            if (
                isinstance(candidate, KeyringBackend)
                and (supported := _supported_native_backend(candidate)) is not None
            ):
                return supported
    return None


def _native_backend_label(backend: KeyringBackend) -> str:
    identity = f'{type(backend).__module__}.{type(backend).__name__}'.lower()
    if 'macos' in identity:
        return 'macOS Keychain'
    if 'windows' in identity or 'winvault' in identity:
        return 'Windows Credential Locker'
    if 'secretservice' in identity or 'libsecret' in identity:
        return 'Secret Service'
    if 'kwallet' in identity:
        return 'KWallet'
    return 'system credential store'


def _encrypt_api_key(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    vault_password: str,
) -> dict[str, str]:
    salt = os.urandom(_VAULT_SALT_BYTES)
    nonce = os.urandom(_VAULT_NONCE_BYTES)
    key = _derive_vault_key(vault_password, salt)
    associated_data = _vault_associated_data(provider=provider, model=model, base_url=base_url)
    ciphertext = AESGCM(key).encrypt(nonce, api_key.encode('utf-8'), associated_data)
    return {
        'kind': 'encrypted_vault',
        'kdf': 'scrypt-v1',
        'salt': _base64_encode(salt),
        'nonce': _base64_encode(nonce),
        'ciphertext': _base64_encode(ciphertext),
    }


def _decrypt_api_key(metadata: dict[str, object], credential: dict[str, str], vault_password: str) -> str:
    salt = _base64_decode(credential['salt'], field='salt')
    nonce = _base64_decode(credential['nonce'], field='nonce')
    ciphertext = _base64_decode(credential['ciphertext'], field='ciphertext')
    if len(salt) != _VAULT_SALT_BYTES or len(nonce) != _VAULT_NONCE_BYTES:
        raise CredentialPersistenceError('Saved encrypted API settings are malformed')
    key = _derive_vault_key(vault_password, salt)
    associated_data = _vault_associated_data(
        provider=cast('str', metadata['provider']),
        model=cast('str', metadata['model']),
        base_url=cast('str', metadata['base_url']),
    )
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, associated_data)
        return plaintext.decode('utf-8')
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise CredentialPersistenceError('Vault password is incorrect or the encrypted vault is damaged') from exc


def _derive_vault_key(vault_password: str, salt: bytes) -> bytes:
    return Scrypt(
        salt=salt,
        length=_VAULT_KDF_LENGTH,
        n=_VAULT_KDF_N,
        r=_VAULT_KDF_R,
        p=_VAULT_KDF_P,
    ).derive(vault_password.encode('utf-8'))


def _vault_associated_data(*, provider: str, model: str, base_url: str) -> bytes:
    value = {
        'schema': 'agent-trace-studio.model-api.v2',
        'provider': provider,
        'model': model,
        'base_url': base_url,
    }
    return json.dumps(value, separators=(',', ':'), sort_keys=True).encode('utf-8')


def _metadata_text(value: dict[object, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise CredentialPersistenceError('Saved API settings are incomplete')
    return item.strip()


def _base64_encode(value: bytes) -> str:
    return base64.b64encode(value).decode('ascii')


def _base64_decode(value: str, *, field: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CredentialPersistenceError(f'Saved encrypted API {field} is malformed') from exc
