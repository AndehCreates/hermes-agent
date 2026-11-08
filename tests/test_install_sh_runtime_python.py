"""Exercise the native path stage against real Python processes and private homes."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parent.parent / "scripts" / "install.sh"
SHIMS = ("hermes", "hermes-agent", "hermes-acp")


@pytest.fixture
def layout(tmp_path):
    home = tmp_path / "home"
    source = tmp_path / "immutable source"
    command_dir = home / ".local" / "bin"
    command_dir.mkdir(parents=True)
    source.mkdir()
    (home / ".bashrc").write_text("# existing shell state\n", encoding="utf-8")
    (source / "source_marker.py").write_text(
        "VALUE = 'immutable-candidate'\n", encoding="utf-8"
    )
    entrypoint = """import json, os, sys
import source_marker
print(json.dumps({
    "source": source_marker.VALUE,
    "module": source_marker.__file__,
    "python": sys.executable,
    "argv": sys.argv[1:],
    "pythonpath": os.environ.get("PYTHONPATH"),
    "pythonhome": os.environ.get("PYTHONHOME"),
    "home": os.environ.get("HOME"),
    "hermes_home": os.environ.get("HERMES_HOME"),
}))
"""
    for name in ("hermes", "run_agent.py"):
        (source / name).write_text(entrypoint, encoding="utf-8")
    for name in SHIMS:
        (command_dir / name).write_text("prior-" + name + "\n", encoding="utf-8")
    hostile = tmp_path / "shadow"
    hostile.mkdir()
    (hostile / "source_marker.py").write_text("VALUE = 'wrong-source'\n")
    env = os.environ.copy()
    env.update(
        HOME=str(home),
        HERMES_HOME=str(home / "state"),
        PATH=str(command_dir) + os.pathsep + os.environ["PATH"],
        PYTHONPATH=str(hostile),
        PYTHONHOME=str(tmp_path / "invalid-python-home"),
    )
    env.pop("HERMES_INSTALL_DIR", None)
    return home, source, command_dir, env


def install(layout, args):
    _, source, _, env = layout
    return subprocess.run(
        ["bash", str(INSTALLER), "--dir", str(source), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def snapshot(layout):
    home, _, command_dir, _ = layout
    return {
        name: (command_dir / name).read_bytes() for name in SHIMS
    } | {".bashrc": (home / ".bashrc").read_bytes()}


def assert_source_launchers(layout, runtime):
    home, source, command_dir, env = layout
    forwarded = ["argument with spaces", 'literal "$value"']
    for name in SHIMS:
        result = subprocess.run(
            [str(command_dir / name), *forwarded],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        observed = json.loads(result.stdout)
        assert observed["source"] == "immutable-candidate"
        assert Path(observed["module"]).resolve() == source / "source_marker.py"
        assert os.path.samefile(observed["python"], runtime)
        assert observed["argv"] == (["acp"] if name == "hermes-acp" else []) + forwarded
        assert observed["pythonpath"] is None and observed["pythonhome"] is None
        assert observed["home"] == str(home)
        assert observed["hermes_home"] == env["HERMES_HOME"]


def test_explicit_runtime_keeps_source_separate_from_existing_venv(layout, tmp_path):
    _, source, _, _ = layout
    runtime = tmp_path / "retained runtime" / "python"
    runtime.parent.mkdir()
    runtime.symlink_to(sys.executable)
    poison = source / "venv" / "bin" / "python"
    poison.parent.mkdir(parents=True)
    poison.write_text("#!/bin/sh\nexit 77\n", encoding="utf-8")
    poison.chmod(0o755)
    before = poison.read_bytes()

    result = install(
        layout, ["--stage", "path", "--runtime-python", str(runtime), "--json"]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert_source_launchers(layout, runtime)
    assert poison.read_bytes() == before


def test_runtime_and_source_shell_metacharacters_are_literal(layout, tmp_path):
    home, source, command_dir, env = layout
    escaped_source = source.with_name('source "$USER" `literal`')
    source.rename(escaped_source)
    layout = home, escaped_source, command_dir, env
    runtime = tmp_path / 'runtime "$(touch runtime-injected)" `literal`'
    runtime.symlink_to(sys.executable)
    result = install(
        layout, ["--stage", "path", "--runtime-python", str(runtime)]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert_source_launchers(layout, runtime)
    assert not (escaped_source / "runtime-injected").exists()
    assert not (Path.cwd() / "runtime-injected").exists()


@pytest.mark.parametrize(
    "kind", ["missing", "relative", "directory", "nonexecutable", "not-python", "newline"]
)
def test_invalid_runtime_refuses_before_replacing_any_launcher(layout, tmp_path, kind):
    runtime = tmp_path / "runtime"
    if kind == "relative":
        runtime = Path("relative-python")
    elif kind == "directory":
        runtime.mkdir()
    elif kind == "nonexecutable":
        runtime.write_text("not executable\n")
    elif kind == "not-python":
        runtime.write_text("#!/bin/sh\nexit 0\n")
        runtime.chmod(0o755)
    elif kind == "newline":
        runtime = tmp_path / "runtime\npython"
        runtime.symlink_to(sys.executable)
    before = snapshot(layout)
    result = install(
        layout, ["--stage", "path", "--runtime-python", str(runtime), "--json"]
    )
    assert result.returncode != 0
    assert snapshot(layout) == before


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--stage", "venv"],
        ["--stage", "python-deps"],
        ["--stage", "repository"],
        ["--stage", "path", "--no-venv"],
        ["--stage", "path", "--manifest"],
        ["--stage", "path", "--ensure", "node"],
    ],
)
def test_runtime_override_cannot_enter_environment_mutation_stages(layout, args):
    before = snapshot(layout)
    result = install(layout, [*args, "--runtime-python", sys.executable, "--json"])
    assert result.returncode != 0
    assert snapshot(layout) == before
    assert not (layout[1] / "venv").exists()


@pytest.mark.parametrize("missing", ["hermes", "run_agent.py"])
def test_missing_source_entrypoint_refuses_before_any_launcher_write(layout, missing):
    (layout[1] / missing).unlink()
    before = snapshot(layout)
    result = install(
        layout, ["--stage", "path", "--runtime-python", sys.executable, "--json"]
    )
    assert result.returncode != 0
    assert snapshot(layout) == before


@pytest.mark.parametrize("args", [["--runtime-python"], ["--runtime-python", ""]])
def test_missing_runtime_argument_is_rejected_without_effect(layout, args):
    before = snapshot(layout)
    result = install(layout, ["--stage", "path", *args])
    assert result.returncode != 0
    assert snapshot(layout) == before


def test_default_path_stage_still_uses_the_install_venv(layout):
    source = layout[1]
    runtime = source / "venv" / "bin" / "python"
    runtime.parent.mkdir(parents=True)
    runtime.symlink_to(sys.executable)
    result = install(layout, ["--stage", "path", "--json"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert_source_launchers(layout, runtime)
