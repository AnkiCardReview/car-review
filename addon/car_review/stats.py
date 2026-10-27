"""Car stats: how your drives went (Tools ▸ Car Review ▸ Car stats).

Car answers are found through the applied-grades record (apply.LEDGER_KEY): each one is saved
in Anki's review log with the exact time it was pressed in the car, and a review's id is that
time. The record keeps 30 days, so these stats cover the last 30 days.

The clip timings (how long each clip took to start, how long it played, how long you took to
press) come from the phone with its grades and are kept in CarReview/timing.

    html = report_html(col, decks)      # the whole window
    lines = timing_lines(entries)       # the timing part, also used by tools/timing_report.py
"""

from __future__ import annotations

import html
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .apply import LEDGER_KEY
from .common import HOME

DRIVE_GAP_MS = 45 * 60 * 1000  # car answers further apart than this belong to different drives
MATURE_DAYS = 21
TIMING_DIR = HOME / "timing"


@dataclass
class Tally:
    """Reviews (Anki's "review" type, the ones retention is about) and all answers."""
    reviews: int = 0
    passed: int = 0
    mature: int = 0
    mature_passed: int = 0
    answers: int = 0
    again: int = 0
    cards: set = field(default_factory=set)

    def add(self, cid: int, ease: int, kind: int, last_ivl: int) -> None:
        self.answers += 1
        self.cards.add(cid)
        if ease == 1:
            self.again += 1
        if kind == 1:  # a review (not learning, relearning or a filtered-deck preview)
            self.reviews += 1
            self.passed += ease > 1
            if last_ivl >= MATURE_DAYS:
                self.mature += 1
                self.mature_passed += ease > 1


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "–"


def _rows(col, decks: list[str], since_ms: int) -> list[tuple]:
    """(id, cid, ease, type, lastIvl, top-level deck) for answers in the car decks since `since_ms`."""
    names = {}
    for d in col.decks.all_names_and_ids():
        top = next((c for c in decks if d.name == c or d.name.startswith(c + "::")), None)
        if top:
            names[d.id] = top
    if not names:
        return []
    out = []
    for rid, cid, ease, kind, last_ivl, did, odid in col.db.all(
            "select r.id, r.cid, r.ease, r.type, r.lastIvl, c.did, c.odid from revlog r "
            "join cards c on c.id = r.cid where r.id >= ? and r.ease > 0 and r.type < 4", since_ms):
        top = names.get(odid or did)
        if top:
            out.append((rid, cid, ease, kind, last_ivl, top))
    return out


def drives(rows: list[tuple], car_ids: set[int]) -> list[list[tuple]]:
    """Car answers grouped into drives, oldest first."""
    groups: list[list[tuple]] = []
    for row in sorted(r for r in rows if r[0] in car_ids):
        if groups and row[0] - groups[-1][-1][0] <= DRIVE_GAP_MS:
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def latest_timing() -> tuple[str, list[dict]]:
    files = sorted(TIMING_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime) if TIMING_DIR.exists() else []
    if not files:
        return "", []
    try:
        return files[-1].stem, json.loads(files[-1].read_text(encoding="utf-8"))
    except ValueError:
        return "", []


def _p(values: list[int], q: float) -> int:
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0


def timing_lines(entries: list[dict]) -> list[tuple[str, str]]:
    """(label, value) lines about where a drive's time went, from the phone's clip timings."""
    if not entries:
        return []
    q = [e for e in entries if e["k"] == "q"]
    a = [e for e in entries if e["k"] == "a"]
    started = lambda e: e.get("lag", -1) >= 0  # noqa: E731

    def ms(values: list[int]) -> str:
        return f"{_p(values, .5)} ms (90% under {_p(values, .9)} ms)" if values else "–"

    def secs(values: list[int]) -> str:
        return f"{_p(values, .5) / 1000:.1f} s (90% under {_p(values, .9) / 1000:.1f} s)" if values else "–"

    out = [
        ("Sound starts after ⏭ Good", ms([e["lag"] for e in q if e.get("via") == "f" and started(e)])),
        ("Sound starts after ⏮ Again", ms([e["lag"] for e in q if e.get("via") == "b" and started(e) and e["lag"] < 550])),
        ("Answer starts after ⏭", ms([e["lag"] for e in a if e.get("via") == "f" and started(e)])),
        ("Question length", secs([e["len"] for e in q if not e.get("cut")])),
        ("Answer length", secs([e["len"] for e in a if not e.get("cut")])),
        ("Your thinking time after a question", secs([e["wait"] for e in q if e.get("wait", -1) >= 0 and not e.get("auto")])),
        ("Your grading time after an answer", secs([e["wait"] for e in a if e.get("wait", -1) >= 0])),
    ]
    auto = sum(1 for e in a if e.get("via") == "t")
    if auto:
        out.append(("Answers played by themselves", f"{auto} of {len(a)}"))
    cut = sum(1 for e in q + a if e.get("cut"))
    out.append(("Pressed before a clip finished", f"{cut} times"))
    listen = sum(e["len"] for e in entries)
    gaps = sum(e["lag"] for e in entries if started(e) and e["lag"] < 5000)
    waits = sum(e["wait"] for e in entries if e.get("wait", -1) >= 0 and e["wait"] < 120_000)
    total = listen + gaps + waits
    if total:
        out.append(("Time listening / thinking / phone delay",
                    f"{_pct(listen, total)} / {_pct(waits, total)} / {_pct(gaps, total)}"))
    return out


