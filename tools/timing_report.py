"""Where a drive's time goes, from the clip timings the phone uploads with its grades (app v10+).

    python tools/timing_report.py            # the newest day in ~/CarReview/timing
    python tools/timing_report.py FILE.json  # a timing file, or a grade file from the inbox

For each clip the phone records: how long after the press (or the clip before) the sound
started (lag), how long it played (len), and how long you took to press after it ended (wait).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOME = Path(os.environ.get("CAR_REVIEW_HOME") or Path.home() / "CarReview")


def load(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("timing", []) if isinstance(data, dict) else data


def pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(p * len(values)))]


def line(label: str, values: list[int], unit: str = "ms") -> str:
    if not values:
        return f"  {label:<44} none"
    if unit == "s":
        f = lambda ms: f"{ms / 1000:.1f} s"  # noqa: E731
    else:
        f = lambda ms: f"{ms} ms"  # noqa: E731
    return (f"  {label:<44} median {f(pct(values, .5)):>8}   90% under {f(pct(values, .9)):>8}"
            f"   slowest {f(max(values)):>8}   ({len(values)})")


def report(entries: list[dict]) -> str:
    out = []
    q = [e for e in entries if e["k"] == "q"]
    a = [e for e in entries if e["k"] == "a"]
    ok = lambda e: e.get("lag", -1) >= 0  # noqa: E731
    out.append(f"{len(entries)} clips ({len(q)} questions, {len(a)} answers, "
               f"{len(entries) - len(q) - len(a)} deck cues)\n")
    out.append("Gap from your press to the sound starting (the phone's own delay):")
    out.append(line("question, after ⏭ Good", [e["lag"] for e in q if e.get("via") == "f" and ok(e)]))
    out.append(line("question, after ⏮ Again", [e["lag"] for e in q if e.get("via") == "b" and ok(e)
                                                and e["lag"] < 550]))
    out.append(line("answer, after ⏭ reveal", [e["lag"] for e in a if e.get("via") == "f" and ok(e)]))
    out.append(line("question with no press (Start, deck cue)", [e["lag"] for e in q if not e.get("via") and ok(e)]))
    out.append("\nClip lengths (after the silence is trimmed):")
    out.append(line("question", [e["len"] for e in q if not e.get("cut")], "s"))
    out.append(line("answer", [e["len"] for e in a if not e.get("cut")], "s"))
    out.append("\nYour time after a clip ended, before pressing:")
    out.append(line("after the question (thinking)", [e["wait"] for e in q if e.get("wait", -1) >= 0], "s"))
    out.append(line("after the answer (grading)", [e["wait"] for e in a if e.get("wait", -1) >= 0], "s"))
    cut_q = sum(1 for e in q if e.get("cut"))
    cut_a = sum(1 for e in a if e.get("cut"))
    out.append(f"  pressed before the clip finished: {cut_q} questions, {cut_a} answers")

    listen = sum(e["len"] for e in entries)
    gaps = sum(e["lag"] for e in entries if ok(e) and e["lag"] < 5000)
    waits = sum(e.get("wait", 0) for e in entries if e.get("wait", -1) >= 0 and e["wait"] < 120_000)
    total = listen + gaps + waits
    if total:
        out.append("\nWhere the time went:")
        for label, ms in (("listening to clips", listen), ("you thinking / deciding", waits),
                          ("the phone starting clips", gaps)):
            out.append(f"  {label:<28} {ms / 60000:5.1f} min  {100 * ms / total:4.0f}%")
        cards = len([e for e in a if not e.get("cut") or e.get("len")]) or 1
        out.append(f"  about {total / 1000 / cards:.1f} s per card")
    return "\n".join(out)


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        files = sorted((HOME / "timing").glob("*.json"), key=lambda f: f.stat().st_mtime)
        if not files:
            sys.exit(f"No timing files in {HOME / 'timing'} yet (they arrive with the grades from app v10).")
        path = files[-1]
    print(f"{path.name}\n")
    print(report(load(path)))


if __name__ == "__main__":
    main()
