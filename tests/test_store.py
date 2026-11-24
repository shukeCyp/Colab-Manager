"""Unit tests for AccountStore and credential normalization."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from colab_manager.store import AccountStore, normalize_authorized_user_info, validate_alias


def test_validate_alias():
    assert validate_alias("acc1") == "acc1"
    assert validate_alias("work-pro_01.a") == "work-pro_01.a"

    for bad in ["", "   ", "default", "ALL", "best", "-start", "has space", "a/b"]:
        with pytest.raises(ValueError):
            validate_alias(bad)


def test_normalize_authorized_user_info():
    raw = {"refresh_token": "r1", "client_id": "cid", "client_secret": "csec"}
    norm = normalize_authorized_user_info(raw)
    assert norm["type"] == "authorized_user"
    assert "type" not in raw  # does not mutate input


def test_store_lifecycle_and_permissions(tmp_path: Path):
    store = AccountStore(tmp_path)
    creds = {"refresh_token": "tok1", "client_id": "id1", "client_secret": "sec1"}

    rec1 = store.upsert_account("acc1", creds, email="a1@example.com", note="primary")
    assert rec1.alias == "acc1"
    assert store.resolve_alias(None) == "acc1"
    assert store.resolve_alias("default") == "acc1"

    # Verify strict file permissions on POSIX
    mode = stat.S_IMODE(os.stat(store.credentials_path("acc1")).st_mode)
    assert mode == 0o600
    saved = store.read_credentials("acc1")
    assert saved["type"] == "authorized_user"

    # Add second account and switch default
    store.upsert_account("acc2", creds, email="a2@example.com", set_default=True)
    assert store.resolve_alias(None) == "acc2"
    assert store.find_by_email("A1@EXAMPLE.COM").alias == "acc1"

    # Rename default account preserves default pointer
    store.rename_account("acc2", "pro")
    assert store.resolve_alias(None) == "pro"
    assert store.credentials_path("pro").is_file()
    assert not store.credentials_path("acc2").exists()

    # Remove default account falls back to remaining account
    store.remove_account("pro")
    assert store.resolve_alias(None) == "acc1"
    assert len(store.list_accounts()) == 1
