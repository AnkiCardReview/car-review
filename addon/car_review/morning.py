"""The morning list: shortly after Anki's day starts, ask Anki's own scheduler for the exact queue
for the new day and send it to the phone, so the car never plays a card Anki wouldn't show.

Runs as a silent scheduled task (Windows Task Scheduler) without Anki open. It opens a temporary
COPY of the collection with Anki's engine (the `anki` package, same version as your Anki), so the
real collection is never touched. Anki writes `morning.json` (profile, collection path, decks)
every time it sends a list, so the job always uses your current settings.

Many laptops can't wake themselves for a scheduled task (wake timers off, Modern Standby). So when
Anki closes on the charger in the evening, `helper overnight` also keeps the laptop awake until
just after the task's time and sends the list itself if the task hasn't.

    python -m car_review.helper morning
    python -m car_review.helper schedule      # create or update the daily task
    python -m car_review.helper unschedule
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .common import HOME

SETTINGS = HOME / "morning.json"
TASK_NAME = "Car Review morning list"


def save_settings(**fields) -> None:
    """Called by the add-on with profile, collection path, decks, days_ahead, auto_voice, rollover."""
    HOME.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(fields))


def build_morning_pack() -> dict:
    """Today's exact list from a copy of the collection, plus the next days for preloading audio."""
    if not SETTINGS.exists():
        raise RuntimeError("Car Review hasn't sent a list from Anki yet, so the morning job doesn't know your decks.")
    settings = json.loads(SETTINGS.read_text())
    source = Path(settings["collection"])
    try:
        from anki.collection import Collection  # Anki's own engine, same version as your Anki
    except ImportError as err:
        raise RuntimeError("The morning list needs Anki's engine in Car Review's Python.") from err
    from .builder import build_pack

    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal"):
            if Path(str(source) + suffix).exists():
                shutil.copy2(str(source) + suffix, Path(tmp) / ("collection.anki2" + suffix))
        col = Collection(str(Path(tmp) / "collection.anki2"))
        try:
            return build_pack(col, settings["decks"], days_ahead=int(settings.get("days_ahead", 3)))
        finally:
            col.close(downgrade=False)


def task_time() -> str:
    """Half an hour after Anki's day starts (its rollover hour, 4 am by default)."""
    rollover = 4
    if SETTINGS.exists():
        rollover = int(json.loads(SETTINGS.read_text()).get("rollover", 4))
    return f"{rollover:02d}:30"


def schedule(python: Path, workdir: Path) -> str:
    """Create or update the daily task. Windows only for now."""
    if sys.platform != "win32":
        raise RuntimeError("The morning job is only set up automatically on Windows so far.")
    pythonw = python.with_name("pythonw.exe") if python.with_name("pythonw.exe").exists() else python
    script = f"""
$a = New-ScheduledTaskAction -Execute '{pythonw}' -Argument '-m car_review.helper morning' -WorkingDirectory '{workdir}'
$t = New-ScheduledTaskTrigger -Daily -At '{task_time()}'
$s = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName '{TASK_NAME}' -Description 'Sends today''s Car Review list to your phone (made by the Car Review Anki add-on).' -Action $a -Trigger $t -Settings $s -Force | Out-Null
"""
    run = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=60)
    if run.returncode != 0:
        raise RuntimeError(run.stderr.strip()[-500:])
    return f"daily at {task_time()}"


def unschedule() -> None:
    if sys.platform == "win32":
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false -ErrorAction SilentlyContinue"],
                       capture_output=True, text=True, timeout=60)
