"""Tests for CLI filtering, runner invocation, and JSON usage output."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from colab_manager.cli import _extract_flag_value, _filter_and_sort_usage, app
from colab_manager.models import AccountUsageReport, AssignmentDetail
from colab_manager.runner import build_colab_invocation, generate_shell_env
from colab_manager.store import AccountStore

runner = CliRunner()


def _sample_reports() -> list[AccountUsageReport]:
    return [
        AccountUsageReport(
            alias="acc_free",
            email="free@example.com",
            ok=True,
            balance=12.5,
            rate_hourly=0.0,
            assignments_count=0,
            eligible_gpus=["T4"],
            eligible_tpus=[],
        ),
        AccountUsageReport(
            alias="acc_pro",
            email="pro@example.com",
            is_default=True,
            ok=True,
            balance=1500.0,
            rate_hourly=2.5,
            assignments_count=1,
            eligible_gpus=["H100", "A100", "L4", "T4"],
            eligible_tpus=["V6E1", "V5E1"],
            assignments=[
                AssignmentDetail(
                    alias="acc_pro",
                    email="pro@example.com",
                    session_name="train",
                    endpoint="m-s-abc",
                    accelerator="A100",
                    machine_shape="High-RAM",
                    variant="GPU",
                )
            ],
        ),
        AccountUsageReport(
            alias="acc_broken",
            email="broken@example.com",
            ok=False,
            error="invalid_grant",
        ),
    ]


def test_filter_and_sort_usage():
    reports = _sample_reports()

    by_balance = _filter_and_sort_usage(reports, sort_by="balance", ascending=False)
    assert [r.alias for r in by_balance] == ["acc_pro", "acc_free", "acc_broken"]

    only_a100 = _filter_and_sort_usage(reports, gpu="a100")
    assert [r.alias for r in only_a100] == ["acc_pro"]

    only_active = _filter_and_sort_usage(reports, only_active=True)
    assert [r.alias for r in only_active] == ["acc_pro"]

    min_bal = _filter_and_sort_usage(reports, min_balance=100)
    assert [r.alias for r in min_bal] == ["acc_pro"]


def test_extract_flag_value():
    assert _extract_flag_value(["new", "--gpu", "a100", "--high-mem"], "--gpu") == "a100"
    assert _extract_flag_value(["new", "--gpu=t4"], "--gpu") == "t4"
    assert _extract_flag_value(["new", "--tpu", "v5e1"], "--gpu") is None


def test_runner_builds_isolated_adc_invocation(tmp_path: Path):
    store = AccountStore(tmp_path)
    store.upsert_account("acc1", {"refresh_token": "r", "client_id": "c", "client_secret": "s"})

    with patch("colab_manager.runner.find_colab_executable", return_value="/usr/local/bin/colab"):
        cmd, env = build_colab_invocation(store, "acc1", ["new", "--gpu", "t4"])
        assert cmd == [
            "/usr/local/bin/colab",
            "--auth=adc",
            "--config",
            str(store.sessions_path("acc1")),
            "new",
            "--gpu",
            "t4",
        ]
        assert env["GOOGLE_APPLICATION_CREDENTIALS"] == str(store.credentials_path("acc1"))

        posix_snippet = generate_shell_env(store, "acc1", shell="posix")
        assert "GOOGLE_APPLICATION_CREDENTIALS=" in posix_snippet
        assert "alias colab=" in posix_snippet

        fish_snippet = generate_shell_env(store, "acc1", shell="fish")
        assert "set -gx GOOGLE_APPLICATION_CREDENTIALS" in fish_snippet


def test_cli_usage_json_and_pick(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COLAB_MANAGER_HOME", str(tmp_path))
    sample = _sample_reports()

    with patch("colab_manager.cli.fetch_accounts_usage_concurrent", return_value=sample):
        res = runner.invoke(app, ["usage", "--json"])
        assert res.exit_code == 0
        payload = json.loads(res.stdout)
        assert payload["summary"]["accounts_total"] == 3
        assert payload["summary"]["accounts_ok"] == 2
        assert payload["summary"]["total_balance"] == 1512.5
        assert payload["summary"]["total_rate_hourly"] == 2.5
        assert payload["summary"]["total_active_assignments"] == 1

        pick_res = runner.invoke(app, ["pick", "--gpu", "h100"])
        assert pick_res.exit_code == 0
        assert pick_res.stdout.strip() == "acc_pro"

        pick_idle = runner.invoke(app, ["pick", "--idle"])
        assert pick_idle.exit_code == 0
        assert pick_idle.stdout.strip() == "acc_free"

        pick_none = runner.invoke(app, ["pick", "--gpu", "H100", "--idle"])
        assert pick_none.exit_code == 1


def test_cli_account_crud_from_file_and_sessions_json(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COLAB_MANAGER_HOME", str(tmp_path / "mgr"))
    cred_file = tmp_path / "adc.json"
    cred_file.write_text(
        json.dumps(
            {
                "refresh_token": "fake-refresh",
                "client_id": "cid",
                "client_secret": "csec",
            }
        ),
        encoding="utf-8",
    )

    with patch("colab_manager.cli.extract_email_from_credentials", return_value="u1@example.com"):
        r_add = runner.invoke(
            app,
            ["auth", "add", "acc1", "--from-file", str(cred_file), "--note", "first"],
        )
        assert r_add.exit_code == 0
        assert "Saved account acc1" in r_add.stdout

    r_ls = runner.invoke(app, ["ls"])
    assert r_ls.exit_code == 0
    assert "acc1" in r_ls.stdout

    r_note = runner.invoke(app, ["auth", "note", "acc1", "updated-note"])
    assert r_note.exit_code == 0

    r_ren = runner.invoke(app, ["auth", "rename", "acc1", "work"])
    assert r_ren.exit_code == 0

    r_use = runner.invoke(app, ["use", "work"])
    assert r_use.exit_code == 0

    r_env = runner.invoke(app, ["env", "work"])
    assert r_env.exit_code == 0
    assert "GOOGLE_APPLICATION_CREDENTIALS=" in r_env.stdout

    with patch("colab_manager.cli.fetch_accounts_usage_concurrent", return_value=_sample_reports()):
        r_sess = runner.invoke(app, ["sessions", "--json"])
        assert r_sess.exit_code == 0
        items = json.loads(r_sess.stdout)
        assert len(items) == 1
        assert items[0]["session_name"] == "train"
        assert items[0]["accelerator"] == "A100"

    r_rm = runner.invoke(app, ["auth", "rm", "work", "--yes"])
    assert r_rm.exit_code == 0
    assert "Removed account work" in r_rm.stdout


def test_main_at_alias_and_best_shortcut_dispatch(tmp_path: Path, monkeypatch):
    from colab_manager import cli as cli_mod

    monkeypatch.setenv("COLAB_MANAGER_HOME", str(tmp_path))
    store = AccountStore(tmp_path)
    store.upsert_account("acc_pro", {"refresh_token": "r", "client_id": "c", "client_secret": "s"})

    called = {}

    def fake_run(_store, alias, colab_args):
        called["alias"] = alias
        called["args"] = colab_args
        return 0

    monkeypatch.setattr("sys.argv", ["cm", "@best", "new", "--gpu", "A100", "--high-mem"])
    with (
        patch("colab_manager.cli.fetch_accounts_usage_concurrent", return_value=_sample_reports()),
        patch("colab_manager.cli.run_colab_command", side_effect=fake_run),
    ):
        try:
            cli_mod.main()
        except SystemExit as e:
            assert e.code == 0

    assert called["alias"] == "acc_pro"
    assert called["args"] == ["new", "--gpu", "A100", "--high-mem"]

