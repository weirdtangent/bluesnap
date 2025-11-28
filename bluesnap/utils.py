"""
Miscellaneous helpers shared across modules.
"""

from __future__ import annotations

import subprocess


def resolve_controller_identifier(adapter: str) -> str:
    """
    Return the controller address (e.g., E4:5F:...) for a given adapter name (e.g., hci0).
    """

    try:
        result = subprocess.run(
            ["hciconfig", adapter],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else "unknown error"
        raise RuntimeError(f"Failed to query adapter '{adapter}': {stderr}") from exc

    for line in result.stdout.splitlines():
        if "BD Address" in line:
            return line.split("BD Address:")[1].split()[0].strip()
    raise RuntimeError(f"Unable to determine controller identifier for adapter '{adapter}'")


__all__ = ["resolve_controller_identifier"]
