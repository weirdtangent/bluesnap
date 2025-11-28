# Bluesnap Bridge

Bluesnap is a Raspberry Pi bridge that keeps a Bluetooth speaker paired, listens to a
Snapcast stream, and reports its health/controls to Home Assistant via MQTT v5 using the
2024 `homeassistant/device/*` discovery topics. The project acts as the glue between
MusicAssistant → Snapserver → Pi → Bluetooth speaker so an existing multi-room audio setup
can reuse wireless speakers that only expose Bluetooth.

## Current Status

This repository currently contains the configuration schema, sample config, Bluetooth
controller/watchdog, Snapcast supervisor, MQTT v5 bridge, provisioning helpers, and the
initial service orchestrator. Upcoming work will add:

- Telemetry + watchdog modules feeding MQTT and system logs.
- An idempotent `scripts/setup.py` that installs dependencies, refreshes systemd units, and
  restarts services after each `git pull`.

## Getting Started

1. **Install prerequisites and clone into `/opt`**

   ```bash
   sudo apt update
   sudo apt install -y git neovim curl
   curl -LsSf https://astral.sh/uv/install.sh | sh
   export PATH="$HOME/.local/bin:$PATH"  # ensure uv is on PATH for this shell
   cd /opt
   sudo git clone https://github.com/weirdtangent/bluesnap.git
   sudo chown -R "$USER":"$USER" /opt/bluesnap
   cd /opt/bluesnap
   ```

2. **Copy and edit the configuration**

   ```bash
   cp config/bluesnap.conf.sample config/bluesnap.yaml
   $EDITOR config/bluesnap.yaml
   ```

   Fill in your Snapserver host, MQTT broker, Bluetooth speaker MACs, and logging targets.

3. **Run the bluesnap system setup**

   ```bash
   bluesnap-setup --config config/bluesnap.yaml
   ```

   The setup helper will:

   - Install/refresh required apt packages (`bluez`, `snapclient`, `python3-venv`, `curl`).
   - Ensure Astral's `uv` CLI is present, create/refresh `.venv`, and install Python deps.
   - Install or update the bundled systemd unit so `bluesnap.service` starts on boot.
   - Add your user to the `bluetooth` group (requires re-login), ensure the BlueZ service is
     running, unblock/power on the adapter, and restart the bridge so changes take effect.

   Running this after every `git pull` keeps the Pi in sync. If you prefer manual control,
   pass `--skip-systemd` and launch the bridge ad-hoc with `bluesnap-service --config config/bluesnap.yaml`.

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

## Manual Bluetooth provisioning

Use `bluetoothctl` directly to pair/trust/connect your speaker, then copy the MAC into
`config/bluesnap.yaml`. One common flow:

```bash
sudo bluetoothctl
[bluetooth]# select <controller_id>      # e.g. E4:5F:01:77:5E:1A
[bluetooth]# power on
[bluetooth]# pairable on
[bluetooth]# scan on                     # watch for "Device <MAC> <name>"
[bluetooth]# pair <MAC>                  # keep the speaker in pairing mode
[bluetooth]# trust <MAC>
[bluetooth]# connect <MAC>
[bluetooth]# info <MAC>                  # confirm Paired/Trusted/Connected are "yes"
[bluetooth]# quit
```

Once paired and trusted, the Bluesnap controller keeps that speaker online with its 10-second
watchdog loop, and the MQTT bridge exposes telemetry/control entities in Home Assistant.

## Service management

- Check status: `sudo systemctl status bluesnap.service`
- View logs: `journalctl -u bluesnap.service -f`
- Restart manually: `sudo systemctl restart bluesnap.service`

Each run of `bluesnap-setup --config config/bluesnap.yaml` reapplies the systemd unit, reloads
the daemon, and restarts the service, so it is safe to run after every `git pull`.

## Contributing

- Run `ruff check .` and `black .` before pushing.
- Keep filenames/directories lowercase (e.g., `bluesnap/...`).
- Logging statements should use double quotes, log device names instead of raw IDs, and wrap
  raw `device_id` values in parentheses when they must be logged.

MIT Licensed.
