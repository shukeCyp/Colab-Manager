"""Colab 多账号管理器命令行入口（`colabm` / `cm`）。"""

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
from typer import rich_utils as _typer_rich_utils
from typing_extensions import Annotated

# 安全将 Typer 帮助面板标题汉化（不影响升级兼容性）
for _attr, _label in (
    ("USAGE_PREFIX", "用法: "),
    ("ARGUMENTS_PANEL_TITLE", "位置参数"),
    ("OPTIONS_PANEL_TITLE", "选项"),
    ("COMMANDS_PANEL_TITLE", "子命令"),
    ("ERRORS_PANEL_TITLE", "错误"),
    ("DEFAULT_STRING", "[默认: {}]"),
    ("REQUIRED_LONG_STRING", "[必填]"),
):
    if hasattr(_typer_rich_utils, _attr):
        setattr(_typer_rich_utils, _attr, _label)

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
        "Google Colab CLI (`colab`) 单机多账号管理与全量算力监控工具。\n\n"
        "快捷用法：使用 `cm @<别名> <原生 colab 参数...>`（或 `cm @best ...`）"
        "直接以指定账号透传执行任意原生 `colab` 命令。"
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

auth_app = typer.Typer(
    help="管理 Google 账号、OAuth 凭证与令牌健康状态。",
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
        raise RuntimeError("没有符合筛选条件的健康账号。")
    return candidates[0]


def _extract_flag_value(args: List[str], flag_name: str) -> Optional[str]:
    """从透传参数中提取 `--gpu T4` 或 `--gpu=T4` 的值。"""
    prefix = f"{flag_name}="
    for i, token in enumerate(args):
        if token == flag_name and i + 1 < len(args):
            return args[i + 1]
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


# ---------------------------------------------------------------------------
# 账号与凭证管理命令
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
        err_console.print(f"[red]错误:[/] {e}")
        raise typer.Exit(code=1)

    if from_file is not None:
        if not from_file.is_file():
            err_console.print(f"[red]错误:[/] 找不到凭证文件: {from_file}")
            raise typer.Exit(code=1)
        try:
            cred_data = normalize_authorized_user_info(
                json.loads(from_file.read_text(encoding="utf-8"))
            )
        except Exception as e:
            err_console.print(f"[red]错误:[/] 凭证 JSON 格式无效: {e}")
            raise typer.Exit(code=1)
    else:
        try:
            cred_data = run_interactive_oauth_flow(
                client_config_path=client_oauth_config,
                open_browser=open_browser,
            )
        except KeyboardInterrupt:
            err_console.print("\n[yellow]已取消操作。[/]")
            raise typer.Exit(code=130)
        except Exception as e:
            err_console.print(f"[red]OAuth 授权失败:[/] {e}")
            raise typer.Exit(code=1)

    email = extract_email_from_credentials(cred_data)
    if email:
        existing_email = store.find_by_email(email)
        if existing_email and existing_email.alias != alias:
            err_console.print(
                f"[yellow]提示:[/] 邮箱 [bold]{email}[/] 此前已绑定到别名 [bold]{existing_email.alias}[/]。"
            )

    rec = store.upsert_account(
        alias,
        cred_data,
        email=email,
        note=note,
        set_default=set_default,
    )
    console.print(
        f"[green]已保存账号[/] [bold]{rec.alias}[/] "
        f"({rec.email or '邮箱未知'}) -> {store.credentials_path(rec.alias)}"
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
        err_console.print(f"[red]错误:[/] {e}")
        raise typer.Exit(code=1)

    email = extract_email_from_credentials(cred_data)
    chosen_alias = alias or suggest_alias_from_email(email, fallback="main")
    try:
        chosen_alias = validate_alias(chosen_alias)
    except ValueError as e:
        err_console.print(f"[red]错误:[/] {e}")
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
    extra = "（已同步 sessions.json）" if (native_sessions and native_sessions.exists()) else ""
    console.print(
        f"[green]已导入[/]原生 colab 登录态（来源：{source}）为 [bold]{rec.alias}[/] "
        f"({rec.email or '邮箱未知'}){extra}"
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
    alias: Annotated[str, typer.Argument(help="账号简短别名（例如 acc1、pro、work）")],
    note: Annotated[str, typer.Option("--note", "-n", help="可选备注标签（例如：主力 Pro+）")] = "",
    from_file: Annotated[
        Optional[Path],
        typer.Option("--from-file", "-f", help="直接导入已有的 ADC / authorized_user JSON 凭证文件"),
    ] = None,
    open_browser: Annotated[
        bool,
        typer.Option("--open", help="自动在系统默认浏览器中打开 OAuth 授权链接"),
    ] = False,
    default: Annotated[
        bool,
        typer.Option("--default", "-d", help="将该账号设为默认账号"),
    ] = False,
    client_oauth_config: Annotated[
        Optional[str],
        typer.Option("-c", "--client-oauth-config", help="自定义 OAuth 客户端配置 JSON 路径"),
    ] = None,
) -> None:
    """通过浏览器 OAuth 登录或本地 ADC 文件新增一个 Google 账号。"""
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
        typer.Argument(help="导入后的账号别名（省略则根据邮箱前缀自动生成）"),
    ] = None,
    note: Annotated[str, typer.Option("--note", "-n", help="可选备注标签")] = "",
    copy_sessions: Annotated[
        bool,
        typer.Option(
            "--copy-sessions/--no-copy-sessions",
            help="是否同时复制 ~/.config/colab-cli/sessions.json 到该账号",
        ),
    ] = True,
    default: Annotated[
        bool,
        typer.Option("--default", "-d", help="设为默认账号"),
    ] = False,
) -> None:
    """一键导入本机原生 `colab`（系统钥匙串）当前已登录的账号。"""
    _do_auth_import(
        alias,
        note=note,
        copy_sessions=copy_sessions,
        set_default=default,
    )


