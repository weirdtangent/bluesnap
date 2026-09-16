"""
Regression tests for the bluetoothctl subprocess runner.

The bug these cover: bluetoothctl attached to a pipe can spin and emit output
without ever reaching EOF. The original runner used
``asyncio.wait_for(proc.communicate(), ...)``, which buffers without bound and --
critically -- does not kill the child when it times out, so every stuck call
orphaned a process that kept allocating. On an affected device that reached
~900 MB RSS per orphan within seconds and eventually took the host off the
network entirely.
"""

from __future__ import annotations

import asyncio
import os
import stat
import textwrap

import pytest

from bluesnap.bluetooth_controller import (
    _MAX_OUTPUT_BYTES,
    BluetoothCommandError,
    BluetoothController,
)
from bluesnap.config import BluetoothConfig, BluetoothSpeakerConfig

MAC = "AA:BB:CC:DD:EE:FF"


def _fake_btctl(tmp_path, body: str):
    """Put a fake `bluetoothctl` at the front of PATH and return its dir."""
    script = tmp_path / "bluetoothctl"
    script.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(tmp_path)


def _controller(monkeypatch, path_dir: str) -> BluetoothController:
    monkeypatch.setenv("PATH", path_dir + os.pathsep + os.environ["PATH"])
    # Skip the hciconfig shell-out; irrelevant to the runner under test.
    monkeypatch.setattr(
        "bluesnap.bluetooth_controller.resolve_controller_identifier",
        lambda adapter: adapter,
    )
    config = BluetoothConfig(speaker=BluetoothSpeakerConfig(name="test", mac=MAC))
    return BluetoothController(config)


@pytest.mark.asyncio
async def test_runaway_btctl_is_killed_and_does_not_hang(tmp_path, monkeypatch):
    """A bluetoothctl that spews forever must time out, not wedge or balloon."""
    path_dir = _fake_btctl(
        tmp_path,
        """
        import sys
        while True:
            sys.stdout.write("[bluetooth]# " * 512)
            sys.stdout.flush()
        """,
    )
    controller = _controller(monkeypatch, path_dir)

    # Caught by the output cap in milliseconds -- it never reaches the timeout.
    with pytest.raises(BluetoothCommandError, match="runaway output"):
        await controller._run_btctl(["info", MAC], timeout=2)

    # The child must be gone: the original code left it running and spinning.
    await asyncio.sleep(0.2)
    assert not _surviving_fake_btctl(path_dir)


@pytest.mark.asyncio
async def test_btctl_output_is_capped(tmp_path, monkeypatch):
    """Excess output is cut off and reported, not buffered without limit."""
    path_dir = _fake_btctl(
        tmp_path,
        """
        import sys
        for _ in range(4096):
            sys.stdout.write("x" * 4096)
        sys.stdout.flush()
        """,
    )
    controller = _controller(monkeypatch, path_dir)
    with pytest.raises(BluetoothCommandError, match="runaway output"):
        await controller._run_btctl(["info", MAC], timeout=20)


@pytest.mark.asyncio
async def test_btctl_normal_output_is_returned(tmp_path, monkeypatch):
    """The happy path still returns stdout verbatim."""
    path_dir = _fake_btctl(
        tmp_path,
        """
        import sys
        sys.stdin.read()
        sys.stdout.write("Device AA:BB:CC:DD:EE:FF\\n\\tConnected: yes\\n")
        """,
    )
    controller = _controller(monkeypatch, path_dir)
    out = await controller._run_btctl(["info", MAC], timeout=20)
    assert "Connected: yes" in out


@pytest.mark.asyncio
async def test_silent_hang_hits_the_timeout_and_is_killed(tmp_path, monkeypatch):
    """A bluetoothctl that produces nothing and never exits must still be reaped."""
    path_dir = _fake_btctl(
        tmp_path,
        """
        import time
        time.sleep(3600)
        """,
    )
    controller = _controller(monkeypatch, path_dir)

    with pytest.raises(BluetoothCommandError, match="timed out"):
        await controller._run_btctl(["info", MAC], timeout=1)

    await asyncio.sleep(0.2)
    assert not _surviving_fake_btctl(path_dir)


@pytest.mark.asyncio
async def test_finite_child_that_overruns_the_cap_still_fails(tmp_path, monkeypatch):
    """
    Overrunning the cap must fail even when the child exits cleanly.

    Truncation is signalled explicitly rather than inferred from
    ``proc.returncode``: a finite child can overrun the cap and still exit 0,
    and the event loop may reap it before we look at the return code.
    """
    path_dir = _fake_btctl(
        tmp_path,
        """
        import sys
        sys.stdout.write("x" * (2 << 20))
        sys.stdout.flush()
        sys.exit(0)
        """,
    )
    controller = _controller(monkeypatch, path_dir)
    with pytest.raises(BluetoothCommandError, match="runaway output"):
        await controller._run_btctl(["info", MAC], timeout=20)


@pytest.mark.asyncio
async def test_output_of_exactly_the_cap_is_not_truncated(tmp_path, monkeypatch):
    """
    Exactly ``_MAX_OUTPUT_BYTES`` is legitimate output, not a runaway.

    The cap is exceeded only by going *over* it, so a stream that lands
    precisely on the limit has to reach EOF normally rather than being
    reported as truncated.
    """
    path_dir = _fake_btctl(
        tmp_path,
        f"""
        import sys
        sys.stdout.write("y" * {_MAX_OUTPUT_BYTES})
        sys.stdout.flush()
        """,
    )
    controller = _controller(monkeypatch, path_dir)
    out = await controller._run_btctl(["info", MAC], timeout=20)
    assert len(out) == _MAX_OUTPUT_BYTES


@pytest.mark.asyncio
async def test_btctl_calls_are_serialized(tmp_path, monkeypatch):
    """
    Overlapping invocations fight over the adapter, so the runner takes a lock.

    The watchdog and keepalive loops both call _run_btctl independently; on the
    live host that produced two concurrent bluetoothctl processes.
    """
    marker = tmp_path / "concurrent"
    path_dir = _fake_btctl(
        tmp_path,
        f"""
        import os, sys, time
        lock = {str(marker)!r}
        if os.path.exists(lock):
            sys.stderr.write("overlap\\n")
            sys.exit(1)
        open(lock, "w").close()
        time.sleep(0.3)
        os.remove(lock)
        sys.stdout.write("ok\\n")
        """,
    )
    controller = _controller(monkeypatch, path_dir)
    results = await asyncio.gather(
        controller._run_btctl(["info", MAC], timeout=20),
        controller._run_btctl(["info", MAC], timeout=20),
    )
    assert all("ok" in r for r in results)


def _surviving_fake_btctl(path_dir: str) -> bool:
    """
    True if this test's own fake bluetoothctl is still running.

    Scoped to the per-test tmp_path rather than matching every `bluetoothctl`
    on the machine, so a real one -- or an orphan leaked by an earlier run --
    cannot make the result lie in either direction.
    """
    import subprocess

    out = subprocess.run(
        ["pgrep", "-f", f"{path_dir}/bluetoothctl"],
        capture_output=True,
        text=True,
    )
    return bool(out.stdout.strip())
