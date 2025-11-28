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
import logging
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


def ensure_virtualenv(uv_path: str, venv_path: Path) -> None:
    run([uv_path, "venv", str(venv_path)])
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
    run(["sudo", "usermod", "-aG", BLUETOOTH_GROUP, user], check=False)


def install_systemd_unit(repo_root: Path, config_path: Path) -> None:
    systemd_dir = repo_root / "systemd"
    template_path = systemd_dir / "bluesnap.service"
    if not template_path.exists():
        raise FileNotFoundError(f"service template missing: {template_path}")
    template = template_path.read_text()
    content = template.replace("{{REPO_PATH}}", str(repo_root)).replace(
        "{{CONFIG_PATH}}", str(config_path)
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
    repo_root = Path(__file__).resolve().parent.parent
    config_path = (repo_root / args.config).resolve()

    if not config_path.exists():
        logging.error("configuration file not found at %s", config_path)
        return 1

    ensure_apt_packages()
    uv_path = ensure_uv()
    ensure_virtualenv(uv_path, repo_root / args.venv)
    ensure_boot_script(repo_root)
    ensure_bluetooth_group()

    if not args.skip_systemd:
        install_systemd_unit(repo_root, config_path)
    else:
        logging.info("skipping systemd installation per flag")
    logging.info("setup complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
