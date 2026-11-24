"""CLI entry point for colab-manager (`colabm` / `cm`)."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.live import Live
from typing_extensions import Annotated

from colab_manager import __version__
from colab_manager.api import (
    check_accounts_auth_concurrent,
    fetch_accounts_usage_concurrent,
    unassign_account_sessions,
)
from colab_manager.models import AccountUsageReport
from colab_manager.oauth import (
    default_native_sessions_path,
    extract_email_from_credentials,
    read_native_colab_token,
    run_interactive_oauth_flow,
    suggest_alias_from_email,
    sync_account_to_native_keyring,
)
from colab_manager.render import (
    build_usage_renderable,
    render_accounts_table,
    render_auth_check_table,
    render_sessions_table,
    render_usage_table,
)
from colab_manager.runner import generate_shell_env, run_colab_command
from colab_manager.store import AccountStore, normalize_authorized_user_info, validate_alias

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="colabm",
    help=(
        "Multi-account manager for Google Colab CLI (`colab`).\n\n"
        "Shortcut: run `cm @<alias> <colab args...>` (or `cm @best ...`) "
        "to execute any native `colab` command under that account."
    ),
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

auth_app = typer.Typer(
    help="Manage Google accounts, OAuth credentials, and token health.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(auth_app, name="auth")


def _get_store() -> AccountStore:
    return AccountStore()


def _filter_and_sort_usage(
    reports: List[AccountUsageReport],
    *,
    sort_by: str = "balance",
    ascending: bool = False,
    only_active: bool = False,
    min_balance: Optional[float] = None,
    gpu: Optional[str] = None,
    tpu: Optional[str] = None,
) -> List[AccountUsageReport]:
    filtered: List[AccountUsageReport] = []
    want_gpu = gpu.upper() if gpu else None
    want_tpu = tpu.upper() if tpu else None

    for r in reports:
        if only_active and r.assignments_count <= 0:
            continue
        if min_balance is not None:
            if not r.ok or (r.balance or 0.0) < min_balance:
                continue
        if want_gpu:
            if not r.ok or want_gpu not in {g.upper() for g in r.eligible_gpus}:
                continue
        if want_tpu:
            if not r.ok or want_tpu not in {t.upper() for t in r.eligible_tpus}:
                continue
        filtered.append(r)

    key_name = sort_by.lower()
    if key_name == "name":
        filtered.sort(key=lambda r: r.alias.lower(), reverse=not ascending)
    elif key_name == "rate":
        filtered.sort(
            key=lambda r: (r.ok, r.rate_hourly if r.rate_hourly is not None else -1.0),
            reverse=not ascending,
        )
    elif key_name in ("active", "assignments"):
        filtered.sort(
            key=lambda r: (r.ok, r.assignments_count),
            reverse=not ascending,
        )
    else:  # balance
        filtered.sort(
            key=lambda r: (r.ok, r.balance if r.balance is not None else -1.0),
            reverse=not ascending,
        )
    return filtered


def _pick_best_account(
    store: AccountStore,
    *,
    gpu: Optional[str] = None,
    tpu: Optional[str] = None,
    idle_only: bool = False,
    min_balance: Optional[float] = None,
) -> AccountUsageReport:
    reports = fetch_accounts_usage_concurrent(store, include_assignments=False)
    candidates = [r for r in reports if r.ok]
    if idle_only:
        candidates = [r for r in candidates if r.assignments_count == 0]
    candidates = _filter_and_sort_usage(
        candidates,
        sort_by="balance",
        ascending=False,
        min_balance=min_balance,
        gpu=gpu,
        tpu=tpu,
    )
    if not candidates:
        raise RuntimeError("No healthy account matched the selection criteria.")
    return candidates[0]


def _extract_flag_value(args: List[str], flag_name: str) -> Optional[str]:
    """Extract `--gpu T4` or `--gpu=T4` from arbitrary forwarded colab args."""
    prefix = f"{flag_name}="
    for i, token in enumerate(args):
        if token == flag_name and i + 1 < len(args):
            return args[i + 1]
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


# ---------------------------------------------------------------------------
# Auth / Account Management Commands
# ---------------------------------------------------------------------------


def _do_auth_add(
    alias: str,
    *,
    note: str = "",
    from_file: Optional[Path] = None,
    open_browser: bool = False,
    set_default: bool = False,
    client_oauth_config: Optional[str] = None,
) -> None:
    store = _get_store()
    try:
        alias = validate_alias(alias)
    except ValueError as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(code=1)

    if from_file is not None:
        if not from_file.is_file():
            err_console.print(f"[red]Error:[/] Credential file not found: {from_file}")
            raise typer.Exit(code=1)
        try:
            cred_data = normalize_authorized_user_info(
                json.loads(from_file.read_text(encoding="utf-8"))
            )
        except Exception as e:
            err_console.print(f"[red]Error:[/] Invalid JSON file: {e}")
            raise typer.Exit(code=1)
    else:
        try:
            cred_data = run_interactive_oauth_flow(
                client_config_path=client_oauth_config,
                open_browser=open_browser,
            )
        except KeyboardInterrupt:
            err_console.print("\n[yellow]Cancelled.[/]")
            raise typer.Exit(code=130)
        except Exception as e:
            err_console.print(f"[red]OAuth flow failed:[/] {e}")
            raise typer.Exit(code=1)

    email = extract_email_from_credentials(cred_data)
    if email:
        existing_email = store.find_by_email(email)
        if existing_email and existing_email.alias != alias:
            err_console.print(
                f"[yellow]Warning:[/] Email [bold]{email}[/] is already registered "
                f"under alias [bold]{existing_email.alias}[/]."
            )

    rec = store.upsert_account(
        alias,
        cred_data,
        email=email,
        note=note,
        set_default=set_default,
    )
    console.print(
        f"[green]Saved account[/] [bold]{rec.alias}[/] "
        f"({rec.email or 'email unknown'}) -> {store.credentials_path(rec.alias)}"
    )


def _do_auth_import(
    alias: Optional[str] = None,
    *,
    note: str = "",
    copy_sessions: bool = True,
    set_default: bool = False,
) -> None:
    store = _get_store()
    try:
        cred_data, source = read_native_colab_token()
    except Exception as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(code=1)

    email = extract_email_from_credentials(cred_data)
    chosen_alias = alias or suggest_alias_from_email(email, fallback="main")
    try:
        chosen_alias = validate_alias(chosen_alias)
    except ValueError as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(code=1)

    native_sessions = default_native_sessions_path() if copy_sessions else None
    rec = store.upsert_account(
        chosen_alias,
        cred_data,
        email=email,
        note=note,
        set_default=set_default,
        copy_sessions_from=native_sessions,
    )
    extra = " (+ sessions.json)" if (native_sessions and native_sessions.exists()) else ""
    console.print(
        f"[green]Imported[/] native colab login ({source}) as [bold]{rec.alias}[/] "
        f"({rec.email or 'email unknown'}){extra}"
    )


def _do_auth_list() -> None:
    store = _get_store()
    state = store.load()
    accounts = [state.accounts[k] for k in sorted(state.accounts.keys())]
    session_counts = {a.alias: len(store.load_local_sessions(a.alias)) for a in accounts}
    cred_exists = {a.alias: store.credentials_path(a.alias).is_file() for a in accounts}
    render_accounts_table(
        console,
        accounts,
        default_alias=state.default_account,
        local_session_counts=session_counts,
        cred_exists=cred_exists,
    )


@auth_app.command("add")
def auth_add(
    alias: Annotated[str, typer.Argument(help="Short alias for this account (e.g. acc1, pro, work)")],
    note: Annotated[str, typer.Option("--note", "-n", help="Optional note/label for this account")] = "",
    from_file: Annotated[
        Optional[Path],
        typer.Option("--from-file", "-f", help="Import an existing ADC / authorized_user JSON file"),
    ] = None,
    open_browser: Annotated[
        bool,
        typer.Option("--open", help="Automatically open the OAuth URL in your default browser"),
    ] = False,
    default: Annotated[
        bool,
        typer.Option("--default", "-d", help="Set this account as the default"),
    ] = False,
    client_oauth_config: Annotated[
        Optional[str],
        typer.Option("-c", "--client-oauth-config", help="Optional custom OAuth client JSON path"),
    ] = None,
) -> None:
    """Add a new Google account via OAuth login or an existing ADC JSON file."""
    _do_auth_add(
        alias,
        note=note,
        from_file=from_file,
        open_browser=open_browser,
        set_default=default,
        client_oauth_config=client_oauth_config,
    )


@auth_app.command("import")
def auth_import(
    alias: Annotated[
        Optional[str],
        typer.Argument(help="Alias for the imported account (auto-derived from email if omitted)"),
    ] = None,
    note: Annotated[str, typer.Option("--note", "-n", help="Optional note/label")] = "",
    copy_sessions: Annotated[
        bool,
        typer.Option(
            "--copy-sessions/--no-copy-sessions",
            help="Also copy ~/.config/colab-cli/sessions.json into this account",
        ),
    ] = True,
    default: Annotated[
        bool,
        typer.Option("--default", "-d", help="Set as default account"),
    ] = False,
) -> None:
    """Import the account currently logged in to native `colab` (OS keyring)."""
    _do_auth_import(
        alias,
        note=note,
        copy_sessions=copy_sessions,
        set_default=default,
    )


@auth_app.command("login")
def auth_login(
    alias: Annotated[str, typer.Argument(help="Existing account alias to re-authenticate")],
    open_browser: Annotated[
        bool,
        typer.Option("--open", help="Automatically open the OAuth URL in your default browser"),
    ] = False,
    client_oauth_config: Annotated[
        Optional[str],
        typer.Option("-c", "--client-oauth-config", help="Optional custom OAuth client JSON path"),
    ] = None,
) -> None:
    """Re-authenticate an existing account without losing its note or sessions."""
    store = _get_store()
    try:
        resolved = store.resolve_alias(alias)
    except KeyError as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(code=1)

    rec = store.get_account(resolved)
    try:
        cred_data = run_interactive_oauth_flow(
            client_config_path=client_oauth_config,
            open_browser=open_browser,
            login_hint=rec.email if rec else None,
        )
    except KeyboardInterrupt:
        err_console.print("\n[yellow]Cancelled.[/]")
        raise typer.Exit(code=130)
    except Exception as e:
        err_console.print(f"[red]OAuth flow failed:[/] {e}")
        raise typer.Exit(code=1)

    email = extract_email_from_credentials(cred_data)
    updated = store.upsert_account(resolved, cred_data, email=email)
    console.print(
        f"[green]Refreshed credentials for[/] [bold]{updated.alias}[/] ({updated.email or 'email unknown'})"
    )


@auth_app.command("ls")
def auth_ls() -> None:
    """List all managed accounts."""
    _do_auth_list()


@auth_app.command("check")
def auth_check(
    aliases: Annotated[
        Optional[List[str]],
        typer.Argument(help="Account aliases to check (default: all accounts)"),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Output raw JSON")] = False,
) -> None:
    """Concurrently verify token validity, refresh capability, scopes, and email for all accounts."""
    store = _get_store()
    try:
        reports = check_accounts_auth_concurrent(store, aliases or None)
    except KeyError as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps([r.model_dump(mode="json") for r in reports], indent=2))
    else:
        if not reports:
            console.print("[yellow]No accounts registered yet.[/]")
        else:
            render_auth_check_table(console, reports)

    if any(not r.ok for r in reports):
        raise typer.Exit(code=1)


@auth_app.command("rm")
def auth_rm(
    alias: Annotated[str, typer.Argument(help="Account alias to delete")],
    yes: Annotated[bool, typer.Option("-y", "--yes", help="Skip confirmation prompt")] = False,
) -> None:
    """Remove a managed account and its local credentials/sessions."""
    store = _get_store()
    rec = store.get_account(alias)
    if not rec:
        err_console.print(f"[red]Error:[/] Unknown account '{alias}'.")
        raise typer.Exit(code=1)

    if not yes:
        confirmed = typer.confirm(
            f"Remove account '{alias}' ({rec.email or 'no email'}) and delete its local credentials?"
        )
        if not confirmed:
            raise typer.Exit(code=0)

    store.remove_account(alias)
    console.print(f"[green]Removed account[/] [bold]{alias}[/].")


@auth_app.command("rename")
def auth_rename(
    old_alias: Annotated[str, typer.Argument(help="Current alias")],
    new_alias: Annotated[str, typer.Argument(help="New alias")],
) -> None:
    """Rename an existing account alias."""
    store = _get_store()
    try:
        rec = store.rename_account(old_alias, new_alias)
    except (KeyError, ValueError) as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(code=1)
    console.print(f"[green]Renamed[/] [bold]{old_alias}[/] -> [bold]{rec.alias}[/].")


@auth_app.command("note")
def auth_note(
    alias: Annotated[str, typer.Argument(help="Account alias")],
    note: Annotated[str, typer.Argument(help="Note text (pass empty string '' to clear)")],
) -> None:
    """Set or clear a descriptive note for an account."""
    store = _get_store()
    try:
        resolved = store.resolve_alias(alias)
        rec = store.set_note(resolved, note)
    except KeyError as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(code=1)
    console.print(f"[green]Updated note for[/] [bold]{rec.alias}[/]: {rec.note or '(empty)'}")


# Top-level ergonomic aliases for auth commands
@app.command("add")
def top_add(
    alias: Annotated[str, typer.Argument(help="Short alias for this account (e.g. acc1, pro, work)")],
    note: Annotated[str, typer.Option("--note", "-n", help="Optional note/label")] = "",
    from_file: Annotated[Optional[Path], typer.Option("--from-file", "-f", help="Import existing ADC JSON")] = None,
    open_browser: Annotated[bool, typer.Option("--open", help="Open OAuth URL in default browser")] = False,
    default: Annotated[bool, typer.Option("--default", "-d", help="Set as default account")] = False,
) -> None:
    """Shortcut for `auth add`."""
    _do_auth_add(alias, note=note, from_file=from_file, open_browser=open_browser, set_default=default)


@app.command("import")
def top_import(
    alias: Annotated[Optional[str], typer.Argument(help="Alias (auto-derived from email if omitted)")] = None,
    note: Annotated[str, typer.Option("--note", "-n", help="Optional note/label")] = "",
    copy_sessions: Annotated[bool, typer.Option("--copy-sessions/--no-copy-sessions")] = True,
    default: Annotated[bool, typer.Option("--default", "-d", help="Set as default account")] = False,
) -> None:
    """Shortcut for `auth import`: import current native `colab` login."""
    _do_auth_import(alias, note=note, copy_sessions=copy_sessions, set_default=default)


@app.command("ls")
def top_ls() -> None:
    """Shortcut for `auth ls`: list all managed accounts."""
    _do_auth_list()


@app.command("use")
def use_account(
    alias: Annotated[str, typer.Argument(help="Account alias to set as default")],
    sync_keyring: Annotated[
        bool,
        typer.Option(
            "--sync-keyring",
            help="Also write this account's token into native `colab` OS keyring",
        ),
    ] = False,
) -> None:
    """Switch the default account (optionally syncing back to native `colab` keyring)."""
    store = _get_store()
    try:
        resolved = store.resolve_alias(alias)
        rec = store.set_default(resolved)
    except KeyError as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(code=1)

    msg = f"[green]Default account set to[/] [bold]{rec.alias}[/] ({rec.email or 'email unknown'})"
    if sync_keyring:
        try:
            sync_account_to_native_keyring(store, resolved)
            msg += " [cyan](synced to native colab keyring)[/]"
        except Exception as e:
            err_console.print(f"[red]Failed to sync native keyring:[/] {e}")
            raise typer.Exit(code=1)
    console.print(msg)


# ---------------------------------------------------------------------------
# Multi-Account Usage, Sessions, and Smart Picker
# ---------------------------------------------------------------------------


@app.command("usage")
def usage_cmd(
    aliases: Annotated[
        Optional[List[str]],
        typer.Argument(help="Specific account aliases to query (default: all accounts)"),
    ] = None,
    sort: Annotated[
        str,
        typer.Option("--sort", "-s", help="Sort by: balance, rate, active, name"),
    ] = "balance",
    asc: Annotated[
        bool,
        typer.Option("--asc", help="Sort ascending instead of descending"),
    ] = False,
    only_active: Annotated[
        bool,
        typer.Option("--only-active", help="Only show accounts with active assignments"),
    ] = False,
    min_balance: Annotated[
        Optional[float],
        typer.Option("--min-balance", help="Filter accounts with balance >= N compute units"),
    ] = None,
    gpu: Annotated[
        Optional[str],
        typer.Option("--gpu", help="Filter accounts eligible for this GPU (e.g. H100, A100, L4, T4)"),
    ] = None,
    tpu: Annotated[
        Optional[str],
        typer.Option("--tpu", help="Filter accounts eligible for this TPU (e.g. V6E1, V5E1)"),
    ] = None,
    no_eligible: Annotated[
        bool,
        typer.Option("--no-eligible", help="Hide Eligible GPUs/TPUs column for compact terminals"),
    ] = False,
    watch: Annotated[
        Optional[int],
        typer.Option("--watch", "-w", help="Continuously refresh every N seconds (Ctrl+C to exit)"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output machine-readable JSON"),
    ] = False,
) -> None:
    """Query compute-unit balance, burn rate, active runtimes, and eligible GPUs/TPUs across all accounts."""
    store = _get_store()

    def _query() -> List[AccountUsageReport]:
        raw = fetch_accounts_usage_concurrent(store, aliases or None, include_assignments=True)
        return _filter_and_sort_usage(
            raw,
            sort_by=sort,
            ascending=asc,
            only_active=only_active,
            min_balance=min_balance,
            gpu=gpu,
            tpu=tpu,
        )

    if watch is not None and not as_json:
        interval = max(3, watch)
        try:
            with Live(console=console, refresh_per_second=2) as live:
                while True:
                    reports = _query()
                    ts = datetime.now().strftime("%H:%M:%S")
                    table = build_usage_renderable(
                        reports,
                        title=f"Colab Multi-Account Usage (updated {ts}, every {interval}s)",
                        show_eligible=not no_eligible,
                    )
                    live.update(table)
                    time.sleep(interval)
        except KeyboardInterrupt:
            return

    try:
        reports = _query()
    except KeyError as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(code=1)

    if as_json:
        payload = {
            "summary": {
                "accounts_total": len(reports),
                "accounts_ok": sum(1 for r in reports if r.ok),
                "total_balance": round(sum(r.balance or 0.0 for r in reports if r.ok), 4),
                "total_rate_hourly": round(sum(r.rate_hourly or 0.0 for r in reports if r.ok), 4),
                "total_active_assignments": sum(r.assignments_count for r in reports if r.ok),
            },
            "accounts": [r.model_dump(mode="json") for r in reports],
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    render_usage_table(console, reports, show_eligible=not no_eligible)


@app.command("sessions")
def sessions_cmd(
    aliases: Annotated[
        Optional[List[str]],
        typer.Argument(help="Specific account aliases to query (default: all accounts)"),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Output JSON")] = False,
) -> None:
    """List all active remote Colab runtimes across all managed accounts."""
    store = _get_store()
    try:
        reports = fetch_accounts_usage_concurrent(store, aliases or None, include_assignments=True)
    except KeyError as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(code=1)

    if as_json:
        items = [a.model_dump(mode="json") for r in reports if r.ok for a in r.assignments]
        typer.echo(json.dumps(items, indent=2))
        return

    render_sessions_table(console, reports)


@app.command("pick")
def pick_cmd(
    gpu: Annotated[Optional[str], typer.Option("--gpu", help="Require eligibility for this GPU (e.g. A100, H100, T4)")] = None,
    tpu: Annotated[Optional[str], typer.Option("--tpu", help="Require eligibility for this TPU (e.g. V5E1, V6E1)")] = None,
    idle: Annotated[bool, typer.Option("--idle", help="Require 0 currently active assignments")] = False,
    min_balance: Annotated[Optional[float], typer.Option("--min-balance", help="Minimum CU balance required")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Output full JSON for the chosen account")] = False,
) -> None:
    """Pick the best available account (highest balance matching optional GPU/TPU/idle filters)."""
    store = _get_store()
    try:
        best = _pick_best_account(
            store,
            gpu=gpu,
            tpu=tpu,
            idle_only=idle,
            min_balance=min_balance,
        )
    except Exception as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(best.model_dump(mode="json"), indent=2))
    else:
        typer.echo(best.alias)


# ---------------------------------------------------------------------------
# Proxy Execution, Stop, and Shell Env
# ---------------------------------------------------------------------------


@app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run_cmd(
    ctx: typer.Context,
    alias: Annotated[
        str,
        typer.Argument(help="Account alias, 'default', or 'best' (auto-pick highest balance)"),
    ],
) -> None:
    """Run any native `colab` command under a specific account (transparent TTY passthrough)."""
    colab_args = list(ctx.args)
    if not colab_args:
        err_console.print("[red]Error:[/] Provide a colab subcommand, e.g. `cm run acc1 sessions`.")
        raise typer.Exit(code=1)

    _dispatch_proxy(alias, colab_args)


def _dispatch_proxy(alias_or_special: str, colab_args: List[str]) -> None:
    store = _get_store()
    target = alias_or_special
    if target.lower() == "best":
        gpu_hint = _extract_flag_value(colab_args, "--gpu")
        tpu_hint = _extract_flag_value(colab_args, "--tpu")
        try:
            best = _pick_best_account(store, gpu=gpu_hint, tpu=tpu_hint)
            target = best.alias
            err_console.print(
                f"[dim][colabm] Auto-selected account '[bold]{target}[/]' "
                f"(balance: {best.balance:.2f} CU, active: {best.assignments_count})[/]"
            )
        except Exception as e:
            err_console.print(f"[red]Error selecting best account:[/] {e}")
            raise typer.Exit(code=1)

    try:
        rc = run_colab_command(store, target, colab_args)
    except (KeyError, FileNotFoundError) as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(code=1)
    raise typer.Exit(code=rc)


@app.command("stop")
def stop_cmd(
    alias: Annotated[str, typer.Argument(help="Account alias (or 'all' with --all)")],
    target: Annotated[
        Optional[str],
        typer.Argument(help="Session name or endpoint ID to unassign"),
    ] = None,
    stop_all: Annotated[
        bool,
        typer.Option("--all", help="Unassign ALL active remote runtimes for the account(s)"),
    ] = False,
    yes: Annotated[bool, typer.Option("-y", "--yes", help="Skip confirmation prompt")] = False,
) -> None:
    """Stop/unassign remote Colab runtimes (including untracked web sessions)."""
    store = _get_store()
    if alias.lower() == "all":
        if not stop_all:
            err_console.print("[red]Error:[/] Pass `--all` when targeting `all` accounts.")
            raise typer.Exit(code=1)
        state = store.load()
        target_aliases = sorted(state.accounts.keys())
    else:
        try:
            target_aliases = [store.resolve_alias(alias)]
        except KeyError as e:
            err_console.print(f"[red]Error:[/] {e}")
            raise typer.Exit(code=1)

    if stop_all and not yes:
        confirmed = typer.confirm(
            f"Unassign ALL active runtimes for {', '.join(target_aliases)}?"
        )
        if not confirmed:
            raise typer.Exit(code=0)

    total_stopped = 0
    for acct in target_aliases:
        try:
            stopped = unassign_account_sessions(store, acct, target, stop_all=stop_all)
            total_stopped += len(stopped)
            if stopped:
                for ep in stopped:
                    console.print(f"[green][{acct}] Stopped[/] {ep}")
            else:
                console.print(f"[dim][{acct}] No active runtimes to stop.[/]")
        except Exception as e:
            err_console.print(f"[red][{acct}] Failed to stop:[/] {e}")

    if stop_all:
        console.print(f"[bold]Total stopped:[/] {total_stopped}")


@app.command("env")
def env_cmd(
    alias: Annotated[Optional[str], typer.Argument(help="Account alias (default: current default account)")] = None,
    shell: Annotated[str, typer.Option("--shell", help="Shell syntax: posix (zsh/bash) or fish")] = "posix",
) -> None:
    """Print shell export + alias snippet so `eval \"$(cm env <alias>)\"` pins this terminal to one account."""
    store = _get_store()
    try:
        snippet = generate_shell_env(store, alias or "default", shell=shell)
    except (KeyError, FileNotFoundError) as e:
        err_console.print(f"[red]Error:[/] {e}")
        raise typer.Exit(code=1)
    typer.echo(snippet)


@app.command("version")
def version_cmd() -> None:
    """Show colab-manager version and config path."""
    store = _get_store()
    console.print(f"colab-manager [bold]{__version__}[/] (config: {store.root})")


def main() -> None:
    """Entry point supporting `@<alias>` shortcut syntax before Typer routing."""
    if len(sys.argv) >= 2 and sys.argv[1].startswith("@"):
        alias = sys.argv[1][1:]
        forwarded = sys.argv[2:]
        if not alias:
            err_console.print("[red]Error:[/] Specify an account alias after '@', e.g. `cm @acc1 usage`.")
            raise SystemExit(1)
        if not forwarded:
            err_console.print(f"[red]Error:[/] Provide a colab command after '@{alias}', e.g. `cm @{alias} sessions`.")
            raise SystemExit(1)
        try:
            _dispatch_proxy(alias, forwarded)
        except typer.Exit as exc:
            raise SystemExit(exc.exit_code)
    app()


if __name__ == "__main__":
    main()
