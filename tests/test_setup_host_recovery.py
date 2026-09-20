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


def test_powersave_survives_unusable_iw(tmp_path, monkeypatch, recorder):
    """A host where `iw dev` fails still gets the durable drop-in, and continues.

    This step runs before the watchdog is installed, so an exception escaping
    here would cost the board both mechanisms rather than just the toggle.
    """
    commands, installed = recorder
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *a, **kw: setup.subprocess.CompletedProcess(
            a[0], 1, stdout="", stderr="sudo: iw: command not found"
        ),
    )

    setup.ensure_wifi_powersave_off(tmp_path)

    assert any("wifi-powersave-off.conf" in str(t) for _, t in installed)
    assert "set power_save off" not in _flat(commands)


def test_powersave_probes_iw_through_sudo(tmp_path, monkeypatch, recorder):
    """`iw` lives in /usr/sbin, off an unprivileged PATH; probe it via sudo.

    Probing it any other way reports "not found" on a host where iw is installed
    and working, silently skipping the toggle -- observed on a live board that
    was left with power-save still enabled.
    """
    commands, _ = recorder
    probes: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        probes.append(cmd)
        return setup.subprocess.CompletedProcess(
            cmd, 0, stdout="phy#0\n\tInterface wlan0\n\t\ttype managed\n", stderr=""
        )

    monkeypatch.setattr(setup.subprocess, "run", fake_run)

    setup.ensure_wifi_powersave_off(tmp_path)

    assert ["sudo", "iw", "dev"] in probes
    assert "sudo iw dev wlan0 set power_save off" in _flat(commands)


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
    monkeypatch.setattr(setup, "_unit_is_active", lambda unit: True)

    setup.ensure_hardware_watchdog(tmp_path)

    flat = _flat(commands)
    assert any("disable-runtime-watchdog" in str(t) for _, t in installed)
    assert "daemon-reexec" in flat
    assert "systemctl enable --now watchdog" in flat
    # Already running, so no redundant start.
    assert "systemctl start watchdog" not in flat


def test_cancelled_restart_is_followed_by_an_explicit_start(tmp_path, monkeypatch, recorder):
    """Debian's unit makes `restart` leave the daemon stopped; start it anyway.

    watchdog.service carries ExecStopPost=... || false, which exits 1 by design
    when run_wd_keepalive=1 (the shipped default). systemd sees the stop half
    fail and cancels the queued start, so a config change silently disarms the
    board -- observed live, where it sat with no watchdog until started by hand.
    """
    commands, _ = recorder
    device = tmp_path / "watchdog"
    device.write_text("")
    monkeypatch.setattr(setup, "WATCHDOG_DEVICE", device)

    # Inactive after the restart, active once explicitly started.
    states = iter([False, True])
    monkeypatch.setattr(setup, "_unit_is_active", lambda unit: next(states))

    setup.ensure_hardware_watchdog(tmp_path)

    assert "sudo systemctl start watchdog" in _flat(commands)


def test_inactive_watchdog_is_reported_not_raised(tmp_path, monkeypatch, recorder, caplog):
    """A daemon that will not start must be loud, but must not abort setup.

    Raising here would skip install_systemd_unit() and leave the bridge itself
    un-deployed -- trading a missing safety net for a broken speaker.
    """
    device = tmp_path / "watchdog"
    device.write_text("")
    monkeypatch.setattr(setup, "WATCHDOG_DEVICE", device)
    monkeypatch.setattr(setup, "_unit_is_active", lambda unit: False)

    with caplog.at_level("ERROR"):
        setup.ensure_hardware_watchdog(tmp_path)

    assert "NO hardware watchdog" in caplog.text


def test_unit_is_active_reads_systemctl(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return setup.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(setup.subprocess, "run", fake_run)

    assert setup._unit_is_active("watchdog") is True
    assert calls == [["systemctl", "is-active", "--quiet", "watchdog"]]
