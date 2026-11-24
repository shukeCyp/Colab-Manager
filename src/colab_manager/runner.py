"""Transparent subprocess proxy for running native `colab` CLI under a managed account."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from colab_manager.store import AccountStore


def find_colab_executable() -> str:
    """Locate the `colab` CLI binary."""
    exe = shutil.which("colab")
    if exe:
        return exe
    # Fallback to the same bin directory as the current Python executable
    candidate = os.path.join(os.path.dirname(sys.executable), "colab")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    raise FileNotFoundError(
        "Could not find `colab` executable on PATH. Install `google-colab-cli` first."
    )


def build_colab_invocation(
    store: AccountStore,
    alias: str,
    colab_args: List[str],
) -> Tuple[List[str], Dict[str, str]]:
    """Build exact argv and environment variables to isolate one account."""
    resolved = store.resolve_alias(alias)
    creds_path = store.credentials_path(resolved)
    if not creds_path.exists():
        raise FileNotFoundError(
            f"Credentials missing for account '{resolved}'. Run `cm auth login {resolved}`."
        )
    sessions_path = store.sessions_path(resolved)
    sessions_path.parent.mkdir(parents=True, exist_ok=True)

    colab_bin = find_colab_executable()
    cmd = [
        colab_bin,
        "--auth=adc",
        "--config",
        str(sessions_path),
        *colab_args,
    ]
    env = os.environ.copy()
    env["GOOGLE_APPLICATION_CREDENTIALS"] = str(creds_path)
    return cmd, env


def run_colab_command(
    store: AccountStore,
    alias: str,
    colab_args: List[str],
) -> int:
    """Execute native `colab` CLI transparently, preserving full TTY interaction."""
    cmd, env = build_colab_invocation(store, alias, colab_args)
    completed = subprocess.run(cmd, env=env)
    return completed.returncode


def generate_shell_env(
    store: AccountStore,
    alias: str,
    shell: str = "posix",
) -> str:
    """Generate shell export + alias snippet so `colab` uses a specific account in this shell."""
    resolved = store.resolve_alias(alias)
    creds_path = str(store.credentials_path(resolved))
    sessions_path = str(store.sessions_path(resolved))
    colab_bin = find_colab_executable()

    shell = shell.lower()
    if shell == "fish":
        return "\n".join(
            [
                f"set -gx GOOGLE_APPLICATION_CREDENTIALS {shlex.quote(creds_path)}",
                f"set -gx COLAB_MANAGER_ACCOUNT {shlex.quote(resolved)}",
                f"function colab; {shlex.quote(colab_bin)} --auth=adc --config {shlex.quote(sessions_path)} $argv; end",
            ]
        )

    return "\n".join(
        [
            f"export GOOGLE_APPLICATION_CREDENTIALS={shlex.quote(creds_path)}",
            f"export COLAB_MANAGER_ACCOUNT={shlex.quote(resolved)}",
            f"alias colab={shlex.quote(f'{colab_bin} --auth=adc --config {shlex.quote(sessions_path)}')}",
        ]
    )
