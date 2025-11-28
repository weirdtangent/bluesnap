#!/usr/bin/env python3
"""
Idempotent setup helper for the Bluesnap bridge.

Responsibilities:
1. Ensure apt packages (bluez, snapclient, python3-venv, curl) are installed.
2. Ensure Astral's uv CLI is available.
3. Create/refresh the project virtualenv and install dependencies.
4. Install or update the systemd unit so the bridge starts on boot.
5. Restart the service so code changes take effect immediately.
"""

from __future__ import annotations

import argparse
import grp
import logging
import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path

APT_PACKAGES = ["bluez", "snapclient", "python3-venv", "curl"]
BLUETOOTH_GROUP = "bluetooth"
SERVICE_NAME = "bluesnap.service"
UV_INSTALL_SCRIPT = "https://astral.sh/uv/install.sh"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Idempotent setup for the Bluesnap bridge")
    parser.add_argument(
        "--config",
        default="config/bluesnap.yaml",
        help="Path to the YAML config copied from config/bluesnap.conf.sample",
    )
    parser.add_argument(
        "--skip-systemd",
        action="store_true",
        help="Skip installing/enabling the systemd service",
    )
    parser.add_argument(
        "--venv",
        default=".venv",
        help="Virtualenv directory (default: .venv)",
    )
    return parser.parse_args()


