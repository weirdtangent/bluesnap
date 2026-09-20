"""
Regression tests for setup's install-only-when-changed helper.

``bluesnap-setup`` is expected to run after every ``git pull``. The watchdog
follow-up actions it gates on this helper -- restarting watchdog.service, and
re-execing PID 1 to release /dev/watchdog -- are disruptive, and the watchdog is
the board's only in-band recovery from a silent network wedge. Bouncing it on
every routine upgrade would take the recovery mechanism offline precisely as
often as we touch the box, so "contents are identical" must mean "do nothing".

The helper shells out to ``sudo cmp`` and ``sudo install``, so these tests put a
stand-in ``sudo`` at the front of PATH. It runs ``cmp`` for real (that comparison
is the logic under test) but implements ``install -D -m`` itself, because BSD
install -- what a developer running the suite on macOS gets -- has no ``-D``.
The target is Raspberry Pi OS, where GNU install does.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from scripts.setup import _install_if_changed

# Exit code the fake sudo should return for `cmp`, when overriding it.
CMP_RC_ENV = "BLUESNAP_TEST_CMP_RC"


@pytest.fixture
def fake_sudo(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            """
            import os
            import shutil
            import subprocess
            import sys

            argv = sys.argv[1:]
            tool = argv[0]

            if tool == "cmp":
                override = os.environ.get("BLUESNAP_TEST_CMP_RC")
                if override is not None:
                    sys.exit(int(override))
                sys.exit(subprocess.run(argv, check=False).returncode)

            if tool == "install":
                # Portable stand-in for `install -D -m MODE SRC DST`.
                mode = argv[argv.index("-m") + 1]
                src, dst = argv[-2], argv[-1]
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
                os.chmod(dst, int(mode, 8))
                sys.exit(0)

            sys.exit(subprocess.run(argv, check=False).returncode)
            """
        )
    )
    sudo.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return sudo


def test_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        _install_if_changed(tmp_path / "absent.conf", tmp_path / "target.conf", "0644")


def test_installs_when_target_absent(tmp_path, fake_sudo):
    source = tmp_path / "source.conf"
    source.write_text("interval = 10\n")
    target = tmp_path / "nested" / "target.conf"

    assert _install_if_changed(source, target, "0644") is True
    assert target.read_text() == "interval = 10\n"


def test_noop_when_contents_match(tmp_path, fake_sudo):
    source = tmp_path / "source.conf"
    source.write_text("interval = 10\n")
    target = tmp_path / "target.conf"
    target.write_text("interval = 10\n")
    before = target.stat().st_mtime_ns

    assert _install_if_changed(source, target, "0644") is False
    assert target.stat().st_mtime_ns == before


def test_reinstalls_when_contents_differ(tmp_path, fake_sudo):
    source = tmp_path / "source.conf"
    source.write_text("interval = 10\n")
    target = tmp_path / "target.conf"
    target.write_text("interval = 60\n")

    assert _install_if_changed(source, target, "0644") is True
    assert target.read_text() == "interval = 10\n"


def test_applies_requested_mode(tmp_path, fake_sudo):
    source = tmp_path / "check.sh"
    source.write_text("#!/bin/sh\nexit 0\n")
    target = tmp_path / "installed.sh"

    assert _install_if_changed(source, target, "0755") is True
    assert target.stat().st_mode & 0o777 == 0o755


def test_cmp_failure_is_not_treated_as_match(tmp_path, fake_sudo, monkeypatch):
    """A cmp that fails for any reason other than "differs" must still install.

    cmp exits non-zero both when files differ and when it cannot read them at
    all. Only exit 0 -- a positive "these are identical" -- may skip the write;
    anything else has to fall through to install, or a broken cmp would silently
    leave a stale watchdog config in place.
    """
    monkeypatch.setenv(CMP_RC_ENV, "2")
    source = tmp_path / "source.conf"
    source.write_text("interval = 10\n")
    target = tmp_path / "target.conf"
    target.write_text("interval = 10\n")  # identical, yet cmp cannot say so

    assert _install_if_changed(source, target, "0644") is True
    assert target.read_text() == "interval = 10\n"
