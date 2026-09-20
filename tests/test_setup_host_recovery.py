"""
Regression tests for the two host-recovery setup steps.

Both guard the same hazard from opposite ends: ``bluesnap-setup`` must not leave
the board with *less* protection than it started with. The Wi-Fi step must not
abort setup before the watchdog step gets to run, and the watchdog step must not
take /dev/watchdog away from PID 1 unless a daemon can actually take it over.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.setup as setup


@pytest.fixture
def recorder(monkeypatch):
    """Record every command setup would run, and every file it would install."""
    commands: list[list[str]] = []
    installed: list[tuple[Path, Path]] = []

    def fake_run(cmd, *, check=True, env=None):
        commands.append(cmd)

    def fake_install(source: Path, target: Path, mode: str) -> bool:
        installed.append((source, target))
        return True  # "changed", so gated follow-ups are exercised

    monkeypatch.setattr(setup, "run", fake_run)
    monkeypatch.setattr(setup, "_install_if_changed", fake_install)
    return commands, installed


def _flat(commands: list[list[str]]) -> str:
    return " | ".join(" ".join(c) for c in commands)


def test_powersave_survives_missing_iw(tmp_path, monkeypatch, recorder):
    """A host without `iw` still gets the durable drop-in, and setup continues.

    subprocess raises FileNotFoundError when the binary is absent -- check=False
    only suppresses a non-zero exit -- and this step runs before the watchdog is
    installed, so an escape here would cost the board both mechanisms.
    """
    commands, installed = recorder
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)

    setup.ensure_wifi_powersave_off(tmp_path)

    assert any("wifi-powersave-off.conf" in str(t) for _, t in installed)
    assert "iw dev" not in _flat(commands)


def test_powersave_toggles_the_live_interface(tmp_path, monkeypatch, recorder):
    commands, _ = recorder
    monkeypatch.setattr(setup.shutil, "which", lambda name: f"/usr/sbin/{name}")
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *a, **kw: setup.subprocess.CompletedProcess(
            a[0], 0, stdout="phy#0\n\tInterface wlan0\n\t\ttype managed\n", stderr=""
        ),
    )

    setup.ensure_wifi_powersave_off(tmp_path)

    assert "iw dev wlan0 set power_save off" in _flat(commands)


def test_watchdog_handoff_skipped_without_device(tmp_path, monkeypatch, recorder):
    """No /dev/watchdog means no handoff: systemd's config must stay untouched.

    Installing the RuntimeWatchdogSec=0 drop-in here would disable whatever PID 1
    had while handing the device to a daemon that cannot start -- strictly worse
    than doing nothing.
    """
    commands, installed = recorder
    monkeypatch.setattr(setup, "WATCHDOG_DEVICE", tmp_path / "absent-watchdog")

    setup.ensure_hardware_watchdog(tmp_path)

    assert not any("disable-runtime-watchdog" in str(t) for _, t in installed)
    flat = _flat(commands)
    assert "daemon-reexec" not in flat
    assert "enable --now watchdog" not in flat
    # The config files themselves are still staged for a later, working run.
    assert any("bluesnap-net-check.sh" in str(t) for _, t in installed)
    assert any(str(t).endswith("watchdog.conf") for _, t in installed)


def test_watchdog_handoff_runs_when_device_present(tmp_path, monkeypatch, recorder):
    commands, installed = recorder
    device = tmp_path / "watchdog"
    device.write_text("")
    monkeypatch.setattr(setup, "WATCHDOG_DEVICE", device)
    monkeypatch.setattr(
        setup.subprocess, "run", lambda *a, **kw: setup.subprocess.CompletedProcess(a[0], 0)
    )

    setup.ensure_hardware_watchdog(tmp_path)

    flat = _flat(commands)
    assert any("disable-runtime-watchdog" in str(t) for _, t in installed)
    assert "daemon-reexec" in flat
    assert "systemctl enable --now watchdog" in flat


def test_inactive_watchdog_is_reported_not_raised(tmp_path, monkeypatch, recorder, caplog):
    """A daemon that fails to start must be loud, but must not abort setup.

    Raising here would skip install_systemd_unit() and leave the bridge itself
    un-deployed -- trading a missing safety net for a broken speaker.
    """
    device = tmp_path / "watchdog"
    device.write_text("")
    monkeypatch.setattr(setup, "WATCHDOG_DEVICE", device)
    monkeypatch.setattr(
        setup.subprocess, "run", lambda *a, **kw: setup.subprocess.CompletedProcess(a[0], 3)
    )

    with caplog.at_level("ERROR"):
        setup.ensure_hardware_watchdog(tmp_path)

    assert "NO hardware watchdog" in caplog.text
