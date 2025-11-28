"""
MQTT v5 bridge responsible for announcing Home Assistant discovery payloads,
publishing telemetry, and listening for control commands (volume, reconnect).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import paho.mqtt.client as mqtt

from .bluetooth_controller import BluetoothController
from .config import BluesnapConfig
from .snapcast_bridge import SnapcastManager

LOG = logging.getLogger(__name__)


class MQTTBridgeError(RuntimeError):
    """Raised when the bridge encounters repeated MQTT errors."""


ControlHandler = Callable[[dict[str, Any]], asyncio.Future | asyncio.Task | None]


@dataclass
class MQTTTopics:
    discovery_prefix: str
    availability: str
    telemetry: str
    commands_volume: str
    commands_reconnect: str


@dataclass
class MQTTBridge:
    config: BluesnapConfig
    bluetooth: BluetoothController
    snapcast: SnapcastManager
    loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.get_event_loop)

    def __post_init__(self) -> None:
        self._client = mqtt.Client(
            client_id=self.config.mqtt.resolved_client_id(self.config.identity),
            protocol=mqtt.MQTTv5,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        self._client.enable_logger(LOG)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.username_pw_set(self.config.mqtt.username, self.config.mqtt.password)
        if self.config.mqtt.tls.enabled:
            self._client.tls_set(
                ca_certs=str(self.config.mqtt.tls.ca_cert),
                certfile=str(self.config.mqtt.tls.client_cert),
                keyfile=str(self.config.mqtt.tls.client_key),
            )

        topics = self.config.effective_topics()
        self._topics = MQTTTopics(
            discovery_prefix=self.config.mqtt.discovery_prefix.rstrip("/"),
            availability=f"{topics['base']}/status",
            telemetry=f"{topics['base']}/telemetry",
            commands_volume=f"{topics['base']}/command/volume",
            commands_reconnect=f"{topics['base']}/command/reconnect",
        )
        self._connected_event = asyncio.Event()

    async def start(self) -> None:
        LOG.info("connecting to MQTT broker %s:%s", self.config.mqtt.host, self.config.mqtt.port)
        self._client.connect(
            self.config.mqtt.host,
            self.config.mqtt.port,
            keepalive=self.config.mqtt.keepalive,
        )
        self._client.loop_start()
        await self._connected_event.wait()
        await self._publish_discovery()
        await self._publish_availability("online")

    async def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict[str, Any],
        reason_code: int,
        properties: mqtt.Properties | None,
    ) -> None:  # noqa: D401,E501
        if reason_code != 0:
            LOG.error("mqtt connection failed: %s", mqtt.error_string(reason_code))
            return
        LOG.info("connected to mqtt broker")
        client.subscribe(
            [
                (self._topics.commands_volume, 1),
                (self._topics.commands_reconnect, 1),
            ]
        )
        self.loop.call_soon_threadsafe(self._connected_event.set)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict[str, Any],
        reason_code: int,
        properties: mqtt.Properties | None,
    ) -> None:  # noqa: D401,E501
        LOG.warning("mqtt disconnected: %s", mqtt.error_string(reason_code))

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        payload = msg.payload.decode("utf-8")
        LOG.debug("mqtt message %s => %s", msg.topic, payload)
        asyncio.run_coroutine_threadsafe(self._handle_command(msg.topic, payload), self.loop)

    async def _handle_command(self, topic: str, payload: str) -> None:
        if topic == self._topics.commands_volume:
            try:
                volume = int(payload)
            except ValueError:
                LOG.warning("invalid volume payload %s", payload)
                return
            await self.snapcast.set_volume(volume)
        elif topic == self._topics.commands_reconnect:
            await self.bluetooth.stop()
            await self.bluetooth.start()
        else:
            LOG.debug("no handler for topic %s", topic)

    async def publish_telemetry(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data)
        LOG.debug("publishing telemetry: %s", payload)
        result = self._client.publish(self._topics.telemetry, payload, qos=1, retain=False)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            LOG.warning("telemetry publish failed: %s", result.rc)

    async def _publish_discovery(self) -> None:
        device_info = self._device_payload()
        friendly = self.config.identity.friendly_name
        entities: list[tuple[str, str, dict[str, Any]]] = []

        def sensor(
            object_id: str,
            name: str,
            template: str,
            *,
            extra: dict[str, Any] | None = None,
        ) -> None:
            payload: dict[str, Any] = {
                "name": f"{friendly} {name}",
                "state_topic": self._topics.telemetry,
                "value_template": template,
                "device": device_info,
                "availability": [{"topic": self._topics.availability}],
                "entity_category": "diagnostic",
            }
            if extra:
                payload.update(extra)
            entities.append(("sensor", object_id, payload))

        sensor(
            "status",
            "Status",
            '{{ "online" if value_json.snapcast.connected else "offline" }}',
        )
        sensor(
            "snapcast_volume",
            "Snapcast Volume",
            "{{ value_json.snapcast.volume }}",
            extra={
                "unit_of_measurement": "%",
                "state_class": "measurement",
            },
        )
        sensor(
            "snapcast_restarts",
            "Snapcast Restart Count",
            "{{ value_json.snapcast.restart_count }}",
            extra={"state_class": "total_increasing"},
        )

        if "bluetooth" in self.config.telemetry.metrics:
            sensor(
                "bluetooth_connected",
                "Bluetooth Connected",
                '{{ "connected" if value_json.bluetooth.connected else "disconnected" }}',
            )
            sensor(
                "bluetooth_speaker",
                "Bluetooth Speaker",
                "{{ value_json.bluetooth.speaker }}",
            )
            sensor(
                "bluetooth_mac",
                "Bluetooth MAC",
                "{{ value_json.bluetooth.mac }}",
            )

        metrics = set(self.config.telemetry.metrics)
        if "cpu" in metrics:
            sensor(
                "cpu_percent",
                "CPU Percent",
                "{{ value_json.cpu_percent }}",
                extra={
                    "unit_of_measurement": "%",
                    "state_class": "measurement",
                },
            )
        if "memory" in metrics:
            sensor(
                "memory_percent",
                "Memory Percent",
                "{{ value_json.memory_percent }}",
                extra={
                    "unit_of_measurement": "%",
                    "state_class": "measurement",
                },
            )
        if "load" in metrics:
            sensor(
                "load_1m",
                "Load 1m",
                "{{ value_json.load_1m | round(3) }}",
                extra={"state_class": "measurement"},
            )
            sensor(
                "load_5m",
                "Load 5m",
                "{{ value_json.load_5m | round(3) }}",
                extra={"state_class": "measurement"},
            )
            sensor(
                "load_15m",
                "Load 15m",
                "{{ value_json.load_15m | round(3) }}",
                extra={"state_class": "measurement"},
            )
        if "temperature" in metrics:
            sensor(
                "temperature_c",
                "Temperature",
                "{{ value_json.temperature_c }}",
                extra={
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "state_class": "measurement",
                },
            )

        entities.append(
            (
                "number",
                "volume",
                {
                    "name": f"{friendly} Volume",
                    "command_topic": self._topics.commands_volume,
                    "state_topic": self._topics.telemetry,
                    "value_template": "{{ value_json.snapcast.volume }}",
                    "min": 0,
                    "max": 100,
                    "step": 1,
                    "device": device_info,
                    "availability": [{"topic": self._topics.availability}],
                },
            )
        )
        for component, object_id, payload in entities:
            unique = f"{self.config.identity.instance_name}_{object_id}"
            payload["unique_id"] = unique
            topic = f"{self._topics.discovery_prefix}/{component}/{unique}/config"
            self._client.publish(topic, json.dumps(payload), retain=True, qos=1)

    def _device_payload(self) -> dict[str, Any]:
        return {
            "identifiers": [self.config.identity.instance_name],
            "name": self.config.identity.friendly_name,
            "manufacturer": "Bluesnap",
            "model": "Bluetooth Snapcast Bridge",
        }

    async def _publish_availability(self, state: str) -> None:
        self._client.publish(self._topics.availability, state, retain=True, qos=1)
