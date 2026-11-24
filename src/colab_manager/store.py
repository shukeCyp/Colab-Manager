"""Secure storage for managed accounts, ADC credential files, and session configs."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import filelock

from colab_manager.models import AccountRecord, RegistryState, utc_now

ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,31}$")
RESERVED_ALIASES = frozenset({"default", "all", "best", "auto"})

_OWNER_FILE_MODE = 0o600
_OWNER_DIR_MODE = 0o700


def get_manager_home() -> Path:
    """Return the root config directory (overrideable via COLAB_MANAGER_HOME)."""
    override = os.environ.get("COLAB_MANAGER_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".config" / "colab-manager").resolve()


def validate_alias(alias: str) -> str:
    alias = alias.strip()
    if not alias:
        raise ValueError("Account alias cannot be empty.")
    if alias.lower() in RESERVED_ALIASES:
        raise ValueError(f"Alias '{alias}' is reserved. Choose a different name.")
    if not ALIAS_PATTERN.match(alias):
        raise ValueError(
            f"Invalid alias '{alias}'. Use 1-32 characters: letters, digits, '.', '_', '-' "
            "(must start with a letter or digit)."
        )
    return alias


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, _OWNER_DIR_MODE)


def _write_private_json(path: Path, payload: Any) -> None:
    _ensure_private_dir(path.parent)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _OWNER_FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        with contextlib.suppress(OSError):
            os.chmod(path, _OWNER_FILE_MODE)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def normalize_authorized_user_info(data: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure credential JSON conforms to Google ADC authorized_user schema."""
    normalized = dict(data)
    if "type" not in normalized:
        normalized["type"] = "authorized_user"
    return normalized


