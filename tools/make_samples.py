"""Make Kokoro voice samples for the test page and time how fast this computer generates speech.

Run with the Car Review Python environment:
    python tools/make_samples.py

Writes, for each voice, docs/samples/<voice>-<card>-q.mp3 (the question),
<voice>-<card>-a.mp3 (all of the card's answers together), and prints the generation speed.
The Good/Again sounds come from tools/make_sounds.py.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import lameenc
import numpy as np
from kokoro import KPipeline

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "addon"))
from car_review.speech import Spoken, hint_phrase  # noqa: E402

RATE = 24000
OUT = Path(__file__).resolve().parent.parent / "docs" / "samples"

# Made-up general-knowledge cards (not from any shared deck): question with one
# "{blank}" per answer, the hint for each blank (or None), and the answers.
CARDS = [
    ("The most common cause of community acquired pneumonia is {blank}.", [None],
     ["Streptococcus pneumoniae."]),
    ("First line treatment for anaphylaxis is {blank}.", ["drug"],
     ["Intramuscular epinephrine."]),
    ("In hyperkalemia, the E C G shows peaked T waves. The first step to protect the heart is {blank}.",
     [None], ["I V calcium gluconate."]),
    ("The two most common causes of acute pancreatitis are {blank} and {blank}.", [None, None],
     ["Gallstones.", "Alcohol."]),
]

# lang_code 'a' = American English, 'b' = British English.
VOICES = {"af_heart": "a"}  # the voice chosen for Car Review


def speak(pipeline: KPipeline, voice: str, text: str) -> np.ndarray:
    chunks = [audio.numpy() for _, _, audio in pipeline(text, voice=voice, speed=1.0)]
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def question_text(question: str, hints: list[str | None]) -> str:
    """The whole question as one sentence, each blank said as "blank" or as its hint question."""
    pieces = question.split("{blank}")
    out = pieces[0]
    for hint, piece in zip(hints, pieces[1:]):
        out += f" {hint_phrase(hint) if hint else 'blank'} {piece}"
    out = " ".join(out.split()).replace(" .", ".")
    return out if out.endswith(("?", ".")) else out + "."


def to_mp3(audio: np.ndarray, path: Path) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(48)
    encoder.set_in_sample_rate(RATE)
    encoder.set_channels(1)
    encoder.set_quality(2)
    path.write_bytes(encoder.encode(pcm) + encoder.flush())


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.mp3"):
        old.unlink()
    pipelines: dict[str, KPipeline] = {}
    total_time = 0.0
    for voice, lang in VOICES.items():
        pipeline = pipelines.setdefault(lang, KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M"))
        speak(pipeline, voice, "Warm up.")  # load the voice before timing
        started = time.perf_counter()
        audio_secs = 0.0
        for n, (question, hints, answers) in enumerate(CARDS, start=1):
            # Blanks with the same cloze number are revealed together, as in Anki.
            clips = {
                "q": speak(pipeline, voice, question_text(question, hints)),
                "a": speak(pipeline, voice, Spoken(answers=answers).answer_text()),
            }
            for name, audio in clips.items():
                audio_secs += len(audio) / RATE
                to_mp3(audio, OUT / f"{voice}-{n}-{name}.mp3")
        took = time.perf_counter() - started
        total_time += took
        print(f"{voice}: {audio_secs:.1f}s of audio in {took:.1f}s ({audio_secs / took:.1f}x real time)")
    (OUT / "cards.json").write_text(json.dumps(len(CARDS)))  # how many sample cards the page cycles through
    per_card = total_time / (len(VOICES) * len(CARDS))
    print(f"average {per_card:.2f}s to make one card; 200 cards would take about {200 * per_card / 60:.1f} min")


if __name__ == "__main__":
    main()
