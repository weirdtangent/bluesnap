# Bluesnap Bridge

Bluesnap is a Raspberry Pi bridge that keeps a Bluetooth speaker paired, listens to a
Snapcast stream, and reports its health/controls to Home Assistant via MQTT v5 using the
2024 `homeassistant/device/*` discovery topics. The project acts as the glue between
MusicAssistant → Snapserver → Pi → Bluetooth speaker so an existing multi-room audio setup
can reuse wireless speakers that only expose Bluetooth.

## Current Status

This repository currently contains the configuration schema, sample config, and project
scaffolding (pyproject + formatting config). Upcoming work will add:

- A Bluetooth controller with a 10-second watchdog to reconnect dropped speakers.
- A Snapcast client supervisor that binds the Snapcast audio stream to the Bluetooth sink.
- An MQTT v5 bridge publishing discovery payloads (telemetry + volume control) and
  listening for control commands from Home Assistant.
- Telemetry + watchdog modules feeding MQTT and system logs.
- An idempotent `scripts/setup.py` that installs dependencies, refreshes systemd units, and
  restarts services after each `git pull`.

## Getting Started

1. **Install prerequisites, clone into `/opt`, & create a virtual environment with `uv`**

   ```bash
   sudo apt update
   sudo apt install -y git neovim curl
   curl -LsSf https://astral.sh/uv/install.sh | sh
   export PATH="$HOME/.local/bin:$PATH"  # ensure uv is on PATH for this shell
   cd /opt
   sudo git clone https://github.com/weirdtangent/bluesnap.git
   sudo chown -R "$USER":"$USER" /opt/bluesnap
   cd /opt/bluesnap
   uv venv .venv
   source .venv/bin/activate
   uv pip install -e '.[dev]'
   ```

2. **Copy and edit the configuration**

   ```bash
   cp config/bluesnap.conf.sample config/bluesnap.yaml
   $EDITOR config/bluesnap.yaml
   ```

   Fill in your Snapserver host, MQTT broker, Bluetooth speaker MACs, and logging targets.

3. **Run formatting and linting**

   ```bash
   source .venv/bin/activate
   uv run ruff check .
   uv run black --check .
   ```

4. **Next steps**

   Implementation of the runtime services is in progress. Once the upcoming `scripts/setup.py`
   is available it will handle installing apt packages (bluez, snapclient), refreshing the
   systemd unit, and restarting the bridge so you can rapidly test new commits.

## Configuration Reference

The loader expects YAML at `config/bluesnap.yaml`. Every available field is documented in
[`config/bluesnap.conf.sample`](config/bluesnap.conf.sample). Highlights:

- `identity`: names and suffixes used for MQTT topics and discovery payloads.
- `mqtt`: MQTT v5 broker, TLS options, `homeassistant/device/*` discovery prefix, and base topic.
- `bluetooth`: adapter name, list of speakers (name + MAC + keepalive), default speaker, and the
  10-second reconnect interval.
- `snapcast`: server host/port, control port, latency/buffer targets, backend (alsa/pulse/pipewire/
  bluealsa), and optional explicit audio device string.
- `telemetry`: interval and which metrics (cpu/memory/load/temp/bluetooth) to publish.
- `watchdog`: thresholds for restarting components or rebooting the Pi when repeated failures
  occur.
- `logging`: log level plus optional remote syslog target.

## Contributing

- Run `ruff check .` and `black .` before pushing.
- Keep filenames/directories lowercase (e.g., `bluesnap/...`).
- Logging statements should use double quotes, log device names instead of raw IDs, and wrap
  raw `device_id` values in parentheses when they must be logged.

MIT Licensed.
