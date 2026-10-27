"""Apply car grades to Anki, exactly as if the buttons had been pressed in Anki.

Uses only Anki's own Python API, so it runs the same inside Anki and on a copy of a
collection (for testing).

    plan = plan_grades(col, grade_files)      # read-only: what would happen
    report = apply_plan(col, plan)            # apply, as one undo step

Safety rules (see the plan document):
- Each card's grades go through Anki's normal answer function, oldest first, with the time
  they were pressed in the car, so FSRS, learning steps, the load balancer, leeches and
  sibling burying behave exactly as they would in Anki.
- A card is skipped if it was reviewed anywhere else after the car list was made, if it's
  deleted, suspended or buried, or if a review card is no longer due.
- Grades from an earlier Anki day are only applied when the caller allows it, because Anki
  would then measure the next interval from today instead of the day of the drive.
- Applied grades are recorded in the collection config (which syncs), so no computer ever
  applies the same grade twice.
- After each answer, the card's new due date is checked against what Anki said that button
  would do. Any difference is reported.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from anki.collection import Collection
from anki.consts import QUEUE_TYPE_LRN, QUEUE_TYPE_REV, QUEUE_TYPE_DAY_LEARN_RELEARN
from anki.scheduler.v3 import CardAnswer

LEDGER_KEY = "carReviewApplied"
SKIP_TAG = "car-review::skip"
LEDGER_DAYS = 30
RATINGS = {1: CardAnswer.AGAIN, 3: CardAnswer.GOOD}


@dataclass
class CardPlan:
    cid: int
    grades: list[dict]          # this card's car grades, oldest first
    preview: str = ""           # what the first grade would do, in words


@dataclass
class Plan:
    cards: list[CardPlan] = field(default_factory=list)
    skip_tags: list[int] = field(default_factory=list)   # note ids marked "not audio friendly"
    skip_grades: list[dict] = field(default_factory=list)  # the double-Back presses behind skip_tags
    skipped: Counter = field(default_factory=Counter)    # reason -> number of cards
    late_days: set[int] = field(default_factory=set)     # earlier Anki days with grades
    today: int = 0

    @property
    def grade_count(self) -> int:
        return sum(len(c.grades) for c in self.cards)


@dataclass
class Report:
    applied: Counter = field(default_factory=Counter)    # "Good"/"Again" -> count
    cards: int = 0
    tagged: int = 0
    skipped: Counter = field(default_factory=Counter)
    mismatches: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    changes: object = None  # Anki OpChanges for the merged undo step, so Anki's screens refresh


def _grade_key(g: dict) -> str:
    return f"{g['cid']}:{g['at']}"


def _ledger(col: Collection) -> dict:
    return col.get_config(LEDGER_KEY, {}) or {}


def plan_grades(col: Collection, grade_files: list[dict]) -> Plan:
    """Work out what applying these grade files would do, without changing anything."""
    today = col.sched.today
    plan = Plan(today=today)
    ledger = _ledger(col)

    by_card: dict[int, list[dict]] = defaultdict(list)
    for f in grade_files:
        for g in f.get("grades", []):
            if _grade_key(g) in ledger:
                continue  # already applied, on this or another computer
            if g.get("rating") == 0:
                nid = col.db.scalar("select nid from cards where id = ?", g["cid"])
                if nid and nid not in plan.skip_tags:
                    plan.skip_tags.append(nid)
                plan.skip_grades.append(g)
                continue
            if g.get("rating") not in RATINGS:
                plan.skipped["unknown answer"] += 1
                continue
            by_card[g["cid"]].append(g)

    for cid, grades in sorted(by_card.items(), key=lambda kv: min(g["at"] for g in kv[1])):
        grades.sort(key=lambda g: g["at"])
        reason = _card_problem(col, cid, grades, today)
        if reason:
            plan.skipped[reason] += 1
            continue
        for g in grades:
            if g.get("day", today) < today:
                plan.late_days.add(g["day"])
        plan.cards.append(CardPlan(cid, grades, _preview(col, cid, grades[0]["rating"])))
    return plan


def _card_problem(col: Collection, cid: int, grades: list[dict], today: int) -> str | None:
    row = col.db.first("select queue, type, due from cards where id = ?", cid)
    if not row:
        return "card deleted"
    queue, ctype, due = row
    if queue < 0:
        return "suspended or buried"
    made_at_ms = min(int(g.get("list", 0)) for g in grades) * 1000
    ours = {g["at"] for g in grades}
    elsewhere = col.db.list("select id from revlog where cid = ? and id > ?", cid, made_at_ms)
    if any(rid not in ours for rid in elsewhere):
        return "reviewed elsewhere after the car list was made"
    if queue == QUEUE_TYPE_REV and due > max(g.get("day", today) for g in grades):
        return "no longer due"
    return None


def _preview(col: Collection, cid: int, rating: int) -> str:
    states = col._backend.get_scheduling_states(cid)
    state = states.again if rating == 1 else states.good
    secs = _state_secs(state)
    days = _state_days(state)
    label = "Again" if rating == 1 else "Good"
    if secs is not None:
        return f"{label} → back in {max(1, round(secs / 60))} min"
    if days is not None:
        return f"{label} → due in {days} day{'s' if days != 1 else ''}"
    return label


def _normal(state):
    return state.normal if state.WhichOneof("kind") == "normal" else state.filtered.rescheduling.original_state


def _state_secs(state) -> int | None:
    n = _normal(state)
    kind = n.WhichOneof("kind")
    if kind == "learning":
        return n.learning.scheduled_secs
    if kind == "relearning":
        return n.relearning.learning.scheduled_secs
    return None


def _state_days(state) -> int | None:
    n = _normal(state)
    return n.review.scheduled_days if n.WhichOneof("kind") == "review" else None


def apply_plan(col: Collection, plan: Plan, allow_late: bool = False) -> Report:
    """Apply a plan as one undo step ("Apply car grades"). Returns what happened."""
    report = Report()
    if plan.late_days and not allow_late:
        report.skipped["grades from an earlier day (not allowed)"] = len(plan.cards)
        return report

    undo_point = col.add_custom_undo_entry("Apply car grades")
    ledger = _ledger(col)
    today = col.sched.today

    for item in plan.cards:
        try:
            for g in item.grades:
                # Build Anki's study queue, as opening a deck does, so the load balancer is active,
                # and ask Anki what each button does for this card right now.
                col.sched.get_queued_cards(fetch_limit=1)
                states = col._backend.get_scheduling_states(item.cid)
                # Anki only accepts answers for the card at the top of a built queue, and car
                # grades come in any order. A no-change card update makes Anki drop the queue,
                # then the answer is applied with the load-balanced outcome computed above.
                col.update_card(col.get_card(item.cid))
                card = col.get_card(item.cid)
                card.timer_started = time.time()
                answer = col.sched.build_answer(card=card, states=states, rating=RATINGS[g["rating"]])
                answer.answered_at_millis = int(g["at"])
                answer.milliseconds_taken = int(g.get("ms", 0))
                expected_days = _state_days(answer.new_state)
                col.sched.answer_card(answer)
                # Anki keeps only the last 30 undo steps and every answer is one, so fold each
                # answer into "Apply car grades" right away; otherwise a long drive pushes it out.
                col.merge_undo_entries(undo_point)
                ledger[_grade_key(g)] = int(g["at"])
                report.applied["Good" if g["rating"] == 3 else "Again"] += 1
                # Self-check: the card must now be where Anki said that button would put it.
                after = col.get_card(item.cid)
                if expected_days is not None:
                    if after.queue != QUEUE_TYPE_REV or after.due != today + expected_days:
                        report.mismatches.append(
                            f"card {item.cid}: expected due on day {today + expected_days}, got queue {after.queue} due {after.due}")
                elif after.queue not in (QUEUE_TYPE_LRN, QUEUE_TYPE_DAY_LEARN_RELEARN):
                    report.mismatches.append(f"card {item.cid}: expected a learning step, got queue {after.queue}")
            report.cards += 1
        except Exception as err:  # keep going with the other cards
            report.failures.append(f"card {item.cid}: {err}")

    if plan.skip_tags:
        col.tags.bulk_add(plan.skip_tags, SKIP_TAG)
        report.tagged = len(plan.skip_tags)
        for g in plan.skip_grades:
            ledger[_grade_key(g)] = int(g["at"])

    # Forget ledger entries older than LEDGER_DAYS; the relay deletes grades after 3 days anyway.
    cutoff = (time.time() - LEDGER_DAYS * 86400) * 1000
    # Undoable, so undoing "Apply car grades" also forgets that they were applied.
    col.set_config(LEDGER_KEY, {k: v for k, v in ledger.items() if v >= cutoff}, undoable=True)
    report.changes = col.merge_undo_entries(undo_point)
    report.skipped.update(plan.skipped)
    return report