@auth_app.command("login")
def auth_login(
    alias: Annotated[str, typer.Argument(help="需要重新授权的已有账号别名")],
    open_browser: Annotated[
        bool,
        typer.Option("--open", help="自动在默认浏览器中打开 OAuth 授权链接"),
    ] = False,
    client_oauth_config: Annotated[
        Optional[str],
        typer.Option("-c", "--client-oauth-config", help="自定义 OAuth 客户端配置 JSON 路径"),
    ] = None,
) -> None:
    """对已有账号重新走 OAuth 授权刷新凭证（保留原备注与本地会话）。"""
    store = _get_store()
    try:
        resolved = store.resolve_alias(alias)
    except KeyError as e:
        err_console.print(f"[red]错误:[/] {e}")
        raise typer.Exit(code=1)

    rec = store.get_account(resolved)
    try:
        cred_data = run_interactive_oauth_flow(
            client_config_path=client_oauth_config,
            open_browser=open_browser,
            login_hint=rec.email if rec else None,
        )
    except KeyboardInterrupt:
        err_console.print("\n[yellow]已取消操作。[/]")
        raise typer.Exit(code=130)
    except Exception as e:
        err_console.print(f"[red]OAuth 授权失败:[/] {e}")
        raise typer.Exit(code=1)

    email = extract_email_from_credentials(cred_data)
    updated = store.upsert_account(resolved, cred_data, email=email)
    console.print(
        f"[green]已刷新账号凭证[/] [bold]{updated.alias}[/] ({updated.email or '邮箱未知'})"
    )


@auth_app.command("ls")
def auth_ls() -> None:
    """列出所有已管理的账号。"""
    _do_auth_list()


