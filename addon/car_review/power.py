"""Is the computer plugged in? No dependencies, so both Anki and the helper can use it."""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path


def on_charger() -> bool:
    """True when plugged in (or when power status can't be read, e.g. a desktop)."""
    if sys.platform == "win32":
        return _windows()
    if sys.platform == "darwin":
        return _mac()
    return _linux()


def _windows() -> bool:
    class Status(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

    s = Status()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        return True
    return s.ACLineStatus != 0  # 0 = on battery, 1 = plugged in, 255 = unknown


def _mac() -> bool:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "Battery Power" not in out  # "Now drawing from 'AC Power'" or "'Battery Power'"


def _linux() -> bool:
    try:
        supplies = [p for p in Path("/sys/class/power_supply").iterdir() if (p / "online").exists()]
        return not supplies or any((p / "online").read_text().strip() == "1" for p in supplies)
    except OSError:
        return True
