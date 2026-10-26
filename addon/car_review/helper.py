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

from .pairing import Pairing

HOME = Path.home() / "CarReview"
CACHE = HOME / "audio-cache"
STATUS = HOME / "status.json"
PAIRING = HOME / "pairing.json"
RELAY = "https://car-review-relay.ankicardreview.workers.dev/v1"
APP_URL = "https://ankicardreview.github.io/car-review/"
RATE = 24000
BATCH = 25


def load_pairing(create: bool = True) -> Pairing:
    if PAIRING.exists():
        return Pairing.from_code(json.loads(PAIRING.read_text())["code"])
    if not create:
        raise SystemExit("not paired yet")
    pairing = Pairing.new()
    HOME.mkdir(parents=True, exist_ok=True)
    PAIRING.write_text(json.dumps({"code": pairing.code, "created": int(time.time())}))
    return pairing


def pairing_link(pairing: Pairing) -> str:
    # The secret goes after "#", which browsers never send to any server.
    return f"{APP_URL}#pair={pairing.code}"


def write_status(**fields) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    fields["updated"] = int(time.time())
    STATUS.write_text(json.dumps(fields))


def keep_awake(on: bool) -> None:
    """Ask Windows not to sleep while working (closing the lid still sleeps)."""
    if sys.platform == "win32":
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


class Voice:
    """Kokoro Heart, loaded on first use."""

    def __init__(self) -> None:
        self.pipeline = None

    def mp3(self, text: str) -> bytes:
        import lameenc
        import numpy as np

        if self.pipeline is None:
            from kokoro import KPipeline

            self.pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
        chunks = [a.numpy() for _, _, a in self.pipeline(text, voice="af_heart", speed=1.0)]
        audio = np.concatenate(chunks) if chunks else np.zeros(RATE // 10, dtype=np.float32)
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
        enc = lameenc.Encoder()
        enc.set_bit_rate(48)
        enc.set_in_sample_rate(RATE)
        enc.set_channels(1)
        enc.set_quality(2)
        return enc.encode(pcm) + enc.flush()


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


def phone_list(pack: dict, cards: list[dict], pairing: Pairing, pending: int) -> bytes:
    """The list the phone downloads: clips named by their relay name, nothing else changed."""
    out = dict(pack, cards=[], pending=pending)
    for c in cards:
        c = dict(c)
        c["q_audio"] = pairing.audio_name(c["q_audio"])
        c["a_audio"] = pairing.audio_name(c["a_audio"])
        out["cards"].append(c)
    return json.dumps(out, separators=(",", ":")).encode()


def send(pack_path: Path) -> None:
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pairing = load_pairing()
    relay = Relay(pairing)
    voice = Voice()
    CACHE.mkdir(parents=True, exist_ok=True)
    keep_awake(True)
    cards = pack["cards"]
    started = time.time()
    try:
        on_relay = relay.names("audio/")
        ready: list[dict] = []
        made = 0
        for i, card in enumerate(cards):
            for fp, text in ((card["q_audio"], card["question"]), (card["a_audio"], card["answer"])):
                path = CACHE / f"{fp}.mp3"
                if not path.exists():
                    path.write_bytes(voice.mp3(text))
                    made += 1
                name = pairing.audio_name(fp)
                if name not in on_relay:
                    relay.put(name, path.read_bytes())
                    on_relay.add(name)
            ready.append(card)
            if len(ready) % BATCH == 0 or i == len(cards) - 1:
                relay.put("list", phone_list(pack, ready, pairing, pending=len(cards) - len(ready)))
                write_status(state="sending", ready=len(ready), total=len(cards), made=made,
                             elapsed=int(time.time() - started), made_at=pack["made_at"])
        if not cards:
            relay.put("list", phone_list(pack, [], pairing, pending=0))
        write_status(state="done", ready=len(cards), total=len(cards), made=made,
                     elapsed=int(time.time() - started), made_at=pack["made_at"])
    except Exception as err:
        write_status(state="error", error=str(err), detail=traceback.format_exc()[-2000:])
        raise
    finally:
        keep_awake(False)


def main() -> None:
    ap = argparse.ArgumentParser(prog="car_review.helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="make missing audio, encrypt and upload a car list")
    s.add_argument("--pack", type=Path, required=True)
    sub.add_parser("status", help="print the last progress report")
    sub.add_parser("link", help="print the phone pairing link")
    q = sub.add_parser("qr", help="save the pairing QR code as a PNG")
    q.add_argument("--out", type=Path, required=True)
    sub.add_parser("repair", help="forget the old phone: delete its box and make a new pairing")
    args = ap.parse_args()
    if args.cmd == "send":
        send(args.pack)
    elif args.cmd == "status":
        print(STATUS.read_text() if STATUS.exists() else "{}")
    elif args.cmd == "link":
        print(pairing_link(load_pairing()))
    elif args.cmd == "qr":
        import segno

        segno.make(pairing_link(load_pairing()), error="m").save(str(args.out), scale=8, border=3)
        print(args.out)
    elif args.cmd == "repair":
        if PAIRING.exists():
            old = load_pairing(create=False)
            requests.delete(f"{RELAY}/{old.box_id}", headers={"X-Write-Key": old.write_key}, timeout=60)
            PAIRING.unlink()
        load_pairing()
        print("new pairing made; scan the new QR code on your phone")


if __name__ == "__main__":
    main()
