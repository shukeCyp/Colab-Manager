"""Unit tests for ColabAccountClient, concurrent usage/auth queries, and unassign."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from colab_manager.api import (
    ColabAccountClient,
    _strip_xssi,
    check_accounts_auth_concurrent,
    fetch_accounts_usage_concurrent,
    unassign_account_sessions,
)
from colab_manager.store import AccountStore


def _make_response(status_code: int = 200, text: str = "", reason: str = "OK"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.reason = reason
    resp.text = text
    return resp


def test_strip_xssi():
    assert _strip_xssi(")]}'\n{\"a\":1}") == '{"a":1}'
    assert _strip_xssi('{"a":1}') == '{"a":1}'


def test_colab_account_client_ccu_assignments_and_unassign():
    session = MagicMock()
    ccu_payload = ")]}'\n" + json.dumps(
        {
            "currentBalance": 420.5,
            "consumptionRateHourly": 1.75,
            "assignmentsCount": 1,
            "eligibleGpus": ["A100", "T4"],
            "eligibleTpus": ["V5E1"],
        }
    )
    assign_payload = ")]}'\n" + json.dumps(
        {
            "assignments": [
                {
                    "endpoint": "m-s-xyz",
                    "accelerator": "T4",
                    "machineShape": 0,
                    "variant": 1,
                    "runtimeProxyInfo": {
                        "token": "ptok",
                        "tokenExpiresInSeconds": 3600,
                        "url": "https://example.test",
                    },
                }
            ]
        }
    )
    unassign_get_payload = ")]}'\n" + json.dumps({"token": "xsrf-123"})

    def fake_request(method, url, **kwargs):
        if url.endswith("/ccu-info"):
            return _make_response(200, ccu_payload)
        if url.endswith("/assignments"):
            return _make_response(200, assign_payload)
        if url.endswith("/unassign/m-s-xyz") and method == "GET":
            return _make_response(200, unassign_get_payload)
        if url.endswith("/unassign/m-s-xyz") and method == "POST":
            assert kwargs["headers"]["X-Goog-Colab-Token"] == "xsrf-123"
            return _make_response(200, "")
        raise AssertionError(f"Unexpected call: {method} {url}")

    session.request.side_effect = fake_request
    client = ColabAccountClient(session)

    ccu = client.get_ccu_full_info()
    assert ccu.current_balance == 420.5
    assert ccu.consumption_rate_hourly == 1.75
    assert ccu.eligible_gpus == ["A100", "T4"]
    assert ccu.eligible_tpus == ["V5E1"]

    assignments = client.list_assignments_raw()
    assert len(assignments) == 1
    assert assignments[0]["endpoint"] == "m-s-xyz"

    client.unassign("m-s-xyz")


def test_colab_account_client_http_error():
    session = MagicMock()
    session.request.return_value = _make_response(401, "Unauthorized", "Unauthorized")
    client = ColabAccountClient(session)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        client.get_ccu_full_info()


def test_fetch_accounts_usage_concurrent_isolates_failures(tmp_path: Path):
    store = AccountStore(tmp_path)
    store.upsert_account(
        "good",
        {"refresh_token": "r1", "client_id": "c", "client_secret": "s"},
        email="good@example.com",
    )
    store.upsert_account(
        "bad",
        {"refresh_token": "r2", "client_id": "c", "client_secret": "s"},
        email="bad@example.com",
    )
    # Seed local session name mapping for good account
    store.sessions_path("good").write_text(
        json.dumps({"my-train": {"endpoint": "ep-1"}}),
        encoding="utf-8",
    )

    def fake_ensure_creds(_store, alias, **_kwargs):
        if alias == "bad":
            raise RuntimeError("invalid_grant: token revoked")
        creds = MagicMock()
        creds.token = "tok-good"
        return creds

    with (
        patch("colab_manager.api.ensure_fresh_credentials", side_effect=fake_ensure_creds),
        patch("colab_manager.api.AuthorizedSession", return_value=MagicMock()),
        patch.object(
            ColabAccountClient,
            "get_ccu_full_info",
            return_value=MagicMock(
                current_balance=88.8,
                consumption_rate_hourly=1.1,
                assignments_count=1,
                eligible_gpus=["T4", "L4"],
                eligible_tpus=[],
            ),
        ),
        patch.object(
            ColabAccountClient,
            "list_assignments_raw",
            return_value=[
                {
                    "endpoint": "ep-1",
                    "accelerator": "L4",
                    "machineShape": 1,
                    "variant": 1,
                    "runtimeProxyInfo": {"tokenExpiresInSeconds": 1800},
                }
            ],
        ),
    ):
        reports = fetch_accounts_usage_concurrent(store)

    by_alias = {r.alias: r for r in reports}
    assert by_alias["good"].ok is True
    assert by_alias["good"].balance == 88.8
    assert by_alias["good"].assignments[0].session_name == "my-train"
    assert by_alias["good"].assignments[0].machine_shape == "High-RAM"

    assert by_alias["bad"].ok is False
    assert "invalid_grant" in (by_alias["bad"].error or "")


def test_check_accounts_auth_concurrent_scopes_and_email_sync(tmp_path: Path):
    store = AccountStore(tmp_path)
    store.upsert_account(
        "ok_acc",
        {"refresh_token": "r1", "client_id": "c", "client_secret": "s"},
        email=None,
    )
    store.upsert_account(
        "missing_scope",
        {"refresh_token": "r2", "client_id": "c", "client_secret": "s"},
        email="m@example.com",
    )

    def fake_ensure(_store, alias, **_kwargs):
        m = MagicMock()
        m.token = f"tok-{alias}"
        return m

    def fake_tokeninfo(creds, **_kwargs):
        if creds.token == "tok-ok_acc":
            return {
                "email": "synced@example.com",
                "expires_in": "3500",
                "scope": (
                    "openid https://www.googleapis.com/auth/userinfo.email "
                    "https://www.googleapis.com/auth/colaboratory"
                ),
            }
        return {
            "email": "m@example.com",
            "expires_in": "1200",
            "scope": "openid https://www.googleapis.com/auth/userinfo.email",
        }

    with (
        patch("colab_manager.api.ensure_fresh_credentials", side_effect=fake_ensure),
        patch("colab_manager.api.fetch_token_info", side_effect=fake_tokeninfo),
    ):
        reports = check_accounts_auth_concurrent(store)

    by_alias = {r.alias: r for r in reports}
    assert by_alias["ok_acc"].ok is True
    assert by_alias["ok_acc"].live_email == "synced@example.com"
    assert store.get_account("ok_acc").email == "synced@example.com"

    assert by_alias["missing_scope"].ok is False
    assert "https://www.googleapis.com/auth/colaboratory" in by_alias["missing_scope"].missing_scopes


def test_unassign_account_sessions_cleans_local_state(tmp_path: Path):
    store = AccountStore(tmp_path)
    store.upsert_account(
        "acc1",
        {"refresh_token": "r1", "client_id": "c", "client_secret": "s"},
    )
    store.sessions_path("acc1").write_text(
        json.dumps(
            {
                "s1": {"endpoint": "ep-1"},
                "s2": {"endpoint": "ep-2"},
            }
        ),
        encoding="utf-8",
    )

    stopped_calls = []

    with (
        patch("colab_manager.api.ensure_fresh_credentials", return_value=MagicMock()),
        patch("colab_manager.api.AuthorizedSession", return_value=MagicMock()),
        patch.object(
            ColabAccountClient,
            "list_assignments_raw",
            return_value=[{"endpoint": "ep-1"}, {"endpoint": "ep-2"}],
        ),
        patch.object(
            ColabAccountClient,
            "unassign",
            side_effect=lambda ep: stopped_calls.append(ep),
        ),
    ):
        stopped = unassign_account_sessions(store, "acc1", "s1")

    assert stopped == ["ep-1"]
    assert stopped_calls == ["ep-1"]
    remaining = json.loads(store.sessions_path("acc1").read_text(encoding="utf-8"))
    assert list(remaining.keys()) == ["s2"]
