#!/usr/bin/env python3
"""
Helper CLI to provision Bluetooth speakers for the Bluesnap bridge.

Examples:
    # Scan for nearby devices for 20 seconds, filtering names that contain "H6020"
    python scripts/bt_tools.py scan --filter H6020

    # Pair, trust, and connect a discovered device
    python scripts/bt_tools.py pair --mac AA:BB:CC:DD:EE:FF
    python scripts/bt_tools.py trust --mac AA:BB:CC:DD:EE:FF
    python scripts/bt_tools.py connect --mac AA:BB:CC:DD:EE:FF
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass

from bluesnap.utils import resolve_controller_identifier

AGENT_CAPABILITY = "NoInputNoOutput"

DEVICE_LINE = re.compile(
    r"Device (?P<mac>(?:[0-9A-F]{2}:){5}[0-9A-F]{2}) (?P<name>.+)", re.IGNORECASE
)


@dataclass(slots=True)
class CLIArgs:
    command: str
    mac: str | None = None
    adapter: str = "hci0"
    duration: int = 20
    name_filter: str | None = None


def normalize_global_options(argv: list[str]) -> list[str]:
    """Ensure adapter flag appears before subcommand."""

    prefix: list[str] = []
    remainder: list[str] = []
    skip_next = False
    for idx, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if token.startswith("--adapter"):
            if token == "--adapter":
                if idx + 1 >= len(argv):
                    raise ValueError("--adapter requires a value")
                value = argv[idx + 1]
                skip_next = True
            else:
                value = token.split("=", 1)[1]
            prefix.extend(["--adapter", value])
        else:
            remainder.append(token)
    return prefix + remainder


def parse_args(argv: Iterable[str]) -> CLIArgs:
    parser = argparse.ArgumentParser(description="Bluetooth provisioning helper")
    parser.set_defaults(command=None)
    parser.add_argument("--adapter", default="hci0", help="Bluetooth adapter (default: hci0)")

    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan for Bluetooth speakers")
    scan.add_argument("--duration", type=int, default=20, help="Scan duration in seconds")
    scan.add_argument("--filter", dest="name_filter", help="Optional case-insensitive name filter")

    for action in ("pair", "trust", "connect", "remove"):
        cmd = sub.add_parser(action, help=f"{action.capitalize()} a device by MAC address")
        cmd.add_argument("--mac", required=True, help="Device MAC (AA:BB:CC:DD:EE:FF)")
    combo = sub.add_parser("setup", help="Pair, trust, and connect in one step")
    combo.add_argument("--mac", required=True, help="Device MAC (AA:BB:CC:DD:EE:FF)")

    argv_list = normalize_global_options(list(argv))
    parsed = parser.parse_args(argv_list)
    return CLIArgs(
        command=parsed.command,
        mac=getattr(parsed, "mac", None),
        adapter=parsed.adapter,
        duration=getattr(parsed, "duration", 20),
        name_filter=getattr(parsed, "name_filter", None),
    )


async def run_btctl(commands: list[str], timeout: int = 30) -> str:
    """
    Execute bluetoothctl with the supplied newline-delimited commands.
    """

    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None
    payload = "\n".join(commands + ["quit"]) + "\n"
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(payload.encode("utf-8")),
        timeout=timeout,
    )

    if proc.returncode != 0:
        raise RuntimeError(stderr.decode().strip() or "bluetoothctl command failed")
    return stdout.decode()


async def scan_devices(
    controller_id: str,
    duration: int,
    name_filter: str | None,
) -> dict[str, str]:
    """
    Continuously read bluetoothctl output while scan is active and collect found devices.
    """

    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout

    for command in (
        f"select {controller_id}",
        "power on",
        f"agent {AGENT_CAPABILITY}",
        "default-agent",
        "pairable on",
        "scan on",
    ):
        proc.stdin.write(command.encode("utf-8") + b"\n")
        await proc.stdin.drain()

    devices: dict[str, str] = {}
    filter_lower = name_filter.lower() if name_filter else None
    loop = asyncio.get_event_loop()
    end_time = loop.time() + max(5, duration)

    while loop.time() < end_time:
        timeout = max(0.1, end_time - loop.time())
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        except TimeoutError:
            continue
        if not line:
            break
        text = line.decode(errors="ignore").strip()
        match = DEVICE_LINE.search(text)
        if match:
            mac = match.group("mac").upper()
            name = match.group("name").strip()
            if filter_lower and filter_lower not in name.lower():
                continue
            devices[mac] = name

    proc.stdin.write(b"scan off\nquit\n")
    await proc.stdin.drain()
    await proc.wait()
    return devices


def normalize_mac(mac: str | None) -> str:
    if not mac:
        raise ValueError("MAC address is required")
    cleaned = mac.strip().upper()
    if not DEVICE_LINE.match(f"Device {cleaned} dummy"):
        raise ValueError("MAC must be formatted as AA:BB:CC:DD:EE:FF")
    return cleaned


async def handle_scan(args: CLIArgs) -> None:
    controller_id = resolve_controller_identifier(args.adapter)
    devices = await scan_devices(controller_id, args.duration, args.name_filter)
    if not devices:
        print("No devices discovered. Try increasing --duration or moving closer.")
        return
    print(f"Discovered {len(devices)} device(s):")
    for mac, name in sorted(devices.items(), key=lambda item: item[1]):
        print(f"  {mac}  {name}")


async def handle_simple(args: CLIArgs, command: str) -> None:
    mac = normalize_mac(args.mac)
    controller_id = resolve_controller_identifier(args.adapter)
    output = await run_btctl(
        [
            f"select {controller_id}",
            "power on",
            f"agent {AGENT_CAPABILITY}",
            "default-agent",
            "pairable on",
            f"{command} {mac}",
        ]
    )
    print(output.strip())


async def handle_setup(args: CLIArgs) -> None:
    """Pair, trust, and connect sequentially."""

    for action in ("pair", "trust", "connect"):
        print(f"Running {action} for {args.mac} ...")
        try:
            await handle_simple(args, action)
        except RuntimeError as err:
            print(f"{action} failed: {err}", file=sys.stderr)
            break


async def _async_main(argv: Iterable[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)
    try:
        if args.command == "scan":
            await handle_scan(args)
        elif args.command == "setup":
            await handle_setup(args)
        else:
            await handle_simple(args, args.command)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except RuntimeError as err:
        print(f"bluetoothctl error: {err}", file=sys.stderr)
        return 2
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