@auth_app.command("check")
def auth_check(
    aliases: Annotated[
        Optional[List[str]],
        typer.Argument(help="要检查的账号别名（省略则检查全部账号）"),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="以 JSON 格式输出原始结果")] = False,
) -> None:
    """并发检查所有账号的令牌有效性、刷新能力、权限范围与绑定邮箱。"""
    store = _get_store()
    try:
        reports = check_accounts_auth_concurrent(store, aliases or None)
    except KeyError as e:
        err_console.print(f"[red]错误:[/] {e}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps([r.model_dump(mode="json") for r in reports], indent=2))
    else:
        if not reports:
            console.print("[yellow]尚未添加任何账号。请先运行 `cm import` 或 `cm add <别名>`。[/]")
        else:
            render_auth_check_table(console, reports)

    if any(not r.ok for r in reports):
        raise typer.Exit(code=1)


@auth_app.command("rm")
def auth_rm(
    alias: Annotated[str, typer.Argument(help="要删除的账号别名")],
    yes: Annotated[bool, typer.Option("-y", "--yes", help="跳过确认提示直接删除")] = False,
) -> None:
    """删除一个已管理账号及其本地凭证与会话文件。"""
    store = _get_store()
    rec = store.get_account(alias)
    if not rec:
        err_console.print(f"[red]错误:[/] 未找到账号 '{alias}'。")
        raise typer.Exit(code=1)

    if not yes:
        confirmed = typer.confirm(
            f"确认删除账号 '{alias}' ({rec.email or '无邮箱'}) 及其本地凭证文件？"
        )
        if not confirmed:
            raise typer.Exit(code=0)

    store.remove_account(alias)
    console.print(f"[green]已删除账号[/] [bold]{alias}[/]。")


@auth_app.command("rename")
def auth_rename(
    old_alias: Annotated[str, typer.Argument(help="当前别名")],
    new_alias: Annotated[str, typer.Argument(help="新别名")],
) -> None:
    """重命名已有账号的别名。"""
    store = _get_store()
    try:
        rec = store.rename_account(old_alias, new_alias)
    except (KeyError, ValueError) as e:
        err_console.print(f"[red]错误:[/] {e}")
        raise typer.Exit(code=1)
    console.print(f"[green]已重命名[/] [bold]{old_alias}[/] -> [bold]{rec.alias}[/]。")


@auth_app.command("note")
def auth_note(
    alias: Annotated[str, typer.Argument(help="账号别名")],
    note: Annotated[str, typer.Argument(help="备注内容（传空字符串 '' 可清空备注）")],
) -> None:
    """设置或清空账号备注标签。"""
    store = _get_store()
    try:
        resolved = store.resolve_alias(alias)
        rec = store.set_note(resolved, note)
    except KeyError as e:
        err_console.print(f"[red]错误:[/] {e}")
        raise typer.Exit(code=1)
    console.print(f"[green]已更新备注[/] [bold]{rec.alias}[/]: {rec.note or '（空）'}")


# 顶层快捷别名命令
@app.command("add")
def top_add(
    alias: Annotated[str, typer.Argument(help="账号简短别名（例如 acc1、pro、work）")],
    note: Annotated[str, typer.Option("--note", "-n", help="可选备注标签")] = "",
    from_file: Annotated[Optional[Path], typer.Option("--from-file", "-f", help="直接导入已有 ADC JSON 凭证文件")] = None,
    open_browser: Annotated[bool, typer.Option("--open", help="自动在默认浏览器打开授权链接")] = False,
    default: Annotated[bool, typer.Option("--default", "-d", help="设为默认账号")] = False,
) -> None:
    """新增 Google 账号（`auth add` 的快捷命令）。"""
    _do_auth_add(alias, note=note, from_file=from_file, open_browser=open_browser, set_default=default)


@app.command("import")
def top_import(
    alias: Annotated[Optional[str], typer.Argument(help="导入后的别名（省略则根据邮箱自动生成）")] = None,
    note: Annotated[str, typer.Option("--note", "-n", help="可选备注标签")] = "",
    copy_sessions: Annotated[
        bool,
        typer.Option("--copy-sessions/--no-copy-sessions", help="是否同时复制原生 sessions.json"),
    ] = True,
    default: Annotated[bool, typer.Option("--default", "-d", help="设为默认账号")] = False,
) -> None:
    """一键导入本机原生 `colab` 已登录账号（`auth import` 快捷命令）。"""
    _do_auth_import(alias, note=note, copy_sessions=copy_sessions, set_default=default)


@app.command("ls")
def top_ls() -> None:
    """列出所有已管理的账号（`auth ls` 快捷命令）。"""
    _do_auth_list()


@app.command("use")
def use_account(
    alias: Annotated[str, typer.Argument(help="要设为默认的账号别名")],
    sync_keyring: Annotated[
        bool,
        typer.Option(
            "--sync-keyring",
            help="同时将该账号 Token 写回原生 `colab` 的系统钥匙串",
        ),
    ] = False,
) -> None:
    """切换默认账号（可选同步写回原生 `colab` 钥匙串）。"""
    store = _get_store()
    try:
        resolved = store.resolve_alias(alias)
        rec = store.set_default(resolved)
    except KeyError as e:
        err_console.print(f"[red]错误:[/] {e}")
        raise typer.Exit(code=1)

    msg = f"[green]已将默认账号切换为[/] [bold]{rec.alias}[/] ({rec.email or '邮箱未知'})"
    if sync_keyring:
        try:
            sync_account_to_native_keyring(store, resolved)
            msg += " [cyan]（已同步到原生 colab 系统钥匙串）[/]"
        except Exception as e:
            err_console.print(f"[red]同步到系统钥匙串失败:[/] {e}")
            raise typer.Exit(code=1)
    console.print(msg)


# ---------------------------------------------------------------------------
# 全账号算力查询、实例总览与智能选号
# ---------------------------------------------------------------------------


@app.command("usage")
def usage_cmd(
    aliases: Annotated[
        Optional[List[str]],
        typer.Argument(help="指定要查询的账号别名（省略则并发查询全部账号）"),
    ] = None,
    sort: Annotated[
        str,
        typer.Option("--sort", "-s", help="排序字段：balance(余额) / rate(速率) / active(实例数) / name(别名)"),
    ] = "balance",
    asc: Annotated[
        bool,
        typer.Option("--asc", help="改为升序排列（默认降序）"),
    ] = False,
    only_active: Annotated[
        bool,
        typer.Option("--only-active", help="仅显示当前有活跃实例的账号"),
    ] = False,
    min_balance: Annotated[
        Optional[float],
        typer.Option("--min-balance", help="仅显示余额 >= 指定数值（CU）的账号"),
    ] = None,
    gpu: Annotated[
        Optional[str],
        typer.Option("--gpu", help="筛选支持该 GPU 的账号（如 H100、A100、L4、T4）"),
    ] = None,
    tpu: Annotated[
        Optional[str],
        typer.Option("--tpu", help="筛选支持该 TPU 的账号（如 V6E1、V5E1）"),
    ] = None,
    no_eligible: Annotated[
        bool,
        typer.Option("--no-eligible", help="隐藏“可用 GPU / TPU”列以适配超窄终端"),
    ] = False,
    watch: Annotated[
        Optional[int],
        typer.Option("--watch", "-w", help="每隔 N 秒自动刷新实时监控面板（按 Ctrl+C 退出）"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="输出结构化 JSON（便于脚本处理）"),
    ] = False,
) -> None:
    """并发查询所有账号的算力余额、消耗速率、活跃实例与可用 GPU/TPU。"""
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
                        title=f"Colab 多账号算力实时监控（更新于 {ts}，每 {interval} 秒刷新）",
                        show_eligible=not no_eligible,
                    )
                    live.update(table)
                    time.sleep(interval)
        except KeyboardInterrupt:
            return

    try:
        reports = _query()
    except KeyError as e:
        err_console.print(f"[red]错误:[/] {e}")
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
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    render_usage_table(console, reports, show_eligible=not no_eligible)


