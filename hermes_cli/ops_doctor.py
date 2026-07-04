"""Fast read-only operational smoke checks for local Hermes/brain systems."""

from __future__ import annotations

import json
import socket
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path



@dataclass
class OpsCheck:
    name: str
    state: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    next_action: str | None = None


@dataclass
class OpsReport:
    state: str
    checks: list[OpsCheck]

    @property
    def counts(self) -> dict[str, int]:
        counts = {"ok": 0, "warn": 0, "fail": 0}
        for check in self.checks:
            counts[check.state] = counts.get(check.state, 0) + 1
        return counts


_SERVICE_CHECKS: tuple[tuple[str, str], ...] = (
    ("brain-bridge", "Brain bridge control-plane sidecar"),
    ("lifeos", "LifeOS user service"),
    ("brain-alert-notification", "Brain alert notification path"),
)

_SOCKET_CHECKS: tuple[tuple[str, str, int], ...] = (
    ("brain-bridge HTTP", "127.0.0.1", 3001),
    ("second-brain MCP", "127.0.0.1", 8011),
    ("Langfuse", "127.0.0.1", 3030),
)

_LOG_CHECKS: tuple[tuple[str, Path], ...] = (
    (
        "MCP read-only health evidence",
        Path.home() / ".hermes" / "state" / "mcp-recovery-health-check" / "latest-postcondition.json",
    ),
    ("brain-bridge log", Path.home() / "logs" / "brain-bridge.log"),
)


def _run(command: list[str], *, timeout: float = 2.0) -> tuple[int | None, str, str]:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError as exc:
        return None, "", str(exc)
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout:g}s"


def _service_check(service: str, label: str) -> OpsCheck:
    code, out, err = _run(["systemctl", "--user", "is-active", service])
    status = out or err or "unknown"
    evidence = [f"systemctl --user is-active {service}: {status}"]
    if code == 0 and status == "active":
        return OpsCheck(service, "ok", f"{label} is active", evidence)
    timer = service.removesuffix(".service") + ".timer"
    timer_code, timer_out, timer_err = _run(["systemctl", "--user", "is-active", timer])
    show_code, show_out, show_err = _run(
        ["systemctl", "--user", "show", service, "-p", "Type", "-p", "Result", "-p", "ExecMainStatus"],
        timeout=2.0,
    )
    if timer_code == 0:
        evidence.append(f"systemctl --user is-active {timer}: {timer_out or timer_err or 'unknown'}")
    if show_code == 0 and show_out:
        evidence.extend(line for line in show_out.splitlines() if line)
    if status == "inactive" and timer_code == 0 and "Result=success" in show_out:
        return OpsCheck(
            service,
            "ok",
            f"{label} is a timer-driven oneshot; last run succeeded",
            evidence,
        )
    if code is None and "No such file" in status:
        return OpsCheck(
            service,
            "warn",
            "systemctl is unavailable in this environment",
            evidence,
            "Run this check on the host with systemd user services enabled.",
        )
    return OpsCheck(
        service,
        "warn",
        f"{label} is not active ({status})",
        evidence,
        f"Inspect with: systemctl --user status {service}",
    )


def _socket_check(name: str, host: str, port: int) -> OpsCheck:
    evidence = [f"tcp connect {host}:{port} timeout=1s"]
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return OpsCheck(name, "ok", f"{host}:{port} is accepting TCP connections", evidence)
    except OSError as exc:
        return OpsCheck(
            name,
            "warn",
            f"{host}:{port} is not reachable ({exc})",
            evidence,
            f"Verify the owning service and port binding for {host}:{port}.",
        )


def _cron_split_store_check() -> OpsCheck:
    host_path = Path.home() / ".hermes" / "cron" / "jobs.json"
    docker_probe = _run(["docker", "exec", "hermes", "test", "-f", "/opt/data/cron/jobs.json"], timeout=2.0)
    evidence = [f"host cron store: {host_path} exists={host_path.exists()}"]
    docker_exists = docker_probe[0] == 0
    if docker_probe[0] is None:
        evidence.append(f"docker probe unavailable: {docker_probe[2]}")
    else:
        evidence.append(f"docker hermes /opt/data/cron/jobs.json exists={docker_exists}")
    if host_path.exists() and docker_exists:
        return OpsCheck(
            "cron stores",
            "ok",
            "Both host and Docker cron stores are visible for merged health views",
            evidence,
        )
    if host_path.exists() or docker_exists:
        return OpsCheck("cron stores", "ok", "One cron store is visible", evidence)
    return OpsCheck(
        "cron stores",
        "warn",
        "No cron jobs.json store found in host or Docker probe",
        evidence,
        "Run `hermes cron list` or inspect the scheduler deployment.",
    )


