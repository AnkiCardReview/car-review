"""Check grade applying on a temporary COPY of a collection (the original is never opened).

Makes up car grades for real due cards and checks every safety rule:
Good, Again, Again-then-Good (relearning in the car), a new card, a "doesn't work as audio"
skip, a card reviewed elsewhere after the list was made, a grade from an earlier day, the
ledger (no double apply), the self-check, and that one Undo reverts everything.

    python tools/apply_check.py --profile "My Profile" --decks "Deck A" "Deck B"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "addon"))
sys.path.insert(0, str(ROOT / "tools"))
import anki.collection  # noqa: E402,F401  (must load before anki.scheduler)
from anki.scheduler.v3 import CardAnswer  # noqa: E402
from car_review.apply import SKIP_TAG, apply_plan, plan_grades  # noqa: E402
from car_review.builder import build_pack  # noqa: E402
from collection_copy import collection_copy  # noqa: E402

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + label + (f"  ({detail})" if detail and not ok else ""))


def snapshot(col, cid):
    c = col.get_card(cid)
    return (c.queue, c.type, c.due, c.ivl, c.reps, c.lapses, str(c.memory_state))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--decks", nargs="+", required=True)
    args = ap.parse_args()

    with collection_copy(args.profile) as col:
        today = col.sched.today
        pack = build_pack(col, args.decks, include_new=True)
        due_today = [c for c in pack["cards"] if c["kind"] == "review" and c["due_day"] <= today]
        new = [c for c in pack["cards"] if c["kind"] == "new"]
        if len(due_today) < 6 or len(new) < 1:
            print(f"not enough cards to test (reviews due today: {len(due_today)}, new: {len(new)})")
            sys.exit(2)
        good, again_good, again, elsewhere, skipme, late = (c["cid"] for c in due_today[:6])
        newc = new[0]["cid"]
        # Pretend the list was made two hours ago and the drive was 90 minutes ago.
        made = int(time.time()) - 7200
        t0 = (made + 1800) * 1000

        def g(cid, rating, minutes, day=today):
            return {"cid": cid, "rating": rating, "at": t0 + minutes * 60_000 + cid % 1000, "ms": 6000, "day": day, "list": made}

        drive = {"grades": [
            g(good, 3, 0),
            g(again_good, 1, 1), g(again_good, 3, 17),   # missed, came back 16 min later, Good
            g(again, 1, 2),                               # missed, never came back before parking
            g(newc, 3, 3),
            g(skipme, 0, 4),                              # double-Back: not audio friendly
            g(elsewhere, 3, 5),
        ]}

        before = {cid: snapshot(col, cid) for cid in (good, again_good, again, newc, elsewhere, skipme)}
        # What Anki's Good button would do, asked the same way the reviewer does: with the study
        # queue built, so the load balancer is active. (Then a no-change update drops the queue.)
        col.sched.get_queued_cards(fetch_limit=1)
        expected_good = col._backend.get_scheduling_states(good).good.normal.review.scheduled_days
        col.update_card(col.get_card(good))

        # Someone reviews `elsewhere` on their phone after the list was made.
        card = col.get_card(elsewhere)
        card.timer_started = time.time()
        states = col._backend.get_scheduling_states(elsewhere)
        col.sched.answer_card(col.sched.build_answer(card=card, states=states, rating=CardAnswer.GOOD))
        after_elsewhere = snapshot(col, elsewhere)
        revlog_before = col.db.scalar("select count() from revlog")

        plan = plan_grades(col, [drive])
        planned = {c.cid for c in plan.cards}
        check("plan: Good, Again+Good, Again and the new card are planned", planned == {good, again_good, again, newc}, str(planned))
        check("plan: card reviewed elsewhere is skipped", plan.skipped.get("reviewed elsewhere after the car list was made") == 1, str(plan.skipped))
        check("plan: double-Back card is marked for the skip tag", plan.skip_tags == [col.get_card(skipme).nid])
        check("plan: changes nothing by itself", col.db.scalar("select count() from revlog") == revlog_before)
        print("   previews:", [c.preview for c in plan.cards])

        report = apply_plan(col, plan)
        check("apply: 5 grades applied (3 Good, 2 Again)", report.applied == {"Good": 3, "Again": 2}, str(report.applied))
        check("apply: no self-check mismatches", not report.mismatches, "; ".join(report.mismatches))
        check("apply: no failures", not report.failures, "; ".join(report.failures))

        times = {r[0]: r for r in col.db.all("select id, cid, ease, type, time from revlog where id >= ?", t0)}
        for grade in drive["grades"]:
            if grade["rating"] in (1, 3) and grade["cid"] != elsewhere:
                row = times.get(grade["at"])
                check(f"history: card {grade['cid']} logged at the car time with the right button",
                      bool(row) and row[1] == grade["cid"] and row[2] == grade["rating"] and row[4] == 6000, str(row))

        c = col.get_card(good)
        check("Good card: due exactly where Anki's Good button said", c.queue == 2 and c.due == today + expected_good,
              f"due {c.due} vs {today + expected_good}")
        c = col.get_card(again_good)
        check("Again then Good: graduated back to review after its relearning step", c.queue == 2 and c.lapses == before[again_good][5] + 1)
        c = col.get_card(again)
        check("Again only: in relearning, due about 15 min after applying", c.queue == 1 and 0 < c.due - time.time() <= 20 * 60,
              f"queue {c.queue}, due in {c.due - time.time():.0f}s")
        c = col.get_card(newc)
        check("new card: no longer new", c.queue != 0)
        check("FSRS memory state updated", str(col.get_card(good).memory_state) != before[good][6])
        check("card reviewed elsewhere untouched by the car grade", snapshot(col, elsewhere) == after_elsewhere)
        check("skip tag added", col.get_note(col.get_card(skipme).nid).has_tag(SKIP_TAG))
        check("skipped card's scheduling unchanged", snapshot(col, skipme) == before[skipme])

        again_plan = plan_grades(col, [drive])
        check("ledger: applying the same grades again does nothing", not again_plan.cards and not again_plan.skip_tags,
              f"{len(again_plan.cards)} cards")

        car_ids = [x["at"] for x in drive["grades"] if x["rating"] in (1, 3) and x["cid"] != elsewhere]
        col.undo()
        check("one Undo restores every card", all(snapshot(col, cid) == before[cid] for cid in (good, again_good, again, newc, skipme)))
        left = col.db.scalar(f"select count() from revlog where id in ({','.join(map(str, car_ids))})")
        check("one Undo removes the car history", left == 0, f"{left} car reviews left")
        check("one Undo forgets the applied-grades record", plan_grades(col, [drive]).cards != [])

        # A long drive: far more answers than Anki's 30 undo steps, still one Undo.
        many = [c["cid"] for c in due_today[6:46]]
        if len(many) >= 20:
            long_drive = {"grades": [g(cid, 3 if i % 4 else 1, 30 + i) for i, cid in enumerate(many)]}
            before_long = {cid: snapshot(col, cid) for cid in many}
            long_report = apply_plan(col, plan_grades(col, [long_drive]))
            check(f"long drive: all {len(many)} answers applied without errors",
                  sum(long_report.applied.values()) == len(many) and not long_report.failures, str(long_report.failures[:2]))
            col.undo()
            check("long drive: one Undo restores every card", all(snapshot(col, cid) == before_long[cid] for cid in many))
        else:
            print(f"(long-drive check skipped: only {len(many)} more reviews due today)")

        # Picked up mid-drive: the car Again is applied first, the Good after it arrives later.
        if len(due_today) >= 48:
            split = due_today[47]["cid"]
            first = {"grades": [g(split, 1, 60)]}
            apply_plan(col, plan_grades(col, [first]))
            both = {"grades": first["grades"] + [g(split, 3, 76)]}
            second = plan_grades(col, [both])
            check("mid-drive pickup: the later Good still applies after the Again was applied",
                  [c.cid for c in second.cards] == [split] and len(second.cards[0].grades) == 1, f"{second.skipped}")
        else:
            print("(mid-drive check skipped: not enough reviews due today)")

        # A realistic late case: a new card learned in the car yesterday, applied today.
        late = new[1]["cid"] if len(new) > 1 else late
        late_plan = plan_grades(col, [{"grades": [g(late, 3, 0, day=today - 1)]}])
        late_report = apply_plan(col, late_plan)
        check("grades from yesterday are held back unless allowed", late_plan.late_days == {today - 1} and not late_report.applied,
              f"late days {late_plan.late_days}, skipped {late_plan.skipped}, applied {late_report.applied}")

    print(f"\n{sum(results)} of {len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
