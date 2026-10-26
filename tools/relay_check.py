"""End-to-end check of the relay with a throwaway pairing: encrypt, upload, list, download,
decrypt, and confirm wrong keys and oversized files are refused. Deletes its box at the end.

    python tools/relay_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "addon"))
from car_review.pairing import Pairing  # noqa: E402

RELAY = "https://car-review-relay.ankicardreview.workers.dev/v1"


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

    try:
        p.decrypt("grades/x", r.content)
        check("file can't be passed off under another name", False)
    except Exception:
        check("file can't be passed off under another name", True)

    r = requests.get(box, timeout=30)
    check("listing shows the file", r.ok and [f["name"] for f in r.json()["files"]] == ["list"])

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
