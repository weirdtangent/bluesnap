"""
Bluetooth controller responsible for keeping a configured speaker paired,
trusted, and connected. The controller polls every 10 seconds (configurable)
to ensure the link remains healthy and reconnects automatically when the
speaker comes back online.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import BluetoothConfig, BluetoothSpeakerConfig
from .utils import resolve_controller_identifier

LOG = logging.getLogger(__name__)


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
        self._controller_id = resolve_controller_identifier(config.adapter)

        self._running = False
        self._tasks: set[asyncio.Task[None]] = set()
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
                    await self._run_btctl(
                        ["select", self._config.adapter],
                        ["info", self._speaker.mac],
                    )
                    LOG.debug("sent keepalive to '%s'", self._speaker.name)
                except BluetoothCommandError as exc:
                    LOG.debug("keepalive failed for '%s': %s", self._speaker.name, exc)
                self._last_keepalive = now
            await asyncio.sleep(1)

    async def _prepare_adapter(self) -> None:
        """Select the adapter and ensure it is powered on and discoverable."""
        await self._run_btctl(
            ["select", self._controller_id],
            ["power", "on"],
            ["pairable", "on"],
            ["agent", "on"],
            ["default-agent"],
        )

    async def _trust_device(self, mac: str) -> None:
        """Mark the speaker as trusted so the OS reconnects automatically."""
        await self._run_btctl(
            ["select", self._controller_id],
            ["trust", mac],
        )

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
        await self._run_btctl(
            ["select", self._controller_id],
            ["connect", self._speaker.mac],
        )
        self._connected = True
        if self._callbacks.on_connected:
            await self._callbacks.on_connected(self._speaker)

    async def _is_connected(self) -> bool:
        """Return True when bluetoothctl reports the device is connected."""
        output = await self._run_btctl(
            ["select", self._controller_id],
            ["info", self._speaker.mac],
        )
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

    async def _run_btctl(self, *command_groups: list[str], timeout: int = 30) -> str:
        """
        Run bluetoothctl with provided commands and return stdout.

        Each element in ``command_groups`` represents a command followed by
        its arguments, e.g. ``["connect", "AA:BB:CC:DD:EE:FF"]``.
        """

        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin
        for group in command_groups:
            cmd = " ".join(group)
            LOG.debug("btctl <<< %s", cmd)
            proc.stdin.write(cmd.encode("utf-8") + b"\n")
        proc.stdin.write(b"quit\n")
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            raise BluetoothCommandError(stderr.decode().strip())
        return stdout.decode()


__all__ = ["BluetoothController", "ControllerCallbacks", "BluetoothCommandError"]
