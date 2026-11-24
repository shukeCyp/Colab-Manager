"""Rich 终端中文表格与监控面板渲染。"""

from __future__ import annotations

from collections import Counter
from typing import List, Optional

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from colab_manager.models import (
    AccountRecord,
    AccountUsageReport,
    AssignmentDetail,
    AuthCheckReport,
)

# 按算力档次排序，高端卡靠前展示
_GPU_ORDER = ["H100", "A100", "G4", "L4", "T4"]
_TPU_ORDER = ["V6E1", "V5E1"]


def _sort_accelerators(items: List[str], order: List[str]) -> List[str]:
    rank = {name: i for i, name in enumerate(order)}
    return sorted(items, key=lambda x: (rank.get(x.upper(), 99), x.upper()))


def _format_balance(balance: Optional[float]) -> Text:
    if balance is None:
        return Text("-", style="dim")
    label = f"{balance:,.2f}"
    if balance >= 100:
        return Text(label, style="bold green")
    if balance >= 20:
        return Text(label, style="yellow")
    if balance > 0:
        return Text(label, style="bold red")
    return Text(label, style="dim red")


def _format_rate(rate: Optional[float]) -> Text:
    if rate is None:
        return Text("-", style="dim")
    if rate <= 0:
        return Text("0.00/时", style="dim")
    return Text(f"{rate:.2f}/时", style="bold cyan")


def _format_est_left(hours: Optional[float], rate: Optional[float]) -> Text:
    if rate is None:
        return Text("-", style="dim")
    if rate <= 0:
        return Text("空闲", style="dim")
    if hours is None:
        return Text("-", style="dim")
    if hours >= 48:
        days = hours / 24.0
        return Text(f"约{days:.1f}天", style="green")
    if hours >= 10:
        return Text(f"约{hours:.1f}时", style="yellow")
    return Text(f"约{hours:.1f}时", style="bold red")


def _format_shape_cn(shape: str) -> str:
    if shape == "High-RAM":
        return "高内存"
    if shape == "Standard":
        return "标准"
    return shape


def _format_active_summary(report: AccountUsageReport) -> Text:
    if not report.ok:
        return Text("-", style="dim")
    if report.assignments_count == 0 and not report.assignments:
        return Text("0", style="dim")

    if report.assignments:
        counts = Counter(
            f"{a.accelerator}" + ("-高内存" if a.machine_shape == "High-RAM" else "")
            for a in report.assignments
        )
        parts = [f"{cnt}x{hw}" if cnt > 1 else hw for hw, cnt in counts.items()]
        return Text(f"{len(report.assignments)} ({', '.join(parts)})", style="bold magenta")

    return Text(str(report.assignments_count), style="bold magenta")


def _format_eligible(gpus: List[str], tpus: List[str]) -> Text:
    text = Text()
    sorted_gpus = _sort_accelerators(gpus, _GPU_ORDER)
    sorted_tpus = _sort_accelerators(tpus, _TPU_ORDER)

    if not sorted_gpus and not sorted_tpus:
        return Text("-", style="dim")

    for i, gpu in enumerate(sorted_gpus):
        if i > 0:
            text.append(" ", style="dim")
        style = "bold bright_green" if gpu.upper() in ("H100", "A100") else "cyan"
        text.append(gpu.upper(), style=style)

    if sorted_tpus:
        if sorted_gpus:
            text.append("\n")
        text.append("TPU: ", style="dim")
        for i, tpu in enumerate(sorted_tpus):
            if i > 0:
                text.append(" ", style="dim")
            text.append(tpu.upper(), style="blue")

    return text


def build_usage_renderable(
    reports: List[AccountUsageReport],
    *,
    title: str = "Colab 多账号算力总览",
    show_eligible: bool = True,
) -> RenderableType:
    table = Table(
        title=title,
        box=box.ROUNDED,
        header_style="bold white",
        show_footer=True,
        expand=False,
    )

    ok_reports = [r for r in reports if r.ok]
    err_reports = [r for r in reports if not r.ok]
    total_balance = sum(r.balance or 0.0 for r in ok_reports)
    total_rate = sum(r.rate_hourly or 0.0 for r in ok_reports)
    total_active = sum(r.assignments_count for r in ok_reports)
    total_est = (total_balance / total_rate) if total_rate > 0 else None

    if total_est and total_est >= 48:
        total_est_str = f"约{total_est/24:.1f}天"
    elif total_est:
        total_est_str = f"约{total_est:.1f}时"
    else:
        total_est_str = "空闲"

    table.add_column(
        "账号",
        style="bold",
        footer=f"合计 ({len(ok_reports)}/{len(reports)} 正常)",
    )
    table.add_column(
        "余额(CU)",
        justify="right",
        no_wrap=True,
        footer=f"[bold green]{total_balance:,.2f}[/]",
    )
    table.add_column(
        "消耗速率",
        justify="right",
        no_wrap=True,
        footer=f"[bold cyan]{total_rate:.2f}/时[/]",
    )
    table.add_column(
        "预计续航",
        justify="right",
        no_wrap=True,
        footer=total_est_str,
    )
    table.add_column(
        "运行实例",
        no_wrap=True,
        footer=f"[bold magenta]{total_active}[/]",
    )
    if show_eligible:
        table.add_column("可用 GPU / TPU", footer="")
    table.add_column(
        "状态",
        no_wrap=True,
        footer=f"[red]{len(err_reports)} 异常[/]" if err_reports else "[green]正常[/]",
    )

    for r in reports:
        acct_label = Text()
        if r.is_default:
            acct_label.append("* ", style="bold yellow")
        acct_label.append(r.alias, style="bold white")
        if r.note:
            acct_label.append(f" [{r.note}]", style="dim cyan")
        if r.email:
            acct_label.append(f"\n  {r.email}", style="dim")

        status_text = Text("正常", style="green") if r.ok else Text("异常", style="bold red")

        row = [
            acct_label,
            _format_balance(r.balance),
            _format_rate(r.rate_hourly),
            _format_est_left(r.estimated_hours_left, r.rate_hourly),
            _format_active_summary(r),
        ]
        if show_eligible:
            row.append(_format_eligible(r.eligible_gpus, r.eligible_tpus))
        row.append(status_text)
        table.add_row(*row)

    if not err_reports:
        return table

    error_lines = [
        Text(f"  [{r.alias}] {r.error or '未知错误'}", style="red")
        for r in err_reports
    ]
    return Group(table, Text("异常详情:", style="bold red"), *error_lines)


