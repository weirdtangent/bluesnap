"""
Configuration models and loader utilities for the Bluesnap bridge.

The configuration is expressed in YAML and deserialized into pydantic models so
that the rest of the bridge can rely on validated, typed settings. Defaults are
derived from the configured identity wherever possible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


class IdentityConfig(BaseModel):
    """Metadata describing this bridge instance."""

    instance_name: str = Field(
        ..., description="System-facing identifier, e.g. bluesnap-livingroom"
    )
    friendly_name: str = Field(
        ..., description="Human friendly name for logs/UI, e.g. Living Room Bridge"
    )
    unique_suffix: str = Field(
        ..., min_length=1, max_length=8, description="Short suffix used for topics/IDs"
    )


class MQTTTLSConfig(BaseModel):
    enabled: bool = False
    ca_cert: Path | None = None
    client_cert: Path | None = None
    client_key: Path | None = None

    @model_validator(mode="after")
    def validate_paths(self) -> MQTTTLSConfig:
        if self.enabled:
            missing = [
                name
                for name, value in (
                    ("ca_cert", self.ca_cert),
                    ("client_cert", self.client_cert),
                    ("client_key", self.client_key),
                )
                if value is None
            ]
            if missing:
                joined = ", ".join(missing)
                raise ValueError(f"TLS enabled but missing fields: {joined}")
        return self


class MQTTConfig(BaseModel):
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    discovery_prefix: str = "homeassistant"
    base_topic: str | None = None
    client_id: str | None = None
    keepalive: int = 60
    tls: MQTTTLSConfig = MQTTTLSConfig()

    def resolved_base_topic(self, identity: IdentityConfig) -> str:
        if self.base_topic:
            return self.base_topic
        suffix = identity.unique_suffix.replace(" ", "_")
        return f"bluesnap/{suffix}"

    def resolved_client_id(self, identity: IdentityConfig) -> str:
        return self.client_id or f"bluesnap-{identity.unique_suffix}"


class BluetoothSpeakerConfig(BaseModel):
    name: str
    mac: str
    keepalive_interval: int = 30

    @field_validator("mac")
    @classmethod
    def normalize_mac(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if len(cleaned.split(":")) != 6:
            raise ValueError("Speaker MAC must be in AA:BB:CC:DD:EE:FF format")
        return cleaned


class BluetoothConfig(BaseModel):
    adapter: str = "hci0"
    speaker: BluetoothSpeakerConfig
    reconnect_interval: int = 10


class SnapcastConfig(BaseModel):
    server_host: str
    server_stream: str | None = None
    server_port: int = 1704
    control_port: int = 1780
    latency: int = 80
    client_name: str | None = None
    buffer_ms: int = 200
    audio_backend: Literal["alsa", "pulse", "pipewire", "bluealsa"] = "bluealsa"
    audio_device: str | None = None

    def resolved_client_name(self, identity: IdentityConfig) -> str:
        return self.client_name or identity.instance_name


class LoggingSyslogConfig(BaseModel):
    enabled: bool = False
    host: str | None = None
    port: int = 6514
    protocol: Literal["tcp", "udp"] = "tcp"
    tls: bool = True

    @model_validator(mode="after")
    def validate_when_enabled(self) -> LoggingSyslogConfig:
        if self.enabled and not self.host:
            raise ValueError("Syslog enabled but host not provided")
        return self


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    syslog: LoggingSyslogConfig = LoggingSyslogConfig()


class TelemetryConfig(BaseModel):
    interval: int = 15
    metrics: list[Literal["cpu", "memory", "load", "temperature", "bluetooth"]] = Field(
        default_factory=lambda: ["cpu", "memory", "load", "temperature"]
    )


class WatchdogConfig(BaseModel):
    watchdog_interval: int = 60
    max_retries: int = 3
    reboot_on_failure: bool = True


class BluesnapConfig(BaseModel):
    identity: IdentityConfig
    mqtt: MQTTConfig
    bluetooth: BluetoothConfig
    snapcast: SnapcastConfig
    telemetry: TelemetryConfig = TelemetryConfig()
    watchdog: WatchdogConfig = WatchdogConfig()
    logging: LoggingConfig = LoggingConfig()

    @model_validator(mode="after")
    def inject_defaults(self) -> BluesnapConfig:
        # Force evaluation of default-dependent helpers to ensure config issues surface early.
        _ = self.mqtt.resolved_base_topic(self.identity)
        _ = self.mqtt.resolved_client_id(self.identity)
        _ = self.snapcast.resolved_client_name(self.identity)
        return self

    def effective_topics(self) -> dict[str, str]:
        """Return commonly used MQTT topic prefixes."""
        base_topic = self.mqtt.resolved_base_topic(self.identity)
        discovery = self.mqtt.discovery_prefix.rstrip("/")
        device_id = self.identity.instance_name.replace(" ", "_")
        return {
            "base": base_topic,
            "discovery": f"{discovery}/device/{device_id}",
        }


def load_config(path: str | Path) -> BluesnapConfig:
    """
    Load and validate configuration from the provided YAML file.

    Parameters
    ----------
    path:
        Path pointing to the YAML configuration file.
    """

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    data = yaml.safe_load(config_path.read_text()) or {}
    try:
        return BluesnapConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Invalid configuration: {exc}") from exc


__all__ = [
    "BluesnapConfig",
    "IdentityConfig",
    "MQTTConfig",
    "MQTTTLSConfig",
    "BluetoothConfig",
    "BluetoothSpeakerConfig",
    "SnapcastConfig",
    "LoggingConfig",
    "LoggingSyslogConfig",
    "TelemetryConfig",
    "WatchdogConfig",
    "load_config",
]
