"""Is the computer plugged in? No dependencies, so both Anki and the helper can use it."""

from __future__ import annotations

import ctypes
import sys


def on_charger() -> bool:
    """True when plugged in (or when power status can't be read, e.g. a desktop)."""
    if sys.platform != "win32":
        return True

    class Status(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

    s = Status()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        return True
    return s.ACLineStatus != 0  # 0 = on battery, 1 = plugged in, 255 = unknown