class AccountStore:
    """Thread- and process-safe store for managed Colab accounts."""

    def __init__(self, root: Optional[Path] = None):
        self.root = (root or get_manager_home()).resolve()
        self.registry_path = self.root / "accounts.json"
        self.accounts_dir = self.root / "accounts"
        self._lock_path = self.root / "accounts.json.lock"
        _ensure_private_dir(self.root)
        _ensure_private_dir(self.accounts_dir)

    def _lock(self) -> filelock.FileLock:
        return filelock.FileLock(str(self._lock_path))

    def account_dir(self, alias: str) -> Path:
        return self.accounts_dir / alias

    def credentials_path(self, alias: str) -> Path:
        return self.account_dir(alias) / "credentials.json"

    def sessions_path(self, alias: str) -> Path:
        return self.account_dir(alias) / "sessions.json"

    def _load_unlocked(self) -> RegistryState:
        if not self.registry_path.exists():
            return RegistryState()
        raw = self.registry_path.read_text(encoding="utf-8").strip()
        if not raw:
            return RegistryState()
        return RegistryState.model_validate_json(raw)

    def _save_unlocked(self, state: RegistryState) -> None:
        _write_private_json(
            self.registry_path,
            json.loads(state.model_dump_json()),
        )

    def load(self) -> RegistryState:
        with self._lock():
            return self._load_unlocked()

    def list_accounts(self) -> List[AccountRecord]:
        state = self.load()
        return [state.accounts[k] for k in sorted(state.accounts.keys())]

    def get_account(self, alias: str) -> Optional[AccountRecord]:
        state = self.load()
        return state.accounts.get(alias)

    def resolve_alias(self, alias: Optional[str] = None) -> str:
        """Resolve an explicit alias, '@default', or None into a registered alias."""
        state = self.load()
        if not state.accounts:
            raise KeyError(
                "No accounts registered yet. Run `cm auth import` or `cm auth add <alias>` first."
            )
        if alias is None or alias == "default":
            if state.default_account and state.default_account in state.accounts:
                return state.default_account
            first = sorted(state.accounts.keys())[0]
            return first
        if alias not in state.accounts:
            known = ", ".join(sorted(state.accounts.keys()))
            raise KeyError(f"Unknown account '{alias}'. Registered accounts: {known}")
        return alias

    def find_by_email(self, email: str) -> Optional[AccountRecord]:
        if not email:
            return None
        target = email.strip().lower()
        state = self.load()
        for rec in state.accounts.values():
            if rec.email and rec.email.strip().lower() == target:
                return rec
        return None

    def upsert_account(
        self,
        alias: str,
        cred_data: Dict[str, Any],
        *,
        email: Optional[str] = None,
        note: Optional[str] = None,
        set_default: bool = False,
        copy_sessions_from: Optional[Path] = None,
    ) -> AccountRecord:
        alias = validate_alias(alias)
        normalized_creds = normalize_authorized_user_info(cred_data)

        with self._lock():
            state = self._load_unlocked()
            now = utc_now()
            existing = state.accounts.get(alias)
            if existing:
                rec = AccountRecord(
                    alias=alias,
                    email=email if email is not None else existing.email,
                    note=note if note is not None else existing.note,
                    created_at=existing.created_at,
                    updated_at=now,
                )
            else:
                rec = AccountRecord(
                    alias=alias,
                    email=email,
                    note=note or "",
                    created_at=now,
                    updated_at=now,
                )

            acct_dir = self.account_dir(alias)
            _ensure_private_dir(acct_dir)
            _write_private_json(self.credentials_path(alias), normalized_creds)

            if copy_sessions_from and copy_sessions_from.exists():
                dest = self.sessions_path(alias)
                shutil.copy2(copy_sessions_from, dest)
                with contextlib.suppress(OSError):
                    os.chmod(dest, _OWNER_FILE_MODE)

            state.accounts[alias] = rec
            if set_default or not state.default_account or state.default_account not in state.accounts:
                state.default_account = alias
            self._save_unlocked(state)
            return rec

    def update_credentials(self, alias: str, cred_data: Dict[str, Any]) -> None:
        normalized_creds = normalize_authorized_user_info(cred_data)
        cred_file = self.credentials_path(alias)
        lock = filelock.FileLock(str(cred_file) + ".lock")
        with lock:
            _write_private_json(cred_file, normalized_creds)

    def read_credentials(self, alias: str) -> Dict[str, Any]:
        cred_file = self.credentials_path(alias)
        if not cred_file.exists():
            raise FileNotFoundError(
                f"Credentials file not found for account '{alias}': {cred_file}"
            )
        return json.loads(cred_file.read_text(encoding="utf-8"))

    def set_default(self, alias: str) -> AccountRecord:
        with self._lock():
            state = self._load_unlocked()
            if alias not in state.accounts:
                raise KeyError(f"Unknown account '{alias}'.")
            state.default_account = alias
            self._save_unlocked(state)
            return state.accounts[alias]

    def set_note(self, alias: str, note: str) -> AccountRecord:
        with self._lock():
            state = self._load_unlocked()
            if alias not in state.accounts:
                raise KeyError(f"Unknown account '{alias}'.")
            rec = state.accounts[alias]
            rec.note = note
            rec.updated_at = utc_now()
            self._save_unlocked(state)
            return rec

    def update_email(self, alias: str, email: str) -> None:
        if not email:
            return
        with self._lock():
            state = self._load_unlocked()
            rec = state.accounts.get(alias)
            if not rec or rec.email == email:
                return
            rec.email = email
            rec.updated_at = utc_now()
            self._save_unlocked(state)

    def rename_account(self, old_alias: str, new_alias: str) -> AccountRecord:
        new_alias = validate_alias(new_alias)
        with self._lock():
            state = self._load_unlocked()
            if old_alias not in state.accounts:
                raise KeyError(f"Unknown account '{old_alias}'.")
            if new_alias in state.accounts:
                raise ValueError(f"Account '{new_alias}' already exists.")

            old_dir = self.account_dir(old_alias)
            new_dir = self.account_dir(new_alias)
            if old_dir.exists():
                old_dir.rename(new_dir)

            rec = state.accounts.pop(old_alias)
            rec.alias = new_alias
            rec.updated_at = utc_now()
            state.accounts[new_alias] = rec
            if state.default_account == old_alias:
                state.default_account = new_alias
            self._save_unlocked(state)
            return rec

    def remove_account(self, alias: str) -> AccountRecord:
        with self._lock():
            state = self._load_unlocked()
            if alias not in state.accounts:
                raise KeyError(f"Unknown account '{alias}'.")
            rec = state.accounts.pop(alias)
            acct_dir = self.account_dir(alias)
            if acct_dir.exists():
                shutil.rmtree(acct_dir)
            if state.default_account == alias:
                remaining = sorted(state.accounts.keys())
                state.default_account = remaining[0] if remaining else None
            self._save_unlocked(state)
            return rec

    def load_local_sessions(self, alias: str) -> Dict[str, Dict[str, Any]]:
        """Read local colab-cli sessions.json for an account (endpoint -> session info)."""
        path = self.sessions_path(alias)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}
