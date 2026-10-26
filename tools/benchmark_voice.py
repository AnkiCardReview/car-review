"""Time how long the voice takes on your real cards. Nothing is saved.

    python tools/benchmark_voice.py --profile "My Profile" --decks "Deck A" --cards 30 --threads 4
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

import torch
from kokoro import KPipeline

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "addon"))
sys.path.insert(0, str(ROOT / "tools"))
from car_review.speech import hint_phrase, to_speech  # noqa: E402
from speech_preview import read_cards  # noqa: E402

RATE = 24000


def spoken_question(spoken) -> str:
    parts = [(hint_phrase(s.hint) if s.hint else "blank") if s.blank else s.text for s in spoken.question]
    return " ".join(p for p in parts if p)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--decks", nargs="+", required=True)
    ap.add_argument("--cards", type=int, default=30)
    ap.add_argument("--threads", type=int, default=0, help="processor threads for the voice (0 = default)")
    args = ap.parse_args()
    if args.threads:
        torch.set_num_threads(args.threads)

    cards = []
    for _, ord_, _, fields, _ in read_cards(args.profile, args.decks, 13):
        spoken = to_speech(next(iter(fields.values()), ""), ord_ + 1, fields)
        if not spoken.skip:
            cards.append(spoken)
    random.seed(11)
    batch = random.sample(cards, min(args.cards, len(cards)))

    pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
    list(pipeline("Warm up.", voice="af_heart"))
    started = time.perf_counter()
    audio_secs = 0.0
    for spoken in batch:
        for text in [spoken_question(spoken), spoken.answer_text()]:
            for _, _, audio in pipeline(text, voice="af_heart", speed=1.0):
                audio_secs += len(audio) / RATE
    took = time.perf_counter() - started
    per_card = took / len(batch)
    print(f"threads={torch.get_num_threads()}  {len(batch)} cards, {audio_secs:.0f}s of audio in {took:.0f}s")
    print(f"{per_card:.2f}s per card, {audio_secs / len(batch):.1f}s of audio per card, "
          f"{audio_secs / took:.2f}x real time")
    print(f"3000 cards: about {3000 * per_card / 3600:.1f} hours")


if __name__ == "__main__":
    main()
