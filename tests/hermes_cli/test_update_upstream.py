"""Tests for _get_update_target — upstream branch resolution in hermes update."""

import subprocess
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# _get_update_target unit tests
# ---------------------------------------------------------------------------

class TestGetUpdateTarget:
    """Test upstream resolution for hermes update."""

    def _make_run(self, head_output, upstream_output=None, upstream_rc=0):
        """Build a subprocess.run side-effect that fakes git rev-parse calls."""
        def fake_run(cmd, **kwargs):
            if "rev-parse" in cmd and "--abbrev-ref" in cmd and "HEAD" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=head_output, stderr="")
            if "rev-parse" in cmd and "@{u}" in cmd:
                return subprocess.CompletedProcess(
                    cmd, upstream_rc,
                    stdout=upstream_output or "", stderr="" if upstream_rc == 0 else "fatal: no upstream\n",
                )
            raise AssertionError(f"Unexpected command: {cmd}")
        return fake_run

    def test_uses_tracking_upstream(self):
        """Branch tracking fork/feature should resolve to that remote."""
        from hermes_cli.main import _get_update_target

        fake = self._make_run("fix/discord\n", "fork/fix/discord\n")
        with patch("subprocess.run", side_effect=fake):
            branch, remote, remote_branch, upstream_ref = _get_update_target(["git"])

        assert branch == "fix/discord"
        assert remote == "fork"
        assert remote_branch == "fix/discord"
        assert upstream_ref == "fork/fix/discord"

    def test_falls_back_to_origin_when_no_upstream(self):
        """Branch with no upstream should default to origin/<branch>."""
        from hermes_cli.main import _get_update_target

        fake = self._make_run("feature/local\n", upstream_rc=128)
        with patch("subprocess.run", side_effect=fake):
            branch, remote, remote_branch, upstream_ref = _get_update_target(["git"])

        assert branch == "feature/local"
        assert remote == "origin"
        assert remote_branch == "feature/local"
        assert upstream_ref == "origin/feature/local"

    def test_main_branch_with_origin_upstream(self):
        """Standard main branch tracking origin/main."""
        from hermes_cli.main import _get_update_target

        fake = self._make_run("main\n", "origin/main\n")
        with patch("subprocess.run", side_effect=fake):
            branch, remote, remote_branch, upstream_ref = _get_update_target(["git"])

        assert branch == "main"
        assert remote == "origin"
        assert remote_branch == "main"
        assert upstream_ref == "origin/main"

    def test_detached_head_raises(self):
        """Detached HEAD should raise RuntimeError."""
        from hermes_cli.main import _get_update_target

        fake = self._make_run("HEAD\n")
        with patch("subprocess.run", side_effect=fake):
            with pytest.raises(RuntimeError, match="detached HEAD"):
                _get_update_target(["git"])

    def test_upstream_with_nested_slashes(self):
        """Upstream like origin/fix/deep/path should split on first slash only."""
        from hermes_cli.main import _get_update_target

        fake = self._make_run("fix/deep/path\n", "origin/fix/deep/path\n")
        with patch("subprocess.run", side_effect=fake):
            branch, remote, remote_branch, upstream_ref = _get_update_target(["git"])

        assert remote == "origin"
        assert remote_branch == "fix/deep/path"
        assert upstream_ref == "origin/fix/deep/path"