@app.command("sessions")
def sessions_cmd(
    aliases: Annotated[
        Optional[List[str]],
        typer.Argument(help="指定要查询的账号别名（省略则查询全部账号）"),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="以 JSON 格式输出")] = False,
) -> None:
    """跨账号列出所有正在运行的远端 Colab 实例（含网页端开启的实例）。"""
    store = _get_store()
    try:
        reports = fetch_accounts_usage_concurrent(store, aliases or None, include_assignments=True)
    except KeyError as e:
        err_console.print(f"[red]错误:[/] {e}")
        raise typer.Exit(code=1)

    if as_json:
        items = [a.model_dump(mode="json") for r in reports if r.ok for a in r.assignments]
        typer.echo(json.dumps(items, indent=2, ensure_ascii=False))
        return

    render_sessions_table(console, reports)


@app.command("pick")
def pick_cmd(
    gpu: Annotated[Optional[str], typer.Option("--gpu", help="要求支持该 GPU（如 A100、H100、T4）")] = None,
    tpu: Annotated[Optional[str], typer.Option("--tpu", help="要求支持该 TPU（如 V5E1、V6E1）")] = None,
    idle: Annotated[bool, typer.Option("--idle", help="要求当前无活跃运行实例（空闲号）")] = False,
    min_balance: Annotated[Optional[float], typer.Option("--min-balance", help="要求最低算力余额（CU）")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="输出选中账号的完整 JSON 信息")] = False,
) -> None:
    """智能挑选最优账号（按条件筛选并返回余额最高的健康账号别名）。"""
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
        err_console.print(f"[red]错误:[/] {e}")
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(best.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        typer.echo(best.alias)


# ---------------------------------------------------------------------------
# 透传执行、实例关停与终端环境导出
# ---------------------------------------------------------------------------


@app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run_cmd(
    ctx: typer.Context,
    alias: Annotated[
        str,
        typer.Argument(help="账号别名，或使用 'default'（默认号）/ 'best'（自动选最高余额号）"),
    ],
) -> None:
    """以指定账号透明透传执行任意原生 `colab` 命令（完整保留交互式 TTY）。"""
    colab_args = list(ctx.args)
    if not colab_args:
        err_console.print("[red]错误:[/] 请在账号别名后提供原生 colab 子命令，例如 `cm run main sessions`。")
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
                f"[dim][colabm] 已自动选中账号 '[bold]{target}[/]' "
                f"（余额: {best.balance:.2f} CU，活跃实例: {best.assignments_count}）[/]"
            )
        except Exception as e:
            err_console.print(f"[red]自动选号失败:[/] {e}")
            raise typer.Exit(code=1)

    try:
        rc = run_colab_command(store, target, colab_args)
    except (KeyError, FileNotFoundError) as e:
        err_console.print(f"[red]错误:[/] {e}")
        raise typer.Exit(code=1)
    raise typer.Exit(code=rc)


