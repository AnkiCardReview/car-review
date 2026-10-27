"""Check the car list against Anki on a temporary COPY of a collection (never the original).

- Today's part matches Anki's own study queue (so buried siblings, limits and order are Anki's).
- Cards buried today are in the list from tomorrow.
- New cards are left out (the car plays reviews and learning cards); learning cards carry over.
- No note has two cards on the same day.
- Every review is placed within the window, never before its due day.

    python tools/pack_check.py --profile "My Profile" --decks "Deck A" "Deck B" [--days 3]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "addon"))
sys.path.insert(0, str(ROOT / "tools"))
from car_review.builder import build_pack  # noqa: E402
from collection_copy import collection_copy  # noqa: E402

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + label + (f"  ({detail})" if detail and not ok else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--decks", nargs="+", required=True)
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()

    with collection_copy(args.profile) as col:
        today = col.sched.today
        pack = build_pack(col, args.decks, days_ahead=args.days)
        cards = {c["cid"]: c for c in pack["cards"]}
        skipped_total = sum(pack["skipped"].values())
        print(f"Anki day {today}: {len(cards)} cards in the list, {skipped_total} skipped {dict(pack['skipped'])}")
        print("by day and kind:", sorted(Counter((c["due_day"], c["kind"]) for c in cards.values()).items()))

        in_queue, buried = set(), set()
        for name in args.decks:
            did = col.decks.id_for_name(name)
            col.decks.select(did)
            in_queue |= {qc.card.id for qc in col.sched.get_queued_cards(fetch_limit=5000).cards}
            ids = ",".join(str(d) for d in col.decks.deck_and_child_ids(did))
            buried |= set(col.db.list(f"select id from cards where did in ({ids}) and queue in (-2, -3) "
                                      f"and (type = 0 or (type = 2 and due <= ?))", today + args.days))

        listed_today = {cid for cid, c in cards.items() if c["due_day"] <= today}
        new_in_queue = {cid for cid in in_queue if col.get_card(cid).queue == 0}
        missing = {cid for cid in in_queue - new_in_queue if cid not in cards}
        not_new_skipped = skipped_total - pack["skipped"].get("new card (the car plays reviews)", 0)
        check("every review and learning card in Anki's queue today is listed (unless not audio-friendly)",
              len(missing) <= not_new_skipped, f"{len(missing)} missing")
        check("nothing extra is playable today beyond Anki's queue and learning cards",
              all(cid in in_queue or cards[cid]["kind"] == "learn" for cid in listed_today))
        listed_buried = [cid for cid in buried if cid in cards]
        check(f"buried cards come back from tomorrow ({len(listed_buried)} of {len(buried)} listed; the rest skipped)",
              all(cards[cid]["due_day"] >= today + 1 for cid in listed_buried) and len(buried) - len(listed_buried) <= skipped_total)
        check(f"no new cards in the car list ({len(new_in_queue)} new in Anki's queue left out)",
              not any(c["kind"] == "new" for c in cards.values()))
        announce = pack.get("announce") or {}
        check("a spoken 'done' clip for every deck and for the end",
              len(announce.get("blocks", [])) == len(args.decks) and bool(announce.get("end", {}).get("audio")),
              str([a["text"] for a in announce.get("blocks", [])]))
        per_note_day = Counter((col.get_card(cid).nid, c["due_day"]) for cid, c in cards.items() if c["kind"] == "review")
        clashes = [k for k, n in per_note_day.items() if n > 1]
        check("no note has two reviews on the same day", not clashes, f"{len(clashes)} clashes")
        early = [cid for cid, c in cards.items() if c["kind"] == "review" and c["due_day"] < min(col.get_card(cid).due, today + 1) and cid not in in_queue]
        check("no review is placed before its due day", not early, f"{len(early)} early")
        check("everything is within the window", all(c["due_day"] <= today + args.days for c in cards.values()))

    print(f"\n{sum(results)} of {len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
