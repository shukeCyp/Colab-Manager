"""OAuth2 authentication, token refresh, keyring import/sync, and token inspection."""

from __future__ import annotations

import json
import os
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import keyring
import requests as std_requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from colab_cli.auth import (
    KEYRING_SERVICE_NAME,
    KEYRING_USERNAME,
    LEGACY_TOKEN_PATH,
    PUBLIC_SCOPES,
    REMOTE_REDIRECT_URI,
)
from colab_manager.store import AccountStore, normalize_authorized_user_info

TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

REQUIRED_COLAB_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/colaboratory",
        "https://www.googleapis.com/auth/userinfo.email",
    }
)


def load_oauth_client_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load OAuth client config JSON (custom path or bundled colab_cli config)."""
    candidates: List[Path] = []
    if config_path:
        candidates.append(Path(config_path).expanduser())
    else:
        candidates.append(Path.home() / ".colab-cli-oauth-config.json")

    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))

    config_resource = resources.files("colab_cli").joinpath("oauth_config.json")
    if config_resource.is_file():
        return json.loads(config_resource.read_text(encoding="utf-8"))

    raise FileNotFoundError(
        "Could not locate OAuth client config (neither ~/.colab-cli-oauth-config.json "
        "nor bundled colab_cli/oauth_config.json)."
    )


def run_interactive_oauth_flow(
    *,
    client_config_path: Optional[str] = None,
    open_browser: bool = False,
    login_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Run Google's remote copy-paste OAuth2 flow matching official colab-cli."""
    client_config = load_oauth_client_config(client_config_path)
    flow = InstalledAppFlow.from_client_config(client_config, PUBLIC_SCOPES)
    flow.redirect_uri = REMOTE_REDIRECT_URI

    url_kwargs: Dict[str, Any] = {
        "prompt": "consent",
        "access_type": "offline",
        "token_usage": "remote",
    }
    if login_hint:
        url_kwargs["login_hint"] = login_hint

    auth_url, _ = flow.authorization_url(**url_kwargs)

    print("\nTo authorize this Google account, open the URL below in a browser")
    print("(Tip: open in an Incognito window or target Chrome Profile for multi-account login):\n")
    print(f"  {auth_url}\n")

    if open_browser:
        webbrowser.open(auth_url)

    print("After approving, Google will display an authorization code.")
    code = input("Enter the authorization code: ").strip()
    if not code:
        raise ValueError("Authorization code cannot be empty.")

    flow.fetch_token(code=code)
    creds = flow.credentials
    return normalize_authorized_user_info(json.loads(creds.to_json()))


def read_native_colab_token() -> Tuple[Dict[str, Any], str]:
    """Read the currently authenticated token from native colab-cli keyring or legacy file."""
    token_json = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME)
    if token_json:
        return normalize_authorized_user_info(json.loads(token_json)), "keyring"

    legacy = Path(LEGACY_TOKEN_PATH)
    if legacy.is_file():
        return (
            normalize_authorized_user_info(json.loads(legacy.read_text(encoding="utf-8"))),
            str(legacy),
        )

    raise FileNotFoundError(
        "No existing login found in native colab-cli keyring or ~/.config/colab-cli/token.json."
    )


def sync_account_to_native_keyring(store: AccountStore, alias: str) -> None:
    """Write a managed account's OAuth credentials into native colab-cli keyring."""
    cred_data = store.read_credentials(alias)
    payload = json.dumps(cred_data)
    keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME, payload)


def credentials_from_dict(cred_data: Dict[str, Any]) -> Credentials:
    """Build a Google OAuth2 Credentials object from an authorized_user JSON dict."""
    scopes = cred_data.get("scopes") or list(PUBLIC_SCOPES)
    return Credentials.from_authorized_user_info(cred_data, scopes=scopes)


def ensure_fresh_credentials(
    store: AccountStore,
    alias: str,
    *,
    timeout: float = 15.0,
) -> Credentials:
    """Load credentials for an account, refreshing and persisting if expired."""
    cred_data = store.read_credentials(alias)
    creds = credentials_from_dict(cred_data)
    old_token = creds.token

    if not creds.valid:
        if not creds.refresh_token:
            raise RuntimeError(
                f"Account '{alias}' token expired and has no refresh_token. "
                f"Run `cm auth login {alias}` to re-authenticate."
            )
        creds.refresh(Request())
        if creds.token != old_token:
            updated = normalize_authorized_user_info(json.loads(creds.to_json()))
            store.update_credentials(alias, updated)

    return creds


def fetch_token_info(creds: Credentials, *, timeout: float = 10.0) -> Dict[str, Any]:
    """Query Google tokeninfo endpoint for email, scopes, and remaining TTL."""
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())

    resp = std_requests.get(
        TOKENINFO_URL,
        params={"access_token": creds.token},
        timeout=timeout,
    )
    if not resp.ok:
        raise RuntimeError(f"tokeninfo failed ({resp.status_code}): {resp.text[:200]}")
    info = resp.json()

    # Fallback to userinfo if tokeninfo omitted email
    if not info.get("email"):
        try:
            u = std_requests.get(
                USERINFO_URL,
                headers={"Authorization": f"Bearer {creds.token}"},
                timeout=timeout,
            )
            if u.ok:
                email = u.json().get("email")
                if email:
                    info["email"] = email
        except Exception:
            pass

    return info


def extract_email_from_credentials(cred_data: Dict[str, Any]) -> Optional[str]:
    """Best-effort lookup of the Google email for a freshly authorized credential dict."""
    try:
        creds = credentials_from_dict(cred_data)
        info = fetch_token_info(creds)
        return info.get("email")
    except Exception:
        return None


def suggest_alias_from_email(email: Optional[str], fallback: str = "main") -> str:
    """Derive a clean default alias from an email address."""
    if not email or "@" not in email:
        return fallback
    local = email.split("@", 1)[0]
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in local).strip("._-")
    if not cleaned or not cleaned[0].isalnum():
        return fallback
    return cleaned[:24]


def default_native_sessions_path() -> Path:
    return Path(os.path.expanduser("~/.config/colab-cli/sessions.json"))