@app.command("stop")
def stop_cmd(
    alias: Annotated[str, typer.Argument(help="账号别名（配合 --all 时可传 'all' 表示全部账号）")],
    target: Annotated[
        Optional[str],
        typer.Argument(help="要停止的本地会话名或远端节点 ID"),
    ] = None,
    stop_all: Annotated[
        bool,
        typer.Option("--all", help="关停该账号（或全部账号）下所有正在运行的远端实例"),
    ] = False,
    yes: Annotated[bool, typer.Option("-y", "--yes", help="跳过确认提示")] = False,
) -> None:
    """关停/释放远端 Colab 实例（支持一键清理包括网页端开启的全部实例）。"""
    store = _get_store()
    if alias.lower() == "all":
        if not stop_all:
            err_console.print("[red]错误:[/] 针对 `all` 全部账号操作时必须显式加上 `--all` 参数。")
            raise typer.Exit(code=1)
        state = store.load()
        target_aliases = sorted(state.accounts.keys())
    else:
        try:
            target_aliases = [store.resolve_alias(alias)]
        except KeyError as e:
            err_console.print(f"[red]错误:[/] {e}")
            raise typer.Exit(code=1)

    if stop_all and not yes:
        confirmed = typer.confirm(
            f"确认关停以下账号下的全部活跃实例：{', '.join(target_aliases)}？"
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
                    console.print(f"[green][{acct}] 已关停实例[/] {ep}")
            else:
                console.print(f"[dim][{acct}] 当前无运行中实例。[/]")
        except Exception as e:
            err_console.print(f"[red][{acct}] 关停失败:[/] {e}")

    if stop_all:
        console.print(f"[bold]共关停实例数:[/] {total_stopped}")


@app.command("env")
def env_cmd(
    alias: Annotated[Optional[str], typer.Argument(help="账号别名（省略则使用默认账号）")] = None,
    shell: Annotated[str, typer.Option("--shell", help="Shell 语法类型：posix（zsh/bash）或 fish")] = "posix",
) -> None:
    """输出环境变量与 alias 片段，执行 `eval \"$(cm env <别名>)\"` 即可将当前终端固定为该账号。"""
    store = _get_store()
    try:
        snippet = generate_shell_env(store, alias or "default", shell=shell)
    except (KeyError, FileNotFoundError) as e:
        err_console.print(f"[red]错误:[/] {e}")
        raise typer.Exit(code=1)
    typer.echo(snippet)


@app.command("version")
def version_cmd() -> None:
    """显示 colab-manager 版本号与本地配置目录。"""
    store = _get_store()
    console.print(f"colab-manager [bold]{__version__}[/]（配置目录: {store.root}）")


def main() -> None:
    """入口函数：在 Typer 解析前优先支持 `cm @<别名> ...` 快捷透传语法。"""
    if len(sys.argv) >= 2 and sys.argv[1].startswith("@"):
        alias = sys.argv[1][1:]
        forwarded = sys.argv[2:]
        if not alias:
            err_console.print("[red]错误:[/] 请在 '@' 后指定账号别名，例如 `cm @main usage`。")
            raise SystemExit(1)
        if not forwarded:
            err_console.print(f"[red]错误:[/] 请在 '@{alias}' 后提供要执行的 colab 子命令，例如 `cm @{alias} sessions`。")
            raise SystemExit(1)
        try:
            _dispatch_proxy(alias, forwarded)
        except typer.Exit as exc:
            raise SystemExit(exc.exit_code)
    app()


if __name__ == "__main__":
    main()
