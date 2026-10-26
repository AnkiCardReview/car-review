"""Build the car list: the cards Anki would give you in the chosen decks, in Anki's order,
turned into spoken text, plus what Again and Good do to each one.

Uses only Anki's own Python API, so it runs the same inside Anki (the add-on) and on a
copy of a collection (tools). It never changes the collection.

    pack = build_pack(col, ["Deck A", "Deck B"])

Each deck ("block") is played in full before the next, in the order given.
- Today's cards come straight from Anki's study queue for that deck, so the order, limits
  and sibling burying are exactly Anki's.
- Learning cards still waiting on their timer are added with the time they're due.
- Tomorrow's reviews are added (labelled with their day), ordered by the deck preset's review
  sort order, so a list made at night works the next morning. The app only plays cards due
  on the current Anki day.
"""

from __future__ import annotations

import hashlib
import math
import random
import time

from anki.collection import Collection
from anki.consts import QUEUE_TYPE_DAY_LEARN_RELEARN, QUEUE_TYPE_LRN, QUEUE_TYPE_NEW, QUEUE_TYPE_REV

from .speech import to_speech

VOICE = "af_heart"
VOICE_VERSION = "kokoro-82m-v1"  # change when the voice or its settings change, to remake audio
PACK_VERSION = 1


def audio_fingerprint(text: str) -> str:
    return hashlib.sha256(f"{VOICE}|{VOICE_VERSION}|{text}".encode()).hexdigest()


