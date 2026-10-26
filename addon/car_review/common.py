"""Where Car Review keeps its files, and the pairing with the phone.

Standard library only, so both Anki and the helper can use it.

Everything lives in one folder, `CarReview` in your home folder (set CAR_REVIEW_HOME to use
another one, e.g. for testing). Deleting that folder removes Car Review's voice, audio and
pairing completely; your Anki collection is never stored there.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .pairing import Pairing

HOME = Path(os.environ.get("CAR_REVIEW_HOME") or Path.home() / "CarReview")
CACHE = HOME / "audio-cache"         # voiced clips, kept permanently
STATUS = HOME / "status.json"        # the helper's last progress report
PAIRING = HOME / "pairing.json"      # the secret shared with the phone
INBOX = HOME / "inbox"               # decrypted grade files waiting to be applied
PACK = HOME / "outbox" / "pack.json"  # the latest car list, for the helper to send
VENV = HOME / "venv"                 # the helper's own Python (made by voice setup)
VOICE_DIR = HOME / "voice"           # the voice model files

RELAY = "https://car-review-relay.ankicardreview.workers.dev/v1"
APP_URL = "https://ankicardreview.github.io/car-review/"


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