def report_html(col, decks: list[str], now_ms: int | None = None) -> str:
    now_ms = now_ms or int(time.time() * 1000)
    since = now_ms - 30 * 86400 * 1000
    car_ids = {int(v) for v in (col.get_config(LEDGER_KEY, {}) or {}).values()}
    rows = _rows(col, decks, since)
    esc = html.escape
    parts = ["<style>table{border-collapse:collapse;margin:4px 0 14px}td,th{padding:3px 10px;text-align:right}"
             "th{border-bottom:1px solid #888}td:first-child,th:first-child{text-align:left}"
             "h3{margin:14px 0 4px}p{margin:4px 0}</style>"]

    groups = drives(rows, car_ids)
    if not groups:
        parts.append("<p>No car answers in Anki yet. After a drive, open Anki: your car answers are applied "
                     "and show up here.</p>")
        return "".join(parts)

    # Recent drives
    parts.append("<h3>Your drives</h3><table><tr><th>Drive</th><th>Time</th><th>Answers</th><th>Cards</th>"
                 "<th>Reviews right</th><th>Mature right</th><th>Per answer</th></tr>")
    for g in reversed(groups[-10:]):
        t = Tally()
        for rid, cid, ease, kind, last_ivl, _ in g:
            t.add(cid, ease, kind, last_ivl)
        span = (g[-1][0] - g[0][0]) / 1000
        start = time.strftime("%a %d %b %H:%M", time.localtime(g[0][0] / 1000))
        per = f"{span / max(1, t.answers - 1):.0f} s" if t.answers > 1 else "–"
        parts.append(f"<tr><td>{esc(start)}</td><td>{span / 60:.0f} min</td><td>{t.answers}</td><td>{len(t.cards)}</td>"
                     f"<td>{_pct(t.passed, t.reviews)} ({t.passed}/{t.reviews})</td>"
                     f"<td>{_pct(t.mature_passed, t.mature)} ({t.mature_passed}/{t.mature})</td><td>{per}</td></tr>")
    parts.append("</table>")

    # Car vs Anki, per deck
    for title, days in (("Last 7 days", 7), ("Last 30 days", 30)):
        cutoff = now_ms - days * 86400 * 1000
        parts.append(f"<h3>{title}: car or Anki</h3><table><tr><th>Deck</th><th>Where</th><th>Reviews</th>"
                     "<th>Right</th><th>Mature right</th><th>Young right</th></tr>")
        for deck in decks:
            for where, in_car in (("Car", True), ("Anki", False)):
                t = Tally()
                for rid, cid, ease, kind, last_ivl, top in rows:
                    if top == deck and rid >= cutoff and (rid in car_ids) == in_car:
                        t.add(cid, ease, kind, last_ivl)
                young, young_ok = t.reviews - t.mature, t.passed - t.mature_passed
                parts.append(f"<tr><td>{esc(deck)}</td><td>{where}</td><td>{t.reviews}</td>"
                             f"<td>{_pct(t.passed, t.reviews)}</td><td>{_pct(t.mature_passed, t.mature)} ({t.mature})</td>"
                             f"<td>{_pct(young_ok, young)} ({young})</td></tr>")
        parts.append("</table>")
    parts.append("<p>“Right” counts reviews answered Good (or Hard/Easy in Anki) out of all reviews; learning cards "
                 "aren't included. Mature = interval of 21 days or more. Car answers older than 30 days count as Anki.</p>")

    # Where the time went (from the phone's clip timings)
    name, entries = latest_timing()
    lines = timing_lines(entries)
    if lines:
        parts.append("<h3>Where the time went (latest drive day)</h3><table>")
        for label, value in lines:
            parts.append(f"<tr><td>{esc(label)}</td><td>{esc(value)}</td></tr>")
        parts.append("</table><p>Medians. Phone delay is the time from your press to the sound starting.</p>")
    return "".join(parts)
