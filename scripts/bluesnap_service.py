#!/usr/bin/env python3
"""
Main entrypoint that wires together configuration, Bluetooth controller,
Snapcast manager, MQTT bridge, and future health/telemetry loops.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from bluesnap.bluetooth_controller import BluetoothController
from bluesnap.config import BluesnapConfig, load_config
from bluesnap.mqtt_bridge import MQTTBridge
from bluesnap.snapcast_bridge import SnapcastManager


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bluesnap service orchestrator")
    parser.add_argument(
        "--config",
        default="config/bluesnap.yaml",
        help="Path to YAML configuration file (default: config/bluesnap.yaml)",
    )
    parser.add_argument("--log-level", default=None, help="Override configured log level")
    return parser.parse_args()


def configure_logging(config: BluesnapConfig, override: str | None) -> None:
    level = override or config.logging.level
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def run_service(config_path: Path) -> None:
    config = load_config(config_path)
    configure_logging(config, None)
    loop = asyncio.get_running_loop()

    bluetooth = BluetoothController(config.bluetooth, loop=loop)
    snapcast = SnapcastManager(config.snapcast, config.identity, loop=loop)
    mqtt_bridge = MQTTBridge(config, bluetooth, snapcast, loop=loop)

    await bluetooth.start()
    await snapcast.start()
    await mqtt_bridge.start()

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logging.getLogger(__name__).info("shutdown signal received")
        stop_event.set()

    loop.add_signal_handler(signal.SIGTERM, _signal_handler)
    loop.add_signal_handler(signal.SIGINT, _signal_handler)

    await stop_event.wait()
    await mqtt_bridge.stop()
    await snapcast.stop()
    await bluetooth.stop()


def main() -> int:
    args = parse_args()
    return asyncio.run(run_service(Path(args.config)))


if __name__ == "__main__":
    raise SystemExit(main())
