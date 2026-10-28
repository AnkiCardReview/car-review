"""End-to-end check of the relay with a throwaway pairing: encrypt, upload, list, download,
decrypt, and confirm wrong keys and oversized files are refused. Deletes its box at the end.

    python tools/relay_check.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "addon"))
from car_review.pairing import Pairing  # noqa: E402

# RELAY_URL=http://localhost:8787/v1 checks a local `wrangler dev` instead.
RELAY = os.environ.get("RELAY_URL", "https://car-review-relay.ankicardreview.workers.dev/v1")


def main() -> None:
    p = Pairing.new()
    box = f"{RELAY}/{p.box_id}"
    auth = {"X-Write-Key": p.write_key}
    results = []

    def check(label: str, ok: bool) -> None:
        results.append(ok)
        print(("PASS " if ok else "FAIL ") + label)

    secret_text = b'{"cards": ["made-up test card"]}'
    blob = p.encrypt("list", secret_text)
    check("encrypted data doesn't contain the plain text", b"made-up" not in blob)

    r = requests.put(f"{box}/list", data=blob, headers=auth, timeout=30)
    check(f"upload accepted ({r.status_code})", r.status_code == 200)

    r = requests.get(f"{box}/list", timeout=30)
    check("download matches upload", r.status_code == 200 and r.content == blob)
    check("downloaded file decrypts to the original", p.decrypt("list", r.content) == secret_text)
    stamp = r.headers.get("X-Updated", "")
    r = requests.get(f"{box}/list", params={"since": stamp}, timeout=30)
    check(f"unchanged file answers 204 with no body ({r.status_code})", r.status_code == 204 and not r.content)
    r = requests.get(f"{box}/list", params={"since": "1"}, timeout=30)
    check("changed file is sent again", r.status_code == 200 and r.content == blob)

    try:
        p.decrypt("grades/x", r.content)
        check("file can't be passed off under another name", False)
    except Exception:
        check("file can't be passed off under another name", True)

    r = requests.get(box, timeout=30)
    check("listing shows the file", r.ok and [f["name"] for f in r.json()["files"]] == ["list"])

    for name in ("audio/a1", "grades/g1", "grades/g2"):
        requests.put(f"{box}/{name}", data=b"x" * 10, headers=auth, timeout=30)
    r = requests.get(box, params={"prefix": "grades/"}, timeout=30)
    check("prefix listing shows only those files",
          r.ok and [f["name"] for f in r.json()["files"]] == ["grades/g1", "grades/g2"])
    r = requests.delete(f"{box}/grades/g1", headers=auth, timeout=30)
    r = requests.get(box, params={"prefix": "grades/"}, timeout=30)
    check("single file deleted", r.ok and [f["name"] for f in r.json()["files"]] == ["grades/g2"])

    r = requests.put(f"{box}/list", data=b"x", headers={"X-Write-Key": Pairing.new().write_key}, timeout=30)
    check(f"write with the wrong key refused ({r.status_code})", r.status_code == 403)

    r = requests.put(f"{box}/list", data=b"x", timeout=30)
    check(f"write with no key refused ({r.status_code})", r.status_code == 401)

    r = requests.put(f"{box}/big", data=b"0" * 2_000_000, headers=auth, timeout=60)
    check(f"oversized file refused ({r.status_code})", r.status_code == 413)

    r = requests.get(f"{RELAY}/{Pairing.new().box_id}/list", timeout=30)
    check(f"someone else's empty box has nothing ({r.status_code})", r.status_code == 404)

    r = requests.delete(box, headers=auth, timeout=30)
    check("box deleted", r.status_code == 200 and requests.get(f"{box}/list", timeout=30).status_code == 404)

    print(f"\n{sum(results)} of {len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
