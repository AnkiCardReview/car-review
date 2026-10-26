"""Build a car list from a temporary copy of a collection (the original is never opened)
and save it for the helper to send. Mirrors what the add-on's Send to Car does inside Anki.

    python tools/make_pack.py --profile "My Profile" --decks "Deck A" "Deck B" [--limit 3]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "addon"))
sys.path.insert(0, str(ROOT / "tools"))
from car_review.builder import build_pack  # noqa: E402
from car_review.helper import HOME  # noqa: E402
from collection_copy import collection_copy  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--decks", nargs="+", required=True)
    ap.add_argument("--limit", type=int, default=0, help="keep only the first N cards (for testing)")
    ap.add_argument("--out", type=Path, default=HOME / "outbox" / "pack.json")
    args = ap.parse_args()
    with collection_copy(args.profile) as col:
        pack = build_pack(col, args.decks)
    if args.limit:
        pack["cards"] = pack["cards"][: args.limit]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(pack), encoding="utf-8")
    print(f"{len(pack['cards'])} cards -> {args.out}; skipped {pack['skipped']}")


if __name__ == "__main__":
    main()
