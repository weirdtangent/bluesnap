"""
Snapcast client manager that supervises a snapclient subprocess and exposes
basic controls/telemetry for the rest of the Bluesnap bridge.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime

from snapcast.control.server import Snapserver

from .config import IdentityConfig, SnapcastConfig

LOG = logging.getLogger(__name__)


class SnapclientNotFoundError(FileNotFoundError):
    """Raised when the snapclient binary is missing."""


@dataclass(slots=True)
class SnapcastStatus:
    connected: bool = False
    last_start: datetime | None = None
    last_exit: datetime | None = None
    restart_count: int = 0
    last_returncode: int | None = None


class SnapcastManager:
    """
    Supervise a snapclient process pointing to the configured Snapserver.

    If the process exits unexpectedly it is restarted with a short backoff.
    """

    def __init__(
        self,
        config: SnapcastConfig,
        identity: IdentityConfig,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._config = config
        self._identity = identity
        self._loop = loop or asyncio.get_event_loop()
        self._process: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._running = False
        self._status = SnapcastStatus()
        self._snapclient_path = shutil.which("snapclient")
        if not self._snapclient_path:
            raise SnapclientNotFoundError("snapclient binary not found in PATH")
        self._control: Snapserver | None = None
        self._control_client_id: str | None = None

    @property
    def status(self) -> SnapcastStatus:
        return self._status

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._ensure_process()
        await self._ensure_control_client()
        self._monitor_task = self._loop.create_task(self._monitor_loop(), name="snapclient-monitor")

    async def stop(self) -> None:
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task
            self._monitor_task = None
        if self._process and self._process.returncode is None:
            LOG.info("terminating snapclient (pid %s)", self._process.pid)
            self._process.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._process.wait(), timeout=10)
        self._process = None
        if self._control:
            self._control.stop()
            self._control = None
        self._control_client_id = None

    async def set_volume(self, value: int) -> None:
        """
        Adjust snapclient volume (0-100) by calling snapctl.
        """

        value = max(0, min(100, value))
        if not await self._ensure_control_client():
            LOG.warning("snapserver control unavailable; cannot set volume")
            return
        assert self._control_client_id
        volume = {"percent": value, "muted": False}
        try:
            await self._control.client_volume(self._control_client_id, volume)
            LOG.info("set snapclient volume to %s", value)
        except (OSError, RuntimeError) as exc:
            LOG.error("failed to set snapclient volume via RPC: %s", exc)

    async def mute(self, state: bool) -> None:
        if not await self._ensure_control_client():
            LOG.warning("snapserver control unavailable; cannot toggle mute")
            return
        assert self._control_client_id
        volume = {"percent": self._control.client(self._control_client_id).volume, "muted": state}
        try:
            await self._control.client_volume(self._control_client_id, volume)
            LOG.info("set snapclient mute=%s", state)
        except (OSError, RuntimeError) as exc:
            LOG.error("failed to set snapclient mute via RPC: %s", exc)

    async def _monitor_loop(self) -> None:
        while self._running:
            if not self._process:
                await asyncio.sleep(1)
                continue
            returncode = await self._process.wait()
            self._status.connected = False
            self._status.last_exit = datetime.utcnow()
            self._status.last_returncode = returncode
            LOG.warning("snapclient exited with code %s", returncode)
            if not self._running:
                break
            await asyncio.sleep(5)
            await self._ensure_process()

    async def _ensure_process(self) -> None:
        if self._process and self._process.returncode is None:
            return
        command = self._build_command()
        LOG.info("starting snapclient: %s", " ".join(command))
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._status.connected = True
        self._status.last_start = datetime.utcnow()
        self._status.restart_count += 1

    def _build_command(self) -> list[str]:
        config = self._config
        soundcard = config.audio_device or self._default_soundcard(config.audio_backend)
        command: list[str] = [
            self._snapclient_path,
            "--host",
            config.server_host,
            "--port",
            str(config.server_port),
            "--latency",
            str(config.latency),
            "--buffer",
            str(config.buffer_ms),
            "--name",
            self._config.resolved_client_name(self._identity),
        ]
        if soundcard:
            command += ["--soundcard", soundcard]
        if config.server_stream:
            command += ["--stream", config.server_stream]
        return command

    def _default_soundcard(self, backend: str) -> str:
        if backend == "bluealsa":
            return "bluealsa"
        return backend

    async def _ensure_control_client(self) -> bool:
        if self._control is None:
            try:
                self._control = Snapserver(
                    self._loop,
                    self._config.server_host,
                    port=self._config.control_port,
                )
                await self._control.start()
                LOG.info(
                    "connected to snapserver control at %s:%s",
                    self._config.server_host,
                    self._config.control_port,
                )
            except OSError as exc:
                LOG.error("failed to connect to snapserver control: %s", exc)
                self._control = None
                return False
        if not self._control_client_id:
            resolved_name = self._config.resolved_client_name(self._identity)
            for client in self._control.clients:
                if client.friendly_name == resolved_name or client.identifier == resolved_name:
                    self._control_client_id = client.identifier
                    LOG.info("resolved snapclient id %s for '%s'", client.identifier, resolved_name)
                    break
            if not self._control_client_id:
                LOG.warning(
                    "snapclient named '%s' not found on control interface; available: %s",
                    resolved_name,
                    [client.friendly_name for client in self._control.clients],
                )
                return False
        return True


__all__ = ["SnapcastManager", "SnapcastStatus", "SnapclientNotFoundError"]
