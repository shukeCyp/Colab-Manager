"""Rich terminal rendering for usage dashboards, sessions, and auth checks."""

from __future__ import annotations

from collections import Counter
from typing import Iterable, List, Optional

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

# Priority order so top-tier accelerators appear first
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
        return Text("0.00/hr", style="dim")
    return Text(f"{rate:.2f}/hr", style="bold cyan")


def _format_est_left(hours: Optional[float], rate: Optional[float]) -> Text:
    if rate is None:
        return Text("-", style="dim")
    if rate <= 0:
        return Text("idle", style="dim")
    if hours is None:
        return Text("-", style="dim")
    if hours >= 48:
        days = hours / 24.0
        return Text(f"~{days:.1f}d", style="green")
    if hours >= 10:
        return Text(f"~{hours:.1f}h", style="yellow")
    return Text(f"~{hours:.1f}h", style="bold red")


def _format_active_summary(report: AccountUsageReport) -> Text:
    if not report.ok:
        return Text("-", style="dim")
    if report.assignments_count == 0 and not report.assignments:
        return Text("0", style="dim")

    if report.assignments:
        counts = Counter(
            f"{a.accelerator}" + ("-HM" if a.machine_shape == "High-RAM" else "")
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
    title: str = "Colab Multi-Account Usage Overview",
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

    table.add_column(
        "Account",
        style="bold",
        footer=f"Total ({len(ok_reports)}/{len(reports)} ok)",
    )
    table.add_column(
        "Balance",
        justify="right",
        no_wrap=True,
        footer=f"[bold green]{total_balance:,.2f}[/]",
    )
    table.add_column(
        "Rate",
        justify="right",
        no_wrap=True,
        footer=f"[bold cyan]{total_rate:.2f}/hr[/]",
    )
    table.add_column(
        "Est.",
        justify="right",
        no_wrap=True,
        footer=f"~{total_est/24:.1f}d" if (total_est and total_est >= 48) else (f"~{total_est:.1f}h" if total_est else "idle"),
    )
    table.add_column(
        "Active",
        no_wrap=True,
        footer=f"[bold magenta]{total_active}[/]",
    )
    if show_eligible:
        table.add_column("Eligible GPUs / TPUs", footer="")
    table.add_column(
        "Status",
        no_wrap=True,
        footer=f"[red]{len(err_reports)} err[/]" if err_reports else "[green]OK[/]",
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

        status_text = Text("OK", style="green") if r.ok else Text("ERR", style="bold red")

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
        Text(f"  [{r.alias}] {r.error or 'Unknown error'}", style="red")
        for r in err_reports
    ]
    return Group(table, Text("Errors:", style="bold red"), *error_lines)


def render_usage_table(
    console: Console,
    reports: List[AccountUsageReport],
    *,
    show_eligible: bool = True,
) -> None:
    if not reports:
        console.print("[yellow]No accounts matched. Run `cm auth import` or `cm auth add <alias>` first.[/]")
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
        title=f"Active Colab Sessions Across {len(reports)} Account(s)",
        box=box.ROUNDED,
        header_style="bold white",
    )
    table.add_column("Account", style="bold")
    table.add_column("Session")
    table.add_column("Hardware", style="bold cyan")
    table.add_column("Shape")
    table.add_column("Endpoint", style="dim")
    table.add_column("Token TTL", justify="right")

    for a in rows:
        sess_text = (
            Text(a.session_name, style="bold green")
            if a.session_name
            else Text("(web/untracked)", style="dim yellow")
        )
        ttl = "-"
        if a.token_expires_in_seconds is not None:
            mins = a.token_expires_in_seconds // 60
            ttl = f"{mins}m"
        table.add_row(
            a.alias,
            sess_text,
            a.accelerator,
            a.machine_shape,
            a.endpoint,
            ttl,
        )

    if rows:
        console.print(table)
    else:
        console.print("[dim]No active remote Colab assignments found across queried accounts.[/]")

    for err in errors:
        console.print(f"[red][{err.alias}] Query failed: {err.error}[/]")


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
                "No accounts registered yet.\n\n"
                "  [bold]cm auth import[/]            Import currently logged-in native `colab` account\n"
                "  [bold]cm auth add <alias>[/]       Sign in a new Google account via browser OAuth",
                title="Colab Manager",
                border_style="yellow",
            )
        )
        return

    table = Table(
        title="Managed Colab Accounts",
        box=box.ROUNDED,
        header_style="bold white",
    )
    table.add_column("Alias", style="bold")
    table.add_column("Email")
    table.add_column("Note")
    table.add_column("Local Sessions", justify="right")
    table.add_column("Credentials")

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
            "[green]ready[/]" if cred_ok else "[red]missing[/]",
        )

    console.print(table)


def render_auth_check_table(console: Console, reports: List[AuthCheckReport]) -> None:
    table = Table(
        title="OAuth Credential Health Check",
        box=box.ROUNDED,
        header_style="bold white",
    )
    table.add_column("Account", style="bold", no_wrap=True)
    table.add_column("Email", overflow="ellipsis")
    table.add_column("Refresh", justify="center", no_wrap=True)
    table.add_column("TTL", justify="right", no_wrap=True)
    table.add_column("Scopes", justify="right", no_wrap=True)
    table.add_column("Status", no_wrap=True)

    errors: List[AuthCheckReport] = []
    for r in reports:
        alias_text = Text()
        if r.is_default:
            alias_text.append("* ", style="bold yellow")
        alias_text.append(r.alias)

        ttl = "-"
        if r.expires_in_seconds is not None:
            ttl = f"{r.expires_in_seconds // 60}m"

        if not r.ok:
            errors.append(r)

        status = Text("OK", style="bold green") if r.ok else Text("FAIL", style="bold red")
        table.add_row(
            alias_text,
            r.live_email or r.recorded_email or "-",
            "[green]yes[/]" if r.has_refresh_token else "[red]no[/]",
            ttl,
            str(len(r.scopes)) if r.scopes else "-",
            status,
        )

    console.print(table)
    if errors:
        console.print("[bold red]Errors:[/]")
        for r in errors:
            console.print(f"  [red][{r.alias}] {r.error or 'Unknown error'}[/]")
