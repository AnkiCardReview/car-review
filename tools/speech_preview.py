"""Speech preview: show what Car Review would say for your real due cards.

Reads a temporary copy of your Anki collection (never the original) and writes
local/speech-preview.html, which is kept out of git because it contains your card text.

    python tools/speech_preview.py --profile "My Profile" --decks "Deck A" "Deck B"
"""

from __future__ import annotations

import argparse
import collections
import html
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "addon"))
from car_review.speech import CLOZE, hint_phrase, to_speech  # noqa: E402


def read_cards(profile: str, deck_names: list[str], days_ahead: int):
    source = Path(os.environ["APPDATA"]) / "Anki2" / profile / "collection.anki2"
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal"):
            if (source.parent / (source.name + suffix)).exists():
                shutil.copy2(source.parent / (source.name + suffix), Path(tmp) / ("c.anki2" + suffix))
        db = sqlite3.connect(f"file:{Path(tmp) / 'c.anki2'}?mode=ro", uri=True)
        try:
            crt = db.execute("select crt from col").fetchone()[0]
            today = int((time.time() - crt) // 86400)
            wanted = []
            for did, name in db.execute("select id, name from decks"):
                if any(name == d or name.startswith(d + "\x1f") for d in deck_names):
                    wanted.append(did)
            marks = ",".join("?" * len(wanted))
            rows = db.execute(
                f"select c.id, c.ord, c.queue, n.flds, d.name from cards c "
                f"join notes n on n.id = c.nid join decks d on d.id = c.did "
                f"where c.did in ({marks}) and "
                f"((c.queue = 2 and c.due <= ?) or c.queue = 0)",
                (*wanted, today + days_ahead),
            ).fetchall()
        finally:
            db.close()
    return rows


def original_html(front: str, cloze_number: int) -> str:
    """The card's front with this card's blanks shown as highlighted answers."""

    def show(m: re.Match) -> str:
        inner = m.group(2)
        if int(m.group(1)) == cloze_number:
            return f'<mark>{inner}</mark>'
        return inner

    text = CLOZE.sub(show, front)
    return re.sub(r"<img[^>]*>", "[image]", text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, help="Anki profile name")
    ap.add_argument("--decks", nargs="+", required=True, help="deck names (subdecks included)")
    ap.add_argument("--days", type=int, default=13, help="include reviews due up to this many days ahead")
    ap.add_argument("--sample", type=int, default=50)
    args = ap.parse_args()

    rows = read_cards(args.profile, args.decks, args.days)
    results = []
    skips = collections.Counter()
    changes = collections.Counter()
    acronyms = collections.Counter()
    for cid, ord_, queue, flds, deck in rows:
        front = flds.split("\x1f")[0]
        if "{{c" not in front:
            skips["not a cloze card"] += 1
            continue
        spoken = to_speech(front, ord_ + 1)
        if spoken.skip:
            skips[spoken.skip.split(" (")[0]] += 1
        changes.update(spoken.changes)
        said = spoken.question_text() + " " + " ".join(spoken.answers)
        acronyms.update(re.findall(r"\b[A-Z]{2,6}s?\b", said))
        results.append((cid, ord_ + 1, queue, deck.replace("\x1f", "::"), front, spoken))

    random.seed(7)
    sample = random.sample(results, min(args.sample, len(results)))
    kept = sum(1 for r in results if not r[5].skip)
    out = ROOT / "local" / "speech-preview.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(render(results, sample, kept, skips, changes, acronyms), encoding="utf-8")
    print(f"{len(rows)} cards read, {len(results)} cloze cards, {kept} kept for audio "
          f"({100 * kept / max(1, len(results)):.1f}%)")
    print("skipped:", dict(skips))
    print("conversions:", dict(changes))
    print("wrote", out)


def render(results, sample, kept, skips, changes, acronyms) -> str:
    e = html.escape
    rows = []
    for cid, num, queue, deck, front, spoken in sample:
        q = []
        for seg in spoken.question:
            if seg.blank:
                q.append(f'<span class="blank">{e(hint_phrase(seg.hint)) if seg.hint else "blank"}</span>')
            else:
                q.append(e(seg.text))
        answers = "<br>".join(f"{i + 1}. {e(a)}" for i, a in enumerate(spoken.answers))
        status = f'<span class="skip">Skipped: {e(spoken.skip)}</span>' if spoken.skip else ""
        tags = " ".join(f'<span class="tag">{e(c)}</span>' for c in sorted(spoken.changes))
        kind = "new" if queue == 0 else "review"
        rows.append(
            f"<tr><td><div class='meta'>{e(deck)} · c{num} · {kind}</div>{original_html(front, num)}</td>"
            f"<td>{' '.join(q)}<div class='answers'>{answers}</div>{status}<div>{tags}</div></td></tr>"
        )
    skip_list = "".join(f"<li>{e(k)}: {v}</li>" for k, v in skips.most_common())
    change_list = "".join(f"<li>{e(k)}: {v} cards</li>" for k, v in changes.most_common())
    acro = ", ".join(f"{e(k)} ({v})" for k, v in acronyms.most_common(40))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Car Review speech preview</title>
<style>
:root {{ --bg:#fff; --text:#1d1f23; --muted:#667; --line:#e3e5ea; --mark:#fff2a8; --blank:#dbe8ff; --skip:#c0392b; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111317; --text:#e9ebef; --muted:#9aa1ad; --line:#2a2f3a; --mark:#5a4d00; --blank:#1d3768; --skip:#ff7a6b; }} }}
body {{ font: 15px/1.5 -apple-system, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); margin: 24px; }}
table {{ border-collapse: collapse; width: 100%; }} td, th {{ border-top: 1px solid var(--line); padding: 10px; vertical-align: top; width: 50%; text-align: left; }}
mark {{ background: var(--mark); color: inherit; }} .blank {{ background: var(--blank); border-radius: 6px; padding: 0 6px; }}
.meta {{ color: var(--muted); font-size: 12px; }} .answers {{ margin-top: 6px; color: var(--muted); }}
.skip {{ color: var(--skip); font-weight: 600; }} .tag {{ font-size: 11px; border: 1px solid var(--line); border-radius: 10px; padding: 0 6px; color: var(--muted); }}
img {{ max-width: 200px; }}
</style></head><body>
<h1>Car Review speech preview</h1>
<p>{len(results)} cloze cards due in the selected decks · <b>{kept} kept for audio ({100 * kept / max(1, len(results)):.1f}%)</b>.
Left: the card as written (this card's answer highlighted). Right: what Car Review would say; the highlighted word is how the blank is spoken.</p>
<h3>Skipped</h3><ul>{skip_list or "<li>none</li>"}</ul>
<h3>Conversions used</h3><ul>{change_list or "<li>none</li>"}</ul>
<h3>Abbreviations left as letters (check these sound right)</h3><p>{acro}</p>
<h2>{len(sample)} random cards</h2>
<table><tr><th>Card</th><th>Spoken</th></tr>{"".join(rows)}</table>
</body></html>"""


if __name__ == "__main__":
    main()