def run(cmd: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> None:
    logging.info("running: %s", " ".join(cmd))
    subprocess.run(cmd, check=check, env=env)


def ensure_apt_packages() -> None:
    logging.info("ensuring apt packages: %s", ", ".join(APT_PACKAGES))
    run(["sudo", "apt-get", "update"])
    run(["sudo", "apt-get", "install", "-y", *APT_PACKAGES])


def ensure_uv() -> str:
    uv_path = shutil.which("uv")
    if uv_path:
        logging.info("uv already present at %s", uv_path)
        return uv_path
    logging.info("installing uv via %s", UV_INSTALL_SCRIPT)
    install_cmd = f"curl -LsSf {UV_INSTALL_SCRIPT} | sh"
    run(["/bin/bash", "-c", install_cmd])
    uv_path = shutil.which("uv")
    if not uv_path:
        raise RuntimeError("uv installation failed; ensure ~/.local/bin is on PATH")
    logging.info("uv installed at %s", uv_path)
    return uv_path


def _current_user_group() -> tuple[str, str]:
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    return user, group


def ensure_venv_ownership(venv_path: Path) -> None:
    if not venv_path.exists():
        return
    venv_uid = venv_path.stat().st_uid
    if venv_uid == os.getuid():
        return
    user, group = _current_user_group()
    owner = pwd.getpwuid(venv_uid).pw_name
    logging.warning(
        "virtualenv at %s is owned by %s; fixing permissions",
        venv_path,
        owner,
    )
    run(["sudo", "chown", "-R", f"{user}:{group}", str(venv_path)])


def ensure_virtualenv(uv_path: str, venv_path: Path) -> None:
    ensure_venv_ownership(venv_path)
    run([uv_path, "venv", "--clear", str(venv_path)])
    ensure_venv_ownership(venv_path)
    run([uv_path, "pip", "install", "-e", ".[dev]"])


def ensure_boot_script(repo_root: Path) -> None:
    boot_script = repo_root / "scripts" / "bluesnap-boot.sh"
    if not boot_script.exists():
        raise FileNotFoundError(f"boot script missing: {boot_script}")
    boot_script.chmod(0o755)
    logging.info("ensured boot script is executable")


def ensure_bluetooth_group() -> None:
    """Ensure the current user belongs to the bluetooth group."""

    user = Path.home().owner()
    logging.info("adding %s to %s group (if needed)", user, BLUETOOTH_GROUP)
    result = subprocess.run(
        ["groups", user],
        check=True,
        capture_output=True,
        text=True,
    )
    if BLUETOOTH_GROUP in result.stdout.split():
        logging.info("%s already in %s group", user, BLUETOOTH_GROUP)
        return
    run(["sudo", "usermod", "-aG", BLUETOOTH_GROUP, user], check=False)
    logging.warning(
        "Added %s to %s group. You must log out/in (or reboot) for this to take effect.",
        user,
        BLUETOOTH_GROUP,
    )


def ensure_bluetooth_service() -> None:
    logging.info("ensuring bluetooth.service is enabled and running")
    run(["sudo", "systemctl", "enable", "bluetooth.service"])
    run(["sudo", "systemctl", "restart", "bluetooth.service"])


def ensure_rfkill_unblocked() -> None:
    logging.info("unblocking bluetooth via rfkill")
    run(["sudo", "rfkill", "unblock", "bluetooth"], check=False)


def ensure_adapter_powered() -> None:
    logging.info("attempting to power on bluetooth adapter")
    command = "printf 'power on\nquit\n' | bluetoothctl"
    run(["sudo", "bash", "-lc", command], check=False)


def ensure_console_autologin(user: str) -> None:
    """Configure tty1 to auto-login the specified user."""

    dropin_dir = Path("/etc/systemd/system/getty@tty1.service.d")
    desired = (
        "[Service]\n"
        "ExecStart=\n"
        f"ExecStart=-/sbin/agetty --autologin {user} --noclear %I $TERM\n"
    )
    tmp = tempfile.NamedTemporaryFile("w", delete=False)
    try:
        tmp.write(desired)
        tmp.flush()
        run(["sudo", "install", "-D", "-m", "0644", tmp.name, str(dropin_dir / "autologin.conf")])
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    run(["sudo", "systemctl", "daemon-reload"])
    run(["sudo", "systemctl", "restart", "getty@tty1.service"])
    logging.info("enabled console auto-login for %s", user)


def install_systemd_unit(repo_root: Path, config_path: Path) -> None:
    systemd_dir = repo_root / "systemd"
    template_path = systemd_dir / "bluesnap.service"
    if not template_path.exists():
        raise FileNotFoundError(f"service template missing: {template_path}")
    template = template_path.read_text()
    user, group = _current_user_group()
    content = (
        template.replace("{{REPO_PATH}}", str(repo_root))
        .replace("{{CONFIG_PATH}}", str(config_path))
        .replace("{{SERVICE_USER}}", user)
        .replace("{{SERVICE_GROUP}}", group)
        .replace("{{SERVICE_UID}}", str(os.getuid()))
    )
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(content)
        temp_path = Path(tmp.name)
    run(["sudo", "cp", str(temp_path), f"/etc/systemd/system/{SERVICE_NAME}"])
    temp_path.unlink(missing_ok=True)
    run(["sudo", "systemctl", "daemon-reload"])
    run(["sudo", "systemctl", "enable", SERVICE_NAME])
    run(["sudo", "systemctl", "restart", SERVICE_NAME])
    logging.info("systemd service installed and restarted")


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.geteuid() == 0:
        logging.error("Do not run bluesnap-setup with sudo; rerun as the bluesnap user.")
        return 1
    repo_root = Path(__file__).resolve().parent.parent
    config_path = (repo_root / args.config).resolve()
    user, _ = _current_user_group()

    if not config_path.exists():
        logging.error("configuration file not found at %s", config_path)
        return 1

    ensure_apt_packages()
    uv_path = ensure_uv()
    ensure_virtualenv(uv_path, repo_root / args.venv)
    ensure_console_autologin(user)
    ensure_boot_script(repo_root)
    ensure_bluetooth_group()
    ensure_bluetooth_service()
    ensure_rfkill_unblocked()
    ensure_adapter_powered()

    if not args.skip_systemd:
        install_systemd_unit(repo_root, config_path)
    else:
        logging.info("skipping systemd installation per flag")
    logging.info("setup complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
