"""
Periodic telemetry publisher that reports system health and bridge status via MQTT.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any

import psutil

from .bluetooth_controller import BluetoothController
from .config import BluesnapConfig
from .snapcast_bridge import SnapcastManager

if TYPE_CHECKING:  # pragma: no cover
    from .mqtt_bridge import MQTTBridge

LOG = logging.getLogger(__name__)


class TelemetryPublisher:
    """Background task that periodically publishes telemetry payloads."""

    def __init__(
        self,
        config: BluesnapConfig,
        bluetooth: BluetoothController,
        snapcast: SnapcastManager,
        mqtt: MQTTBridge,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._config = config
        self._bluetooth = bluetooth
        self._snapcast = snapcast
        self._mqtt = mqtt
        self._loop = loop or asyncio.get_event_loop()
        self._task: asyncio.Task[None] | None = None
        self._interval = max(5, config.telemetry.interval)
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = self._loop.create_task(self._run(), name="telemetry-loop")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                payload = await self._build_payload()
                await self._mqtt.publish_telemetry(payload)
            except asyncio.CancelledError:  # pragma: no cover - cooperative shutdown
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.warning("telemetry loop error: %s", exc)
            await asyncio.sleep(self._interval)

    async def _build_payload(self) -> dict[str, Any]:
        metrics = set(self._config.telemetry.metrics)
        payload: dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(),
            "identity": {
                "instance_name": self._config.identity.instance_name,
                "friendly_name": self._config.identity.friendly_name,
            },
            "snapcast": {
                "connected": self._snapcast.status.connected,
                "restart_count": self._snapcast.status.restart_count,
            },
        }

        volume = await self._snapcast.current_volume()
        if volume is not None:
            payload["snapcast"]["volume"] = volume

        if "bluetooth" in metrics:
            speaker = self._bluetooth.active_speaker
            payload["bluetooth"] = {
                "connected": self._bluetooth.connected,
                "speaker": speaker.name,
                "mac": speaker.mac,
            }

        if "cpu" in metrics:
            payload["cpu_percent"] = psutil.cpu_percent(interval=None)

        if "memory" in metrics:
            payload["memory_percent"] = psutil.virtual_memory().percent

        if "load" in metrics:
            try:
                load_1m, load_5m, load_15m = os.getloadavg()
                payload["load_1m"] = load_1m
                payload["load_5m"] = load_5m
                payload["load_15m"] = load_15m
            except OSError:
                pass

        if "temperature" in metrics:
            temperature = self._read_temperature()
            if temperature is not None:
                payload["temperature_c"] = temperature

        return payload

    @staticmethod
    def _read_temperature() -> float | None:
        try:
            temps = psutil.sensors_temperatures()
        except (RuntimeError, AttributeError, PermissionError):
            return None
        for readings in temps.values():
            if readings:
                return readings[0].current
        return None


__all__ = ["TelemetryPublisher"]