def build_pack(col: Collection, deck_names: list[str], days_ahead: int = 1, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    sched = col.sched
    today = sched.today
    prefs = col.get_preferences().scheduling
    previous_deck = col.decks.get_current_id()

    cards: list[dict] = []
    skipped: dict[str, int] = {}
    seen_cards: set[int] = set()
    seen_notes: set[int] = set()
    try:
        for block, name in enumerate(deck_names):
            did = col.decks.id_for_name(name)
            if not did:
                skipped[f"deck not found: {name}"] = 1
                continue
            block_start = len(cards)
            for card, kind, again, good in _block_cards(col, did, today, days_ahead):
                if card.id in seen_cards:
                    continue
                seen_cards.add(card.id)
                entry = _card_entry(col, card, kind, again, good, block, name, today, skipped)
                if entry is None:
                    continue
                # One card per note per day, as Anki's sibling burying does.
                day_key = (card.nid, entry["due_day"])
                if kind == "review" and day_key in seen_notes:
                    skipped["sibling already in list"] = skipped.get("sibling already in list", 0) + 1
                    continue
                seen_notes.add(day_key)
                cards.append(entry)
            cards[block_start:] = _order_block(cards[block_start:], col.decks.config_dict_for_deck_id(did))
    finally:
        col.decks.select(previous_deck)

    return {
        "version": PACK_VERSION,
        "made_at": int(now),
        "anki_today": today,
        "next_day_at": sched.day_cutoff,  # when Anki's day after `anki_today` begins
        "learn_ahead_secs": prefs.learn_ahead_secs,
        "voice": VOICE,
        "blocks": deck_names,
        "cards": cards,
        "skipped": skipped,
    }


def _block_cards(col: Collection, did: int, today: int, days_ahead: int):
    """Yield (card, kind, again_secs, good_secs) for one deck block, in play order."""
    col.decks.select(did)
    queued = col.sched.get_queued_cards(fetch_limit=5000, intraday_learning_only=False)
    for qc in queued.cards:
        card = col.get_card(qc.card.id)
        yield card, _kind(card), _secs(qc.states.again), _secs(qc.states.good)

    # Learning cards whose timer hasn't run out yet aren't in the queue; add them with their due time.
    deck_ids = [d for d in col.decks.deck_and_child_ids(did)]
    ids = ",".join(str(d) for d in deck_ids)
    waiting = col.db.list(
        f"select id from cards where did in ({ids}) and "
        f"((queue = {QUEUE_TYPE_LRN} and due < ?) or (queue = {QUEUE_TYPE_DAY_LEARN_RELEARN} and due <= ?)) order by due",
        col.sched.day_cutoff, today,
    )
    for cid in waiting:
        card = col.get_card(cid)
        states = col._backend.get_scheduling_states(cid)
        yield card, _kind(card), _secs(states.again), _secs(states.good)

    # Tomorrow's (and later) reviews, ordered like the deck preset's review sort order.
    if days_ahead > 0:
        later = col.db.list(
            f"select id from cards where did in ({ids}) and queue = {QUEUE_TYPE_REV} and due > ? and due <= ?",
            today, today + days_ahead,
        )
        conf = col.decks.config_dict_for_deck_id(did)
        for cid in _sort_reviews(col, later, conf, today + 1):
            card = col.get_card(cid)
            states = col._backend.get_scheduling_states(cid)
            yield card, "review", _secs(states.again), _secs(states.good)


def _order_block(entries: list[dict], conf: dict) -> list[dict]:
    """Learning cards first, then reviews and new cards per the deck's New/review order.

    Anki's own queue already orders today's cards; this keeps that order within each group and
    places tomorrow's reviews (added after the queue) with the other reviews.
    """
    new_mix = conf.get("newMix", 0)  # 0 mix with reviews, 1 after reviews, 2 before reviews
    new_rank = {0: 1, 1: 2, 2: 0.5}.get(new_mix, 1)
    rank = {"learn": 0, "review": 1, "new": new_rank}
    return sorted(entries, key=lambda e: rank[e["kind"]])  # sorted() is stable


def _kind(card) -> str:
    if card.queue == QUEUE_TYPE_NEW:
        return "new"
    if card.queue in (QUEUE_TYPE_LRN, QUEUE_TYPE_DAY_LEARN_RELEARN):
        return "learn"
    return "review"


def _secs(state) -> int | None:
    """Seconds until the card comes back after this answer, or None if it's done for today."""
    normal = state.normal if state.WhichOneof("kind") == "normal" else state.filtered.rescheduling.original_state
    which = normal.WhichOneof("kind")
    if which == "learning":
        return normal.learning.scheduled_secs
    if which == "relearning":
        return normal.relearning.learning.scheduled_secs
    return None


def _retrievability(card, on_day: int) -> float:
    """FSRS recall probability on a given day (1.0 if the card has no FSRS memory state)."""
    mem = card.memory_state
    if not mem or not mem.stability:
        return 1.0
    decay = card.decay or 0.5
    factor = 0.9 ** (-1 / decay) - 1
    last_day = card.due - card.ivl
    elapsed = max(0, on_day - last_day)
    return math.pow(1 + factor * elapsed / mem.stability, -decay)


def _sort_reviews(col: Collection, cids: list[int], conf: dict, on_day: int) -> list[int]:
    cards = [col.get_card(c) for c in cids]
    order = conf.get("reviewOrder", 0)
    if order == 11:  # retrievability descending
        cards.sort(key=lambda c: -_retrievability(c, on_day))
    elif order == 7:  # retrievability ascending
        cards.sort(key=lambda c: _retrievability(c, on_day))
    elif order == 3:  # intervals ascending
        cards.sort(key=lambda c: c.ivl)
    elif order == 4:  # intervals descending
        cards.sort(key=lambda c: -c.ivl)
    elif order == 8:  # random
        random.Random(on_day).shuffle(cards)
    else:  # due date (and anything else): oldest due first
        cards.sort(key=lambda c: (c.due, c.id))
    return [c.id for c in cards]


def _card_entry(col, card, kind, again, good, block, deck_name, today, skipped) -> dict | None:
    note = card.note()
    notetype = note.note_type()
    if notetype["type"] != 1:  # MODEL_CLOZE
        skipped["not a cloze card"] = skipped.get("not a cloze card", 0) + 1
        return None
    fields = dict(zip([f["name"] for f in notetype["flds"]], note.fields))
    front = note.fields[0]
    spoken = to_speech(front, card.ord + 1, fields)
    if spoken.skip:
        reason = spoken.skip.split(" (")[0]
        skipped[reason] = skipped.get(reason, 0) + 1
        return None
    conf = col.decks.config_dict_for_deck_id(card.odid or card.did)
    question = spoken.spoken_question()
    answer = spoken.answer_text()
    entry = {
        "cid": card.id,
        "nid": card.nid,
        "block": block,
        "deck": deck_name,
        "kind": kind,
        "due_day": card.due if card.queue == QUEUE_TYPE_REV else today,
        "due_at": card.due if card.queue == QUEUE_TYPE_LRN else None,  # epoch secs for intraday learning
        "again_secs": again,
        "good_secs": good,
        "learn_steps": [int(m * 60) for m in conf["new"]["delays"]],
        "relearn_steps": [int(m * 60) for m in conf["lapse"]["delays"]],
        "remaining_steps": card.left % 1000 if kind == "learn" else None,
        "is_relearning": card.type == 3,
        "question": question,
        "answer": answer,
        "q_audio": audio_fingerprint(question),
        "a_audio": audio_fingerprint(answer),
    }
    return entry
