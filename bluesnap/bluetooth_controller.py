"""
Bluetooth controller responsible for keeping a configured speaker paired,
trusted, and connected. The controller polls every 10 seconds (configurable)
to ensure the link remains healthy and reconnects automatically when the
speaker comes back online.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import BluetoothConfig, BluetoothSpeakerConfig

LOG = logging.getLogger(__name__)

# bluetoothctl (BlueZ 5.66) allocates ~850 MB of anonymous heap when it is driven
# as an interactive shell -- commands fed to its stdin -- and essentially nothing
# when the same command is passed as argv. Measured on one host, one adapter, with
# the service stopped so the sampler could not latch onto its children:
#
#     bluetoothctl info <mac>                      0 MB   (argv, stdout /dev/null)
#     bluetoothctl info <mac> | cat                0 MB   (argv, stdout a pipe)
#     printf 'info <mac>\nquit\n' | bluetoothctl  845 MB   (interactive)
#
# The stdout target is irrelevant; only the invocation style matters, and the
# balloon happens while producing ~64 bytes of output. So every command below is
# issued in argv form, one process per command.
#
# `select` is not used: these hosts have a single controller and BlueZ acts on the
# default one. `agent on` / `default-agent` are likewise dropped -- an agent only
# lives for the duration of the shell session, so registering one in a one-shot
# process achieves nothing. A paired, trusted speaker reconnects without it.
#
# The cap, timeout, capture-size guard and unconditional reap all remain. They
# cover a *wedged* child, which is a separate failure from the balloon.
_MAX_OUTPUT_BYTES = 1 << 20  # 1 MiB -- most we hand back to a caller
_MAX_CAPTURE_BYTES = 32 << 20  # 32 MiB -- hard stop for a spewing child
_CAPTURE_POLL_SECONDS = 0.25
_DEFAULT_BTCTL_TIMEOUT = 30


class BluetoothCommandError(RuntimeError):
    """Raised when a bluetoothctl command fails."""


@dataclass(slots=True)
class ControllerCallbacks:
    """Optional hooks for other components to receive state updates."""

    on_connected: Callable[[BluetoothSpeakerConfig], Awaitable[None]] | None = None
    on_disconnected: Callable[[BluetoothSpeakerConfig], Awaitable[None]] | None = None


class BluetoothController:
    """
    Manage a bluetooth speaker connection using bluetoothctl commands.

    The controller attempts to keep the configured speaker connected, retrying
    every ``reconnect_interval`` seconds when it is unavailable. A keepalive
    loop periodically pings the device so that idle speakers do not go to sleep.
    """

    def __init__(
        self,
        config: BluetoothConfig,
        callbacks: ControllerCallbacks | None = None,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._config = config
        self._speaker = config.speaker
        self._callbacks = callbacks or ControllerCallbacks()
        self._loop = loop or asyncio.get_event_loop()

        self._running = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._btctl_lock = asyncio.Lock()
        self._last_keepalive = datetime.min
        self._last_connect_attempt = datetime.min
        self._connected = False

    async def start(self) -> None:
        """Power on the adapter, trust the device, and begin watchdog loops."""
        if self._running:
            return
        self._running = True
        LOG.info("starting bluetooth controller for '%s'", self._speaker.name)
        await self._prepare_adapter()
        await self._trust_device(self._speaker.mac)
        await self._connect_if_needed()
        self._spawn(self._watchdog_loop(), "bt-watchdog")
        self._spawn(self._keepalive_loop(), "bt-keepalive")

    async def stop(self) -> None:
        """Cancel background tasks and stop monitoring."""
        if not self._running:
            return
        LOG.info("stopping bluetooth controller for '%s'", self._speaker.name)
        self._running = False
        for task in list(self._tasks):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    @property
    def active_speaker(self) -> BluetoothSpeakerConfig:
        return self._speaker

    @property
    def connected(self) -> bool:
        return self._connected

    async def _watchdog_loop(self) -> None:
        """Check connection status and reconnect when necessary."""
        interval = max(5, self._config.reconnect_interval)
        while self._running:
            try:
                await self._connect_if_needed()
            except (BluetoothCommandError, TimeoutError, OSError) as exc:
                LOG.warning("bluetooth watchdog loop error: %s", exc)
            await asyncio.sleep(interval)

    async def _keepalive_loop(self) -> None:
        """Issue a harmless command periodically so the speaker stays awake."""
        interval = max(5, self._speaker.keepalive_interval)
        while self._running:
            now = datetime.utcnow()
            if now - self._last_keepalive >= timedelta(seconds=interval):
                try:
                    await self._run_btctl(["info", self._speaker.mac])
                    LOG.debug("sent keepalive to '%s'", self._speaker.name)
                except BluetoothCommandError as exc:
                    LOG.debug("keepalive failed for '%s': %s", self._speaker.name, exc)
                self._last_keepalive = now
            await asyncio.sleep(1)

    async def _prepare_adapter(self) -> None:
        """Select the adapter and ensure it is powered on and discoverable."""
        await self._run_btctl(
            ["power", "on"],
            ["pairable", "on"],
        )

    async def _trust_device(self, mac: str) -> None:
        """Mark the speaker as trusted so the OS reconnects automatically."""
        await self._run_btctl(["trust", mac])

    async def _connect_if_needed(self) -> None:
        """Connect the speaker when disconnected or when we have not tried recently."""
        connected = await self._is_connected()
        if connected:
            return
        now = datetime.utcnow()
        if now - self._last_connect_attempt < timedelta(seconds=self._config.reconnect_interval):
            return
        self._last_connect_attempt = now
        LOG.info("connecting bluetooth speaker '%s'", self._speaker.name)
        await self._run_btctl(["connect", self._speaker.mac])
        self._connected = True
        if self._callbacks.on_connected:
            await self._callbacks.on_connected(self._speaker)

    async def _is_connected(self) -> bool:
        """Return True when bluetoothctl reports the device is connected."""
        output = await self._run_btctl(["info", self._speaker.mac])
        for line in output.splitlines():
            if line.strip().lower().startswith("connected:"):
                result = line.strip().split(":")[1].strip().lower() == "yes"
                self._connected = result
                return result
        if self._callbacks.on_disconnected:
            await self._callbacks.on_disconnected(self._speaker)
        self._connected = False
        return False

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        task = self._loop.create_task(coro, name=name)
        self._tasks.add(task)

        def _cleanup(task: asyncio.Task[None]) -> None:
            self._tasks.discard(task)
            with suppress(asyncio.CancelledError):
                task.result()

        task.add_done_callback(_cleanup)

    async def _run_btctl(
        self,
        *command_groups: list[str],
        timeout: int = _DEFAULT_BTCTL_TIMEOUT,
    ) -> str:
        """
        Run each command through ``bluetoothctl`` in argv form, returning the
        concatenated stdout.

        Each element of ``command_groups`` is one command plus its arguments,
        e.g. ``["connect", "AA:BB:CC:DD:EE:FF"]``, and becomes its own process --
        see the note at the top of this module for why they are not fed to a
        single interactive shell.

        Invocations are serialized: the watchdog and keepalive loops both call
        this, and concurrent bluetoothctl processes fight over the adapter.
        """

        async with self._btctl_lock:
            chunks = []
            for group in command_groups:
                chunks.append(await self._run_btctl_once(group, timeout))
            return "".join(chunks)

    async def _run_btctl_once(self, command: list[str], timeout: int) -> str:
        LOG.debug("btctl <<< %s", " ".join(command))
        with (
            tempfile.TemporaryFile() as out_f,
            tempfile.TemporaryFile() as err_f,
        ):
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl",
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
            )
            try:
                try:
                    await asyncio.wait_for(
                        self._await_btctl(proc, (out_f, err_f)),
                        timeout=timeout,
                    )
                except TimeoutError:
                    raise BluetoothCommandError(
                        f"bluetoothctl timed out after {timeout}s"
                    ) from None
                stdout, out_truncated = _read_capped(out_f, _MAX_OUTPUT_BYTES)
                stderr, err_truncated = _read_capped(err_f, _MAX_OUTPUT_BYTES)
                # Either stream overrunning means the child misbehaved. Checking
                # only stdout would let a stderr overrun exit "successfully", and
                # would hand back a silently truncated message on failure.
                if out_truncated or err_truncated:
                    raise BluetoothCommandError("bluetoothctl produced runaway output")
                if proc.returncode != 0:
                    raise BluetoothCommandError(stderr.decode(errors="replace").strip())
                return stdout.decode(errors="replace")
            finally:
                # wait_for() cancels the *coroutine*, it does not kill the child.
                # Without this, every timeout orphans a process.
                await self._reap(proc)

    async def _await_btctl(self, proc: asyncio.subprocess.Process, capture_files) -> None:
        """Wait for the child, cutting it off if it writes absurd amounts."""
        waiter = asyncio.ensure_future(proc.wait())
        try:
            while True:
                done, _ = await asyncio.wait({waiter}, timeout=_CAPTURE_POLL_SECONDS)
                if waiter in done:
                    return
                captured = sum(os.fstat(f.fileno()).st_size for f in capture_files)
                if captured > _MAX_CAPTURE_BYTES:
                    raise BluetoothCommandError("bluetoothctl produced runaway output")
        finally:
            if not waiter.done():
                waiter.cancel()
                with suppress(asyncio.CancelledError):
                    await waiter

    @staticmethod
    async def _reap(proc: asyncio.subprocess.Process) -> None:
        """Make sure the child is dead and its exit status collected."""
        if proc.returncode is not None:
            return
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(asyncio.CancelledError):
            await proc.wait()
        LOG.warning("killed unresponsive bluetoothctl (pid %s)", proc.pid)


def _read_capped(handle, limit: int) -> tuple[bytes, bool]:
    """
    Read a captured-output file, keeping at most ``limit`` bytes.

    Returns ``(data, truncated)``. Truncation means the child produced more than
    we are willing to hand back -- the cap is exceeded only by going over it, so
    output of exactly ``limit`` bytes is not reported as truncated.
    """
    handle.seek(0)
    data = handle.read(limit + 1)
    if len(data) > limit:
        LOG.warning("bluetoothctl output exceeded %d bytes; truncating", limit)
        return data[:limit], True
    return data, False


__all__ = ["BluetoothController", "ControllerCallbacks", "BluetoothCommandError"]