def render_usage_table(
    console: Console,
    reports: List[AccountUsageReport],
    *,
    show_eligible: bool = True,
) -> None:
    if not reports:
        console.print("[yellow]未匹配到任何账号。请先运行 `cm import` 或 `cm add <别名>` 添加账号。[/]")
        return
    console.print(build_usage_renderable(reports, show_eligible=show_eligible))


def render_sessions_table(
    console: Console,
    reports: List[AccountUsageReport],
) -> None:
    rows: List[AssignmentDetail] = []
    errors: List[AccountUsageReport] = []
    for r in reports:
        if not r.ok:
            errors.append(r)
        else:
            rows.extend(r.assignments)

    table = Table(
        title=f"所有账号活跃实例总览（共查询 {len(reports)} 个账号）",
        box=box.ROUNDED,
        header_style="bold white",
    )
    table.add_column("账号", style="bold", no_wrap=True)
    table.add_column("会话名")
    table.add_column("硬件", style="bold cyan", no_wrap=True)
    table.add_column("内存规格", no_wrap=True)
    table.add_column("实例节点 (Endpoint)", style="dim")
    table.add_column("令牌剩余", justify="right", no_wrap=True)

    for a in rows:
        sess_text = (
            Text(a.session_name, style="bold green")
            if a.session_name
            else Text("(网页端/未托管)", style="dim yellow")
        )
        ttl = "-"
        if a.token_expires_in_seconds is not None:
            mins = a.token_expires_in_seconds // 60
            ttl = f"{mins}分钟"
        table.add_row(
            a.alias,
            sess_text,
            a.accelerator,
            _format_shape_cn(a.machine_shape),
            a.endpoint,
            ttl,
        )

    if rows:
        console.print(table)
    else:
        console.print("[dim]当前查询的账号下没有正在运行的 Colab 实例。[/]")

    for err in errors:
        console.print(f"[red][{err.alias}] 查询失败: {err.error}[/]")


def render_accounts_table(
    console: Console,
    accounts: List[AccountRecord],
    *,
    default_alias: Optional[str],
    local_session_counts: dict[str, int],
    cred_exists: dict[str, bool],
) -> None:
    if not accounts:
        console.print(
            Panel(
                "尚未添加任何账号。\n\n"
                "  [bold]cm import [别名][/]         一键导入本机原生 `colab` 已登录账号\n"
                "  [bold]cm add <别名>[/]            通过浏览器 OAuth 登录新增 Google 账号",
                title="Colab 多账号管理器",
                border_style="yellow",
            )
        )
        return

    table = Table(
        title="已管理的 Colab 账号列表",
        box=box.ROUNDED,
        header_style="bold white",
    )
    table.add_column("别名", style="bold", no_wrap=True)
    table.add_column("邮箱")
    table.add_column("备注")
    table.add_column("本地会话", justify="right", no_wrap=True)
    table.add_column("凭证状态", no_wrap=True)

    for rec in accounts:
        alias_text = Text()
        if rec.alias == default_alias:
            alias_text.append("* ", style="bold yellow")
        alias_text.append(rec.alias)

        cred_ok = cred_exists.get(rec.alias, False)
        table.add_row(
            alias_text,
            rec.email or "[dim]-[/]",
            rec.note or "[dim]-[/]",
            str(local_session_counts.get(rec.alias, 0)),
            "[green]就绪[/]" if cred_ok else "[red]缺失[/]",
        )

    console.print(table)


def render_auth_check_table(console: Console, reports: List[AuthCheckReport]) -> None:
    table = Table(
        title="OAuth 凭证健康检查",
        box=box.ROUNDED,
        header_style="bold white",
    )
    table.add_column("账号", style="bold", no_wrap=True)
    table.add_column("邮箱", overflow="ellipsis")
    table.add_column("刷新令牌", justify="center", no_wrap=True)
    table.add_column("访问剩余", justify="right", no_wrap=True)
    table.add_column("权限数", justify="right", no_wrap=True)
    table.add_column("状态", no_wrap=True)

    errors: List[AuthCheckReport] = []
    for r in reports:
        alias_text = Text()
        if r.is_default:
            alias_text.append("* ", style="bold yellow")
        alias_text.append(r.alias)

        ttl = "-"
        if r.expires_in_seconds is not None:
            ttl = f"{r.expires_in_seconds // 60}分钟"

        if not r.ok:
            errors.append(r)

        status = Text("正常", style="bold green") if r.ok else Text("失败", style="bold red")
        table.add_row(
            alias_text,
            r.live_email or r.recorded_email or "-",
            "[green]有[/]" if r.has_refresh_token else "[red]无[/]",
            ttl,
            str(len(r.scopes)) if r.scopes else "-",
            status,
        )

    console.print(table)
    if errors:
        console.print("[bold red]异常详情:[/]")
        for r in errors:
            console.print(f"  [red][{r.alias}] {r.error or '未知错误'}[/]")
