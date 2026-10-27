"""Car Review background helper: makes missing audio, encrypts, and uploads the car list.

Runs in the Car Review Python environment (it needs the Kokoro voice and `cryptography`),
started by the add-on so it can keep going after Anki closes:

    python -m car_review.helper send   --pack pack.json
    python -m car_review.helper overnight   # after closing Anki: wait for the next day's list
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

from .common import CACHE, HOME, INBOX, PACK, PAIRING, RELAY, STATUS, load_pairing, pairing_link
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
    # Cards not on the phone yet still count as siblings for the phone's fallback: [note, day].
    sent = {c["cid"] for c in cards}
    out["held_back"] = [[c["nid"], c["due_day"]] for c in pack["cards"] if c["cid"] not in sent]
    for c in cards:
        c = dict(c)
        c["q_audio"] = pairing.audio_name(c["q_audio"])
        c["a_audio"] = pairing.audio_name(c["a_audio"])
        out["cards"].append(c)
    if pack.get("announce"):
        rename = lambda a: dict(a, audio=pairing.audio_name(a["audio"]))  # noqa: E731
        out["announce"] = {"blocks": [rename(a) for a in pack["announce"]["blocks"]],
                           "end": rename(pack["announce"]["end"])}
    return json.dumps(out, separators=(",", ":")).encode()


def announce_clips(pack: dict) -> list[dict]:
    """The spoken cues in a list: "<deck> done." per block and "All decks done."."""
    a = pack.get("announce") or {}
    return [*a.get("blocks", []), *([a["end"]] if a.get("end") else [])]


LOCK = HOME / "helper.lock"
OVERNIGHT_LOCK = HOME / "overnight.lock"
LAST_SENT = HOME / "last-sent.json"  # the Anki day of the last list that reached the phone
OVERNIGHT_MAX_WAIT = 12 * 3600       # only wait overnight, not from a midday close
MORNING_DELAY = 35 * 60              # after the rollover; the 4:30 task goes first if it runs


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


def take_lock(lock: Path = LOCK) -> bool:
    """Only one send runs at a time. A later request just leaves a newer pack for it to pick up."""
    import os

    if lock.exists():
        try:
            if pid_alive(int(lock.read_text())):
                return False
        except ValueError:
            pass
    lock.write_text(str(os.getpid()))
    return True


def log(text: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}", flush=True)


def anki_day_at(pack: dict, when: float) -> int:
    """The Anki day number at a given time, from a list's day and its next rollover."""
    if when < pack["next_day_at"]:
        return pack["anki_today"]
    return pack["anki_today"] + 1 + int((when - pack["next_day_at"]) // 86400)


def last_sent_day() -> int:
    try:
        return int(json.loads(LAST_SENT.read_text())["anki_today"])
    except (OSError, ValueError, KeyError):
        return -1


def morning_send(progress: Progress) -> None:
    """Build today's exact list from Anki's scheduler (on a copy of the collection) and send it."""
    from . import morning

    try:
        pack = morning.build_morning_pack()
    except Exception as err:
        write_status(state="error", error=f"morning list: {err}")
        raise
    settings = json.loads(morning.SETTINGS.read_text())
    pack["voice_missing"] = bool(settings.get("auto_voice")) and on_charger()
    path = HOME / "outbox" / "pack.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pack), encoding="utf-8")
    send(path, progress)


def overnight(pack_path: Path, wait_until: float | None = None) -> None:
    """Started when Anki closes. On the charger in the evening, keep the laptop awake (the screen
    can still turn off) until shortly after Anki's next day starts, then send that day's exact
    list, so the phone has it before the morning drive even if the laptop can't wake itself for
    the 4:30 task. Gives up as soon as the laptop is unplugged, and skips the send if today's
    list already reached the phone (the 4:30 task, or Anki opened and closed again)."""
    if not take_lock(OVERNIGHT_LOCK):
        log("overnight: already waiting for the morning list")
        return
    try:
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
        target = wait_until if wait_until is not None else pack["next_day_at"] + MORNING_DELAY
        day = anki_day_at(pack, target)
        if not on_charger():
            log("overnight: on battery, not waiting (the 4:30 task runs when the laptop next wakes)")
            return
        if target - time.time() > OVERNIGHT_MAX_WAIT:
            log("overnight: the next Anki day is too far off, not waiting")
            return
        if last_sent_day() >= day:
            log("overnight: that day's list was already sent")
            return
        log(f"overnight: keeping the laptop awake until {time.strftime('%H:%M', time.localtime(target))} "
            "for the morning list (the screen can turn off)")
        keep_awake(True)
        try:
            while time.time() < target:
                if not on_charger():
                    log("overnight: unplugged, stopped waiting")
                    return
                if last_sent_day() >= day:
                    break
                time.sleep(min(60, max(1, target - time.time())))
            if last_sent_day() >= day:
                log("overnight: today's list was already sent")
                return
            log("overnight: making the morning list")
            morning_send(Progress("Car Review: morning list"))
            log(f"overnight: done ({'sent' if last_sent_day() >= day else 'not sent, see status'})")
        finally:
            keep_awake(False)  # the laptop can sleep again
    finally:
        OVERNIGHT_LOCK.unlink(missing_ok=True)


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
    voicing = False
    try:
        on_relay = relay.names("audio/")
        while True:
            loaded_at = pack_path.stat().st_mtime
            pack = json.loads(pack_path.read_text(encoding="utf-8"))
            cards = pack["cards"]
            # Once asked to make audio (Prepare audio), a newer quick list (closing Anki) keeps it going.
            voicing = voicing or pack.get("voice_missing", True)
            progress.title = "Car Review: making audio and sending" if voicing else "Car Review: sending to your car"

            # The few spoken cues first: made when voicing, uploaded whenever they're ready.
            for cue in announce_clips(pack):
                path = CACHE / f"{cue['audio']}.mp3"
                if not path.exists() and voicing and on_charger():
                    path.write_bytes(voice.mp3(cue["text"]))
                    made += 1
                name = pairing.audio_name(cue["audio"])
                if path.exists() and name not in on_relay:
                    relay.put(name, path.read_bytes())
                    on_relay.add(name)

            def clips(card: dict) -> tuple:
                return (card["q_audio"], card["question"]), (card["a_audio"], card["answer"])

            def cached(card: dict) -> bool:
                return all((CACHE / f"{fp}.mp3").exists() for fp, _ in clips(card))

            # Cards the phone can already play go in every list sent from here on, so a partial
            # list or a Stop never leaves the phone with fewer cards than it had.
            ready_idx = {i for i, c in enumerate(cards)
                         if cached(c) and all(pairing.audio_name(fp) in on_relay for fp, _ in clips(c))}
            to_voice = {i for i, c in enumerate(cards) if not cached(c)} if voicing else set()
            to_upload = {i for i, c in enumerate(cards) if i not in ready_idx and cached(c)}
            # Voice the nearest due days first; the phone list keeps Anki's play order.
            order = sorted(to_voice | to_upload, key=lambda i: (cards[i]["due_day"], i))
            # Count what takes the time: the cards needing audio, or else the cards to upload.
            progress.unit = "cards needing audio" if to_voice else "cards to upload"
            work_total, work_done = len(to_voice) or len(order), 0
            progress.step(0, work_total)
            if to_voice and ready_idx:
                # Voicing takes a while: the phone gets this list's ready cards straight away.
                ready = [cards[j] for j in sorted(ready_idx)]
                relay.put("list", phone_list(pack, ready, pairing, pending=len(cards) - len(ready)))
                write_status(state="sending", ready=len(ready), total=len(cards), made=made,
                             elapsed=int(time.time() - started), made_at=pack["made_at"])
            newer = False
            for i in order:
                if not progress.checkpoint():
                    break  # stopped by the user: the phone keeps every card that's ready
                if pack_path.stat().st_mtime != loaded_at:
                    newer = True
                    break
                for fp, text in clips(cards[i]):
                    path = CACHE / f"{fp}.mp3"
                    if not path.exists():
                        wait_for_charger(progress, ready=len(ready_idx), total=len(cards), made=made)
                        if progress.stopped:
                            break
                        path.write_bytes(voice.mp3(text))
                        made += 1
                    name = pairing.audio_name(fp)
                    if name not in on_relay:
                        relay.put(name, path.read_bytes())
                        on_relay.add(name)
                else:
                    ready_idx.add(i)
                if i not in to_voice and to_voice:
                    continue
                work_done += 1
                progress.step(work_done, work_total)
                if work_done % BATCH == 0 and work_done < work_total:
                    ready = [cards[j] for j in sorted(ready_idx)]
                    relay.put("list", phone_list(pack, ready, pairing, pending=len(cards) - len(ready)))
                    write_status(state="sending", ready=len(ready), total=len(cards), made=made,
                                 elapsed=int(time.time() - started), made_at=pack["made_at"])
            if newer:
                continue  # start over with the newer list; finished clips are reused
            ready = [cards[j] for j in sorted(ready_idx)]
            unvoiced = len(cards) - len(ready)
            relay.put("list", phone_list(pack, ready, pairing, pending=0, unvoiced=unvoiced))
            LAST_SENT.write_text(json.dumps({"anki_today": pack["anki_today"], "made_at": pack["made_at"],
                                             "sent": int(time.time())}))
            break
        state = "stopped" if progress.stopped else "done"
        write_status(state=state, ready=len(ready), total=len(cards), made=made, unvoiced=unvoiced,
                     elapsed=int(time.time() - started), made_at=pack["made_at"])
        message = f"{len(ready):,} cards ready on your phone" + (f" ({made:,} new clips made)" if made else "") + "."
        if unvoiced:
            message += (f" {unvoiced:,} cards have no audio yet: plug in and use "
                        "Tools ▸ Car Review ▸ Prepare audio.")
        progress.auto_close = True  # the result is also in Tools ▸ Car Review ▸ Status
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


def keep_timing(name: str, data: bytes) -> None:
    """Keep the phone's clip timings (times only, no card text) for tools/timing_report.py.
    The inbox is cleared on every fetch, so they're copied out; the last 14 days are kept."""
    try:
        timing = json.loads(data).get("timing")
    except ValueError:
        return
    if not timing:
        return
    folder = HOME / "timing"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (name.replace("/", "_") + ".json")).write_text(json.dumps(timing), encoding="utf-8")
    for old in sorted(folder.glob("*.json"), key=lambda f: f.stat().st_mtime)[:-14]:
        old.unlink()


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
    sub.add_parser("morning", help="send today's exact list from Anki's scheduler (run by the daily task)")
    o = sub.add_parser("overnight", help="on the charger, stay awake until Anki's next day, then send the morning list")
    o.add_argument("--pack", type=Path, default=PACK)
    o.add_argument("--wait-until", type=float, help="epoch seconds (for testing; default: rollover + 35 min)")
    sub.add_parser("schedule", help="create or update the daily morning-list task")
    sub.add_parser("unschedule", help="remove the daily morning-list task")
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
                keep_timing(name, data)
        print(json.dumps(names))
    elif args.cmd == "delete-grades":
        relay = Relay(load_pairing(create=False))
        for name in args.names:
            relay.session.delete(f"{relay.box}/{name}", timeout=60)
    elif args.cmd == "morning":
        morning_send(Progress("Car Review: morning list"))  # silent: no window at 4:30 am
    elif args.cmd == "overnight":
        overnight(args.pack, args.wait_until)
    elif args.cmd == "schedule":
        from . import morning

        print(morning.schedule(Path(sys.executable), Path(__file__).resolve().parent.parent))
    elif args.cmd == "unschedule":
        from . import morning

        morning.unschedule()


if __name__ == "__main__":
    main()
