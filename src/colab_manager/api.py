"""Concurrent Google Colab backend client for multi-account usage and session queries."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from google.auth.transport.requests import AuthorizedSession

from colab_cli.client import (
    ACCEPT_JSON_HEADER,
    COLAB_CLIENT_AGENT_HEADER,
    COLAB_XSRF_TOKEN_HEADER,
    Prod,
    TUN_ENDPOINT,
    XSSI_PREFIX,
    shape_display_label,
)
from colab_manager.models import (
    AccountRecord,
    AccountUsageReport,
    AssignmentDetail,
    AuthCheckReport,
    CcuFullInfo,
)
from colab_manager.oauth import (
    REQUIRED_COLAB_SCOPES,
    ensure_fresh_credentials,
    fetch_token_info,
)
from colab_manager.store import AccountStore

DEFAULT_MAX_WORKERS = 8
DEFAULT_TIMEOUT = 15.0


def _strip_xssi(text: str) -> str:
    if text.startswith(XSSI_PREFIX):
        return text[len(XSSI_PREFIX) :]
    return text


def _hardware_label(accelerator: str) -> str:
    if not accelerator or accelerator.upper() == "NONE":
        return "CPU"
    return accelerator.upper()


def _variant_label(variant_val: object) -> str:
    mapping = {0: "DEFAULT", 1: "GPU", 2: "TPU", "0": "DEFAULT", "1": "GPU", "2": "TPU"}
    if variant_val in mapping:
        return mapping[variant_val]
    return str(variant_val or "DEFAULT")


class ColabAccountClient:
    """Lightweight Colab tunnel API client bound to one AuthorizedSession."""

    def __init__(self, session: AuthorizedSession, *, timeout: float = DEFAULT_TIMEOUT):
        self.env = Prod()
        self.session = session
        self.timeout = timeout

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        hdrs = {
            ACCEPT_JSON_HEADER["key"]: ACCEPT_JSON_HEADER["value"],
            COLAB_CLIENT_AGENT_HEADER["key"]: COLAB_CLIENT_AGENT_HEADER["value"],
        }
        if extra:
            hdrs.update(extra)
        return hdrs

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict:
        url = urljoin(self.env.domain, f"{TUN_ENDPOINT}/{path.lstrip('/')}")
        resp = self.session.request(
            method,
            url,
            params={"authuser": "0"},
            headers=self._headers(extra_headers),
            timeout=self.timeout,
        )
        if not resp.ok:
            snippet = resp.text.strip().replace("\n", " ")[:180]
            raise RuntimeError(f"HTTP {resp.status_code} {resp.reason}: {snippet}")

        body = _strip_xssi(resp.text).strip()
        if not body:
            return {}
        return json.loads(body)

    def get_ccu_full_info(self) -> CcuFullInfo:
        data = self._request_json("ccu-info")
        return CcuFullInfo.model_validate(data)

    def list_assignments_raw(self) -> List[Dict]:
        data = self._request_json("assignments")
        assignments = data.get("assignments")
        return assignments if isinstance(assignments, list) else []

    def unassign(self, endpoint: str) -> None:
        resp = self._request_json(f"unassign/{endpoint}", method="GET")
        xsrf_token = resp.get("token")
        if not xsrf_token:
            raise RuntimeError(f"Missing XSRF token when unassigning '{endpoint}'.")
        self._request_json(
            f"unassign/{endpoint}",
            method="POST",
            extra_headers={COLAB_XSRF_TOKEN_HEADER["key"]: xsrf_token},
        )


def _endpoint_to_session_name(store: AccountStore, alias: str) -> Dict[str, str]:
    local_sessions = store.load_local_sessions(alias)
    mapping: Dict[str, str] = {}
    for s_name, s_data in local_sessions.items():
        if isinstance(s_data, dict):
            ep = s_data.get("endpoint")
            if ep:
                mapping[ep] = s_name
    return mapping


def _parse_assignments(
    raw_items: List[Dict],
    *,
    alias: str,
    email: Optional[str],
    ep_to_name: Dict[str, str],
) -> List[AssignmentDetail]:
    details: List[AssignmentDetail] = []
    for item in raw_items:
        endpoint = str(item.get("endpoint", ""))
        acc = _hardware_label(str(item.get("accelerator", "NONE")))
        shape = shape_display_label(item.get("machineShape"))
        variant = _variant_label(item.get("variant"))
        proxy = item.get("runtimeProxyInfo") or {}
        expires_in = proxy.get("tokenExpiresInSeconds")
        details.append(
            AssignmentDetail(
                alias=alias,
                email=email,
                session_name=ep_to_name.get(endpoint),
                endpoint=endpoint,
                accelerator=acc,
                machine_shape=shape,
                variant=variant,
                token_expires_in_seconds=expires_in,
            )
        )
    return details


def fetch_account_usage(
    store: AccountStore,
    record: AccountRecord,
    *,
    is_default: bool = False,
    include_assignments: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> AccountUsageReport:
    """Fetch complete usage, balance, eligible accelerators, and active sessions for one account."""
    try:
        creds = ensure_fresh_credentials(store, record.alias, timeout=timeout)
        email = record.email
        if not email:
            try:
                info = fetch_token_info(creds, timeout=min(timeout, 8.0))
                email = info.get("email")
                if email:
                    store.update_email(record.alias, email)
            except Exception:
                pass

        session = AuthorizedSession(creds)
        client = ColabAccountClient(session, timeout=timeout)
        ccu = client.get_ccu_full_info()

        assignments: List[AssignmentDetail] = []
        if include_assignments and (ccu.assignments_count > 0 or True):
            # Always query assignments when requested so we stay accurate even if count lags
            try:
                raw_assignments = client.list_assignments_raw()
                ep_map = _endpoint_to_session_name(store, record.alias)
                assignments = _parse_assignments(
                    raw_assignments,
                    alias=record.alias,
                    email=email,
                    ep_to_name=ep_map,
                )
            except Exception:
                assignments = []

        return AccountUsageReport(
            alias=record.alias,
            email=email,
            note=record.note,
            is_default=is_default,
            ok=True,
            balance=ccu.current_balance,
            rate_hourly=ccu.consumption_rate_hourly,
            assignments_count=max(ccu.assignments_count, len(assignments)),
            eligible_gpus=ccu.eligible_gpus,
            eligible_tpus=ccu.eligible_tpus,
            assignments=assignments,
        )
    except Exception as exc:
        return AccountUsageReport(
            alias=record.alias,
            email=record.email,
            note=record.note,
            is_default=is_default,
            ok=False,
            error=str(exc),
        )


def fetch_accounts_usage_concurrent(
    store: AccountStore,
    aliases: Optional[List[str]] = None,
    *,
    include_assignments: bool = True,
    max_workers: int = DEFAULT_MAX_WORKERS,
    timeout: float = DEFAULT_TIMEOUT,
) -> List[AccountUsageReport]:
    """Fetch usage reports for selected (or all) accounts concurrently."""
    state = store.load()
    target_aliases = aliases if aliases else sorted(state.accounts.keys())
    records: List[Tuple[AccountRecord, bool]] = []
    for a in target_aliases:
        resolved = store.resolve_alias(a)
        rec = state.accounts[resolved]
        records.append((rec, resolved == state.default_account))

    if not records:
        return []

    results: Dict[str, AccountUsageReport] = {}
    worker_count = max(1, min(max_workers, len(records)))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        future_map = {
            pool.submit(
                fetch_account_usage,
                store,
                rec,
                is_default=is_def,
                include_assignments=include_assignments,
                timeout=timeout,
            ): rec.alias
            for rec, is_def in records
        }
        for fut in as_completed(future_map):
            alias = future_map[fut]
            results[alias] = fut.result()

    return [results[rec.alias] for rec, _ in records]


def check_account_auth(
    store: AccountStore,
    record: AccountRecord,
    *,
    is_default: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> AuthCheckReport:
    """Validate token refresh, expiry, scopes, and live Google email for one account."""
    try:
        cred_data = store.read_credentials(record.alias)
        has_refresh = bool(cred_data.get("refresh_token"))
        creds = ensure_fresh_credentials(store, record.alias, timeout=timeout)
        info = fetch_token_info(creds, timeout=timeout)

        live_email = info.get("email")
        if live_email and live_email != record.email:
            store.update_email(record.alias, live_email)

        scopes = sorted((info.get("scope") or "").split())
        scope_set = set(scopes)
        missing = sorted(REQUIRED_COLAB_SCOPES - scope_set)

        expires_in: Optional[int] = None
        if info.get("expires_in") is not None:
            try:
                expires_in = int(info["expires_in"])
            except (TypeError, ValueError):
                expires_in = None

        return AuthCheckReport(
            alias=record.alias,
            recorded_email=record.email,
            live_email=live_email,
            is_default=is_default,
            ok=len(missing) == 0,
            error=f"Missing scopes: {', '.join(missing)}" if missing else None,
            expires_in_seconds=expires_in,
            has_refresh_token=has_refresh,
            missing_scopes=missing,
            scopes=scopes,
        )
    except Exception as exc:
        return AuthCheckReport(
            alias=record.alias,
            recorded_email=record.email,
            is_default=is_default,
            ok=False,
            error=str(exc),
        )


def check_accounts_auth_concurrent(
    store: AccountStore,
    aliases: Optional[List[str]] = None,
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    timeout: float = DEFAULT_TIMEOUT,
) -> List[AuthCheckReport]:
    state = store.load()
    target_aliases = aliases if aliases else sorted(state.accounts.keys())
    records: List[Tuple[AccountRecord, bool]] = []
    for a in target_aliases:
        resolved = store.resolve_alias(a)
        rec = state.accounts[resolved]
        records.append((rec, resolved == state.default_account))

    if not records:
        return []

    results: Dict[str, AuthCheckReport] = {}
    worker_count = max(1, min(max_workers, len(records)))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        future_map = {
            pool.submit(
                check_account_auth,
                store,
                rec,
                is_default=is_def,
                timeout=timeout,
            ): rec.alias
            for rec, is_def in records
        }
        for fut in as_completed(future_map):
            alias = future_map[fut]
            results[alias] = fut.result()

    return [results[rec.alias] for rec, _ in records]


def unassign_account_sessions(
    store: AccountStore,
    alias: str,
    target: Optional[str] = None,
    *,
    stop_all: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> List[str]:
    """Unassign one or all remote Colab runtimes for a given account."""
    creds = ensure_fresh_credentials(store, alias, timeout=timeout)
    client = ColabAccountClient(AuthorizedSession(creds), timeout=timeout)
    raw = client.list_assignments_raw()
    ep_map = _endpoint_to_session_name(store, alias)
    name_to_ep = {v: k for k, v in ep_map.items()}

    endpoints_to_stop: List[str] = []
    if stop_all:
        endpoints_to_stop = [str(item.get("endpoint", "")) for item in raw if item.get("endpoint")]
    elif target:
        if target in name_to_ep:
            endpoints_to_stop = [name_to_ep[target]]
        else:
            endpoints_to_stop = [target]
    else:
        raise ValueError("Specify a session/endpoint target or pass --all.")

    stopped: List[str] = []
    for ep in endpoints_to_stop:
        client.unassign(ep)
        stopped.append(ep)

    # Clean up stopped sessions from local sessions.json if present
    sessions_file = store.sessions_path(alias)
    if sessions_file.exists() and stopped:
        try:
            data = json.loads(sessions_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                stopped_set = set(stopped)
                cleaned = {
                    k: v
                    for k, v in data.items()
                    if not (isinstance(v, dict) and v.get("endpoint") in stopped_set)
                }
                if len(cleaned) != len(data):
                    sessions_file.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")
        except Exception:
            pass

    return stopped
