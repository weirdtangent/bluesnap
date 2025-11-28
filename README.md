# Bluesnap Bridge

Bluesnap is a Raspberry Pi bridge that keeps a Bluetooth speaker paired, listens to a
Snapcast stream, and reports its health/controls to Home Assistant via MQTT v5. The project
acts as the glue between MusicAssistant → Snapserver → Pi → Bluetooth speaker so an existing multi-room audio setup can reuse wireless speakers that only expose Bluetooth.

## Current Status

Bluesnap currently ships with:

- A Bluetooth controller/watchdog that keeps the default speaker paired and reconnects
  automatically.
- A Snapcast supervisor that drives `snapclient` via PipeWire or BlueALSA and exposes volume
  controls through MQTT.
- A telemetry publisher that feeds MQTT-based sensors for Home Assistant, plus discovery for
  the volume control.
- An idempotent `scripts/setup.py` helper that installs dependencies, configures systemd, and
  enables console auto-login so the audio stack survives reboots.

## Getting Started

> **Note:** You are responsible for getting Linux + Bluetooth audio working on your Pi.
> Debian 13 (Trixie) never exposed our speaker after days of debugging, and even on
> Debian 12 (Bookworm) we still had to do the usual pairing/audio troubleshooting. Make
> sure your speaker shows up as an audio device (e.g., via PipeWire or BlueALSA) before
> investing time in the steps below.

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
   python3 scripts/setup.py
   ```

   On the first run, this bootstraps `.venv` and installs the `bluesnap-setup` console script
   inside `.venv/bin`. After that initial pass you can simply run `./.venv/bin/bluesnap-setup`
   (or add `.venv/bin` to your `PATH`) to reapply the setup without referencing the Python file.

   The setup helper will:

   - Install/refresh required apt packages (`bluez`, `snapclient`, `python3-venv`, `curl`).
   - Ensure Astral's `uv` CLI is present, create/refresh `.venv`, and install Python deps.
   - Install or update the bundled systemd unit so `bluesnap.service` starts on boot.
   - Add your user to the `bluetooth` group (requires re-login), ensure the BlueZ service is
     running, unblock/power on the adapter, and restart the bridge so changes take effect.

   Running this after every `git pull` keeps the Pi in sync. If you prefer manual control,
   pass `--skip-systemd` and launch the bridge ad-hoc with `bluesnap-service`. Both commands
   default to `config/bluesnap.yaml`; use `--config` if you keep the file elsewhere.

4. **Ensure console auto-login is enabled (required for audio stack)**

   `bluesnap-setup` now configures Raspberry Pi OS to auto-login the `bluesnap`
   user on tty1 so the PipeWire session starts at boot. If you prefer to do it
   manually (or want to double-check), run `sudo raspi-config` and set:

   ```
   System Options → Boot / Auto Login → Console Autologin
   ```

   Reboot afterwards so the change takes effect.

5. **Verify the installation**

   ```bash
   sudo systemctl status bluesnap.service
   journalctl -u bluesnap.service -n 50 --no-pager
   sudo -u bluesnap snapclient --list-soundcards
   ```

   You should see `bluesnap.service` active, recent telemetry logs, and your Bluetooth
   speaker listed as a PipeWire/BlueALSA soundcard.

## Configuration Reference

The loader expects YAML at `config/bluesnap.yaml`. Every available field is documented in
[`config/bluesnap.conf.sample`](config/bluesnap.conf.sample). Highlights:

- `identity`: names and suffixes used for MQTT topics and discovery payloads.
- `mqtt`: MQTT v5 broker, TLS options, classic Home Assistant discovery prefix, and base topic.
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

Each run of `bluesnap-setup` reapplies the systemd unit, reloads
the daemon, and restarts the service, so it is safe to run after every `git pull`.

### Audio backend notes

The default configuration targets PipeWire on Raspberry Pi OS. Console auto-login is required so
the per-user PipeWire session starts automatically; without it the speaker will disconnect after
reboots or when no sessions are logged in. If you prefer BlueALSA instead, set
`snapcast.audio_backend` to `bluealsa` and provide the `audio_device` identifier from
`snapclient --list-soundcards`.

## Contributing

- Run `ruff check .` and `black .` before pushing.
- Keep filenames/directories lowercase (e.g., `bluesnap/...`).
- Logging statements should use double quotes, log device names instead of raw IDs, and wrap
  raw `device_id` values in parentheses when they must be logged.

MIT Licensed.
