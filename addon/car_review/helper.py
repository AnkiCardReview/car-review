"""Car Review background helper: makes missing audio, encrypts, and uploads the car list.

Runs in the Car Review Python environment (it needs the Kokoro voice and `cryptography`),
started by the add-on so it can keep going after Anki closes:

    python -m car_review.helper send   --pack pack.json
    python -m car_review.helper status

Audio is cached on this computer by fingerprint, so each clip is only ever made once. The
phone gets a partial list every 25 cards, so on a big first run it can start with the cards
that are already ready. Progress is written to status.json for the add-on to show.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
import traceback
from pathlib import Path

import requests

from .common import CACHE, HOME, INBOX, PAIRING, RELAY, STATUS, load_pairing, pairing_link
from .pairing import Pairing
from .power import on_charger
from .ui import Progress, run_with_window
from .voice import Voice, available_engine

BATCH = 25


def write_status(**fields) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    fields["updated"] = int(time.time())
    STATUS.write_text(json.dumps(fields))


_caffeinate = None


def keep_awake(on: bool) -> None:
    """Ask the computer not to sleep while working (closing the lid still sleeps)."""
    global _caffeinate
    if sys.platform == "win32":
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    elif sys.platform == "darwin":
        import os
        import subprocess

        if on and _caffeinate is None:
            # Stays awake while this process runs, and stops by itself if it ends.
            _caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        elif not on and _caffeinate is not None:
            _caffeinate.terminate()
            _caffeinate = None


def be_gentle() -> None:
    """Run at low priority (and the voice on 3 cores), so studying and anything else stay smooth."""
    if sys.platform == "win32":
        BELOW_NORMAL_PRIORITY_CLASS = 0x4000
        k32 = ctypes.windll.kernel32
        # Handles are pointer-sized: without these types the 64-bit handle gets truncated.
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass(k32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
    else:
        import os

        os.nice(10)


def wait_for_charger(progress: Progress | None = None, **status) -> None:
    """Pause while on battery; carry on when plugged back in."""
    if on_charger():
        return
    keep_awake(False)  # let the laptop sleep normally while unplugged
    write_status(state="paused (on battery)", **status)
    if progress:
        progress.note = "Paused: the laptop is on battery. Plug it in to continue."
    while not on_charger() and not (progress and progress.stopped):
        time.sleep(5)
    if progress:
        progress.note = ""
    keep_awake(True)


class Relay:
    def __init__(self, pairing: Pairing) -> None:
        self.p = pairing
        self.box = f"{RELAY}/{pairing.box_id}"
        self.session = requests.Session()
        self.session.headers["X-Write-Key"] = pairing.write_key

    def _retry(self, fn):
        for attempt in range(6):
            try:
                r = fn()
                if r.status_code < 500:
                    return r
            except requests.RequestException:
                pass
            time.sleep(min(60, 2 ** attempt))
        raise RuntimeError("relay unreachable")

    def names(self, prefix: str) -> set[str]:
        r = self._retry(lambda: self.session.get(self.box, params={"prefix": prefix}, timeout=60))
        return {f["name"] for f in r.json().get("files", [])} if r.ok else set()

    def put(self, name: str, data: bytes) -> None:
        blob = self.p.encrypt(name, data)
        r = self._retry(lambda: self.session.put(f"{self.box}/{name}", data=blob, timeout=120))
        if not r.ok:
            raise RuntimeError(f"upload of {name} failed: {r.status_code} {r.text[:200]}")

    def get(self, name: str) -> bytes | None:
        r = self._retry(lambda: self.session.get(f"{self.box}/{name}", timeout=60))
        return self.p.decrypt(name, r.content) if r.ok else None


def phone_list(pack: dict, cards: list[dict], pairing: Pairing, pending: int, unvoiced: int = 0) -> bytes:
    """The list the phone downloads: clips named by their relay name, nothing else changed."""
    out = dict(pack, cards=[], pending=pending, unvoiced=unvoiced)
    for c in cards:
        c = dict(c)
        c["q_audio"] = pairing.audio_name(c["q_audio"])
        c["a_audio"] = pairing.audio_name(c["a_audio"])
        out["cards"].append(c)
    return json.dumps(out, separators=(",", ":")).encode()


LOCK = HOME / "helper.lock"


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        k32.CloseHandle(ctypes.c_void_p(handle))
        return code.value == 259  # STILL_ACTIVE
    import os

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def take_lock() -> bool:
    """Only one send runs at a time. A later request just leaves a newer pack for it to pick up."""
    import os

    if LOCK.exists():
        try:
            if pid_alive(int(LOCK.read_text())):
                return False
        except ValueError:
            pass
    LOCK.write_text(str(os.getpid()))
    return True


def send(pack_path: Path, progress: Progress) -> None:
    """Upload the car list. Voices missing cards first if the pack asks for it (and only while
    plugged in); otherwise sends just the cards that already have audio. If a newer pack is
    saved while this runs, it switches to the newer one."""
    if not take_lock():
        progress.finish("Another Car Review send is already running; it will pick up this newer list.")
        return
    pairing = load_pairing()
    relay = Relay(pairing)
    voice = Voice()
    CACHE.mkdir(parents=True, exist_ok=True)
    be_gentle()
    keep_awake(True)
    started = time.time()
    made = 0
    try:
        on_relay = relay.names("audio/")
        while True:
            loaded_at = pack_path.stat().st_mtime
            pack = json.loads(pack_path.read_text(encoding="utf-8"))
            cards = pack["cards"]
            voicing = pack.get("voice_missing", True)
            progress.title = "Car Review: making audio and sending" if voicing else "Car Review: sending to your car"
            progress.unit = "cards"
            # Voice the nearest due days first; the phone list keeps Anki's play order.
            order = sorted(range(len(cards)), key=lambda i: (cards[i]["due_day"], i))
            ready_idx: set[int] = set()
            unvoiced = 0
            newer = False
            for n, i in enumerate(order):
                if not progress.checkpoint():
                    break  # stopped by the user: the phone keeps the cards already sent
                if pack_path.stat().st_mtime != loaded_at:
                    newer = True
                    break
                card = cards[i]
                clips = ((card["q_audio"], card["question"]), (card["a_audio"], card["answer"]))
                if not voicing and not all((CACHE / f"{fp}.mp3").exists() for fp, _ in clips):
                    unvoiced += 1
                else:
                    for fp, text in clips:
                        path = CACHE / f"{fp}.mp3"
                        if not path.exists():
                            wait_for_charger(progress, ready=len(ready_idx), total=len(cards), made=made)
                            path.write_bytes(voice.mp3(text))
                            made += 1
                        name = pairing.audio_name(fp)
                        if name not in on_relay:
                            relay.put(name, path.read_bytes())
                            on_relay.add(name)
                    ready_idx.add(i)
                progress.step(n + 1, len(cards))
                if len(ready_idx) and len(ready_idx) % BATCH == 0 and n + 1 < len(cards):
                    ready = [cards[j] for j in sorted(ready_idx)]
                    relay.put("list", phone_list(pack, ready, pairing, pending=len(cards) - n - 1, unvoiced=unvoiced))
                    write_status(state="sending", ready=len(ready), total=len(cards), made=made,
                                 elapsed=int(time.time() - started), made_at=pack["made_at"])
            if newer:
                continue  # start over with the newer list; finished clips are reused
            ready = [cards[j] for j in sorted(ready_idx)]
            relay.put("list", phone_list(pack, ready, pairing, pending=0, unvoiced=unvoiced))
            break
        state = "stopped" if progress.stopped else "done"
        write_status(state=state, ready=len(ready), total=len(cards), made=made, unvoiced=unvoiced,
                     elapsed=int(time.time() - started), made_at=pack["made_at"])
        message = f"{len(ready):,} cards ready on your phone" + (f" ({made:,} new clips made)" if made else "") + "."
        if unvoiced:
            message += (f" {unvoiced:,} cards have no audio yet: plug in and use "
                        "Tools ▸ Car Review ▸ Prepare audio.")
        # A quick send with nothing to report closes its window by itself.
        progress.auto_close = not made and not unvoiced and not progress.stopped
        progress.finish(message + (" Stopped early." if progress.stopped else " Done ✓"))
    except Exception as err:
        write_status(state="error", error=str(err), detail=traceback.format_exc()[-2000:])
        progress.finish(f"Stopped by an error: {err}")
        raise
    finally:
        keep_awake(False)
        LOCK.unlink(missing_ok=True)


def prepare(pack_path: Path, progress: Progress) -> None:
    """Make audio ahead of time (nearest due date first) without uploading anything."""
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    cards = sorted(pack["cards"], key=lambda c: c["due_day"])
    todo: dict[str, str] = {}
    for c in cards:
        for fp, text in ((c["q_audio"], c["question"]), (c["a_audio"], c["answer"])):
            if fp not in todo and not (CACHE / f"{fp}.mp3").exists():
                todo[fp] = text
    voice = Voice()
    CACHE.mkdir(parents=True, exist_ok=True)
    be_gentle()
    keep_awake(True)
    started = time.time()
    total = len(todo)
    progress.unit = "clips"
    made = 0
    try:
        for fp, text in todo.items():
            if not progress.checkpoint():
                break
            wait_for_charger(progress, made=made, total=total)
            (CACHE / f"{fp}.mp3").write_bytes(voice.mp3(text))
            made += 1
            progress.step(made, total)
            if made % 20 == 0:
                write_status(state="preparing audio ahead", made=made, total=total, cards=len(cards),
                             elapsed=int(time.time() - started))
        state = "stopped" if progress.stopped else "prepared"
        write_status(state=state, made=made, total=total, cards=len(cards), elapsed=int(time.time() - started))
        progress.finish(f"{made:,} of {total:,} clips made." + (" Stopped early; it carries on next time." if progress.stopped else " Done ✓"))
    except Exception as err:
        write_status(state="error", error=str(err), detail=traceback.format_exc()[-2000:])
        progress.finish(f"Stopped by an error: {err}")
        raise
    finally:
        keep_awake(False)


def main() -> None:
    # Started with pythonw.exe (no console window), Python has nowhere to print; keep a log instead.
    if sys.stderr is None or sys.stdout is None:
        HOME.mkdir(parents=True, exist_ok=True)
        log = open(HOME / "helper.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    ap = argparse.ArgumentParser(prog="car_review.helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="make missing audio, encrypt and upload a car list")
    s.add_argument("--pack", type=Path, required=True)
    s.add_argument("--no-window", action="store_true", help="don't show the progress window")
    pr = sub.add_parser("prepare", help="make audio ahead of time for a list (no upload)")
    pr.add_argument("--pack", type=Path, required=True)
    pr.add_argument("--no-window", action="store_true", help="don't show the progress window")
    sub.add_parser("status", help="print the last progress report")
    sub.add_parser("link", help="print the phone pairing link")
    sub.add_parser("repair", help="forget the old phone: delete its box and make a new pairing")
    sub.add_parser("engine", help="print which voice engine is installed (onnx, torch or none)")
    sub.add_parser("voice-check", help="make one short clip to check the voice works")
    sub.add_parser("fetch-grades", help="download and decrypt the phone's grades into the inbox folder")
    d = sub.add_parser("delete-grades", help="delete grade files from the relay once they're applied")
    d.add_argument("names", nargs="+")
    args = ap.parse_args()
    if args.cmd in ("send", "prepare"):
        title = "Car Review: sending to your car" if args.cmd == "send" else "Car Review: making audio ahead"
        progress = Progress(title)
        work = (lambda p: send(args.pack, p)) if args.cmd == "send" else (lambda p: prepare(args.pack, p))
        if args.no_window:
            work(progress)
        else:
            run_with_window(progress, work)
    elif args.cmd == "status":
        print(STATUS.read_text() if STATUS.exists() else "{}")
    elif args.cmd == "link":
        print(pairing_link(load_pairing()))
    elif args.cmd == "engine":
        print(available_engine() or "none")
    elif args.cmd == "voice-check":
        voice = Voice()
        started = time.time()
        clip = voice.mp3("Car Review is ready. Drive safely.")
        print(json.dumps({"engine": voice.engine, "bytes": len(clip), "secs": round(time.time() - started, 1)}))
    elif args.cmd == "repair":
        if PAIRING.exists():
            old = load_pairing(create=False)
            requests.delete(f"{RELAY}/{old.box_id}", headers={"X-Write-Key": old.write_key}, timeout=60)
            PAIRING.unlink()
        load_pairing()
        print("new pairing made; scan the new QR code on your phone")
    elif args.cmd == "fetch-grades":
        relay = Relay(load_pairing(create=False))
        INBOX.mkdir(parents=True, exist_ok=True)
        for old in INBOX.glob("*.json"):
            old.unlink()
        names = sorted(relay.names("grades/"))
        for name in names:
            data = relay.get(name)
            if data is not None:
                (INBOX / (name.replace("/", "_") + ".json")).write_bytes(data)
        print(json.dumps(names))
    elif args.cmd == "delete-grades":
        relay = Relay(load_pairing(create=False))
        for name in args.names:
            relay.session.delete(f"{relay.box}/{name}", timeout=60)


if __name__ == "__main__":
    main()
