"""Tests for the focused Hermes ops doctor."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import ops_doctor
from hermes_cli.subcommands.doctor import build_doctor_parser


def test_collect_ops_report_degrades_on_unreachable_dependencies(monkeypatch, tmp_path):
    calls: list[tuple[str, ...]] = []

    def fake_run(command, *, timeout=2.0):
        calls.append(tuple(command))
        if command[:3] == ["systemctl", "--user", "is-active"]:
            return (3, "inactive", "")
        if command[:2] == ["docker", "exec"]:
            return (1, "", "")
        if command[:2] == ["git", "status"]:
            return (0, "## main...origin/main [behind 2]", "")
        return (0, "", "")

    def fake_socket(*args, **kwargs):
        raise OSError("refused")

    monkeypatch.setattr(ops_doctor, "_run", fake_run)
    monkeypatch.setattr(ops_doctor.socket, "create_connection", fake_socket)
    monkeypatch.setattr(
        ops_doctor,
        "_LOG_CHECKS",
        (("test log", tmp_path / "missing.log"),),
    )

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    report = ops_doctor.collect_ops_report(repo)

    assert report.state == "degraded"
    assert any(check.name == "brain-bridge" for check in report.checks)
    assert any(check.name == "second-brain MCP" for check in report.checks)
    assert any("systemctl" in item for check in report.checks for item in check.evidence)
    assert ("git", "status", "--short", "--branch") in calls
    git_check = next(check for check in report.checks if check.name == "git state")
    assert git_check.state == "ok"


def test_render_ops_report_includes_evidence_and_next_action():
    report = ops_doctor.OpsReport(
        state="degraded",
        checks=[
            ops_doctor.OpsCheck(
                name="example",
                state="warn",
                summary="not ideal",
                evidence=["probe: failed"],
                next_action="inspect it",
            )
        ],
    )

    text = ops_doctor.render_ops_report(report)

    assert "Overall: DEGRADED" in text
    assert "example: not ideal" in text
    assert "evidence: probe: failed" in text
    assert "Recommended next actions" in text


def test_log_freshness_warns_when_log_is_stale(monkeypatch, tmp_path):
    log = tmp_path / "old.log"
    log.write_text("all good\n", encoding="utf-8")
    monkeypatch.setattr(ops_doctor.time, "time", lambda: log.stat().st_mtime + (25 * 3600))

    check = ops_doctor._log_freshness_check("old log", log)

    assert check.state == "warn"
    assert "over 24h" in check.summary
    assert any(item.startswith("mtime_age=") for item in check.evidence)


def test_timer_driven_oneshot_inactive_success_is_ok(monkeypatch):
    def fake_run(command, *, timeout=2.0):
        if command == ["systemctl", "--user", "is-active", "brain-alert-notification"]:
            return (3, "inactive", "")
        if command == ["systemctl", "--user", "is-active", "brain-alert-notification.timer"]:
            return (0, "active", "")
        if command[:4] == ["systemctl", "--user", "show", "brain-alert-notification"]:
            return (0, "Type=oneshot\nResult=success\nExecMainStatus=0", "")
        return (1, "", "")

    monkeypatch.setattr(ops_doctor, "_run", fake_run)

    check = ops_doctor._service_check("brain-alert-notification", "Brain alert notification path")

    assert check.state == "ok"
    assert "timer-driven oneshot" in check.summary


def test_doctor_parser_accepts_ops_mode_and_json():
    import argparse

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")

    def handler(args):
        return args

    build_doctor_parser(sub, cmd_doctor=handler)
    args = parser.parse_args(["doctor", "ops", "--json"])

    assert args.command == "doctor"
    assert args.mode == "ops"
    assert args.json is True
    assert args.func is handler


def test_run_ops_doctor_json_outputs_report(monkeypatch, capsys):
    monkeypatch.setattr(
        ops_doctor,
        "collect_ops_report",
        lambda repo=None: ops_doctor.OpsReport(
            state="healthy",
            checks=[ops_doctor.OpsCheck("sample", "ok", "fine")],
        ),
    )

    code = ops_doctor.run_ops_doctor(Namespace(json=True))
    out = capsys.readouterr().out

    assert code == 0
    assert '"state": "healthy"' in out
    assert '"name": "sample"' in out
