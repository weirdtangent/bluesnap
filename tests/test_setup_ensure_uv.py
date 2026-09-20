"""
Regression tests for uv discovery after a fresh install.

The uv installer drops the binary in ~/.local/bin and appends that directory to
the user's shell profile. bluesnap-setup runs non-interactively (over SSH, or
from the service user), so the profile edit does nothing for the running
process: a plain shutil.which("uv") straight after the install misses a uv that
was just installed successfully, and setup aborts on every fresh board with
"uv installation failed" despite the installer reporting "everything's
installed!". Observed 2026-09-20 deploying to a live Pi.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import scripts.setup as setup


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """An empty HOME whose ~/.local/bin is deliberately absent from PATH."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return tmp_path


def _install_uv_at(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    uv = directory / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)


def test_existing_uv_is_used_without_installing(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda name: "/usr/local/bin/uv")

    def fail(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("should not install when uv is already present")

    monkeypatch.setattr(setup, "run", fail)

    assert setup.ensure_uv() == "/usr/local/bin/uv"


def test_finds_uv_installed_outside_path(fake_home, monkeypatch):
    """The whole point: installed-but-not-on-PATH must resolve, not raise."""
    install_dir = fake_home / ".local" / "bin"

    def fake_run(cmd, *, check=True, env=None):
        _install_uv_at(install_dir)

    monkeypatch.setattr(setup, "run", fake_run)

    assert setup.ensure_uv() == str(install_dir / "uv")
    assert str(install_dir) in os.environ["PATH"].split(os.pathsep)


def test_install_dir_is_pinned_for_the_installer(fake_home, monkeypatch):
    """Pass UV_INSTALL_DIR so the lookup afterwards knows where to look."""
    install_dir = fake_home / ".local" / "bin"
    seen: dict[str, str] = {}

    def fake_run(cmd, *, check=True, env=None):
        seen.update(env or {})
        _install_uv_at(install_dir)

    monkeypatch.setattr(setup, "run", fake_run)
    setup.ensure_uv()

    assert seen["UV_INSTALL_DIR"] == str(install_dir)


def test_path_is_not_duplicated_when_already_present(fake_home, monkeypatch):
    install_dir = fake_home / ".local" / "bin"
    monkeypatch.setenv("PATH", f"{install_dir}{os.pathsep}/usr/bin")

    def fake_run(cmd, *, check=True, env=None):
        _install_uv_at(install_dir)

    monkeypatch.setattr(setup, "run", fake_run)
    setup.ensure_uv()

    assert os.environ["PATH"].split(os.pathsep).count(str(install_dir)) == 1


def test_still_raises_when_uv_is_genuinely_absent(fake_home, monkeypatch):
    """A real install failure must stay an error, not be masked by the retry."""

    def fake_run(cmd, *, check=True, env=None):
        pass  # installer "succeeds" but writes nothing

    monkeypatch.setattr(setup, "run", fake_run)

    with pytest.raises(RuntimeError, match="uv installation failed"):
        setup.ensure_uv()