def _log_freshness_check(name: str, path: Path) -> OpsCheck:
    evidence = [str(path)]
    if not path.exists():
        return OpsCheck(name, "warn", "log file is missing", evidence, f"Confirm the producer writes {path}")
    try:
        stat = path.stat()
        size = stat.st_size
        age_seconds = max(0.0, time.time() - stat.st_mtime)
        tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-3:]
    except OSError as exc:
        return OpsCheck(name, "warn", f"could not read log ({exc})", evidence)
    evidence.append(f"size={size} bytes")
    evidence.append(f"mtime_age={age_seconds / 3600:.1f}h")
    evidence.extend(f"tail: {line[:180]}" for line in tail)
    has_failure_tail = any("error" in line.lower() or "fail" in line.lower() for line in tail)
    is_stale = age_seconds > 24 * 3600
    state = "warn" if has_failure_tail or is_stale else "ok"
    if has_failure_tail:
        summary = "recent log tail contains failure language"
    elif is_stale:
        summary = "log exists but has not been updated in over 24h"
    else:
        summary = "log exists and recent tail is readable"
    return OpsCheck(name, state, summary, evidence)


def _git_state_check(repo: Path | None) -> OpsCheck:
    if repo is None:
        repo = Path.cwd()
    if not (repo / ".git").exists():
        return OpsCheck("git state", "warn", f"{repo} is not a git checkout", [str(repo)])
    code, out, err = _run(["git", "status", "--short", "--branch"], timeout=3.0)
    evidence = [out or err]
    if code != 0:
        return OpsCheck("git state", "warn", "git status failed", evidence)
    first = (out.splitlines() or [""])[0]
    details = "dirty/sync state recorded for operator context" if len(out.splitlines()) > 1 or "behind" in first or "ahead" in first else "clean"
    return OpsCheck("git state", "ok", f"{first or 'clean'} ({details})", evidence)


def collect_ops_report(repo: Path | None = None) -> OpsReport:
    checks: list[OpsCheck] = []
    checks.extend(_service_check(service, label) for service, label in _SERVICE_CHECKS)
    checks.extend(_socket_check(name, host, port) for name, host, port in _SOCKET_CHECKS)
    checks.append(_cron_split_store_check())
    checks.extend(_log_freshness_check(name, path) for name, path in _LOG_CHECKS)
    checks.append(_git_state_check(repo))
    if any(check.state == "fail" for check in checks):
        state = "blocked"
    elif any(check.state == "warn" for check in checks):
        state = "degraded"
    else:
        state = "healthy"
    return OpsReport(state=state, checks=checks)


def _mark(state: str) -> str:
    return {"ok": "✓", "warn": "⚠", "fail": "✗"}.get(state, "?")


def render_ops_report(report: OpsReport) -> str:
    lines = [
        "Hermes Ops Smoke Test",
        f"Overall: {report.state.upper()}  "
        f"(ok={report.counts.get('ok', 0)}, warn={report.counts.get('warn', 0)}, fail={report.counts.get('fail', 0)})",
        "",
    ]
    for check in report.checks:
        lines.append(f"{_mark(check.state)} {check.name}: {check.summary}")
        for item in check.evidence[:4]:
            if item:
                lines.append(f"    evidence: {item}")
        if check.next_action:
            lines.append(f"    next: {check.next_action}")
    actions = [check for check in report.checks if check.state != "ok" and check.next_action]
    if actions:
        lines.extend(["", "Recommended next actions:"])
        for index, check in enumerate(actions[:5], start=1):
            lines.append(f"{index}. {check.name}: {check.next_action}")
    return "\n".join(lines) + "\n"


def run_ops_doctor(args) -> int:
    report = collect_ops_report(Path.cwd())
    if getattr(args, "json", False):
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        print(render_ops_report(report), end="")
    return 0 if report.state == "healthy" else 1
