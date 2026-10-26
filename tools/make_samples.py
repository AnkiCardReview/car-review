"""Make Kokoro voice samples for the test page and time how fast this computer generates speech.

Run with the Car Review Python environment:
    python tools/make_samples.py

Writes docs/samples/<voice>-<n>.mp3 and prints the generation speed for each voice.
"""

from __future__ import annotations

import time
from pathlib import Path

import lameenc
import numpy as np
from kokoro import KPipeline

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "addon"))
from car_review.speech import hint_phrase  # noqa: E402

RATE = 24000
OUT = Path(__file__).resolve().parent.parent / "docs" / "samples"

# Made-up general-knowledge cards (not from any shared deck). "{blank}" is spoken as
# "blank", or as the hint phrased as a question.
CARDS = [
    ("The most common cause of community acquired pneumonia is {blank}.", None,
     "Streptococcus pneumoniae."),
    ("First line treatment for anaphylaxis is {blank}.", "drug",
     "Intramuscular epinephrine."),
    ("In hyperkalemia, the E C G shows peaked T waves. The first step to protect the heart is {blank}.",
     None, "I V calcium gluconate."),
]

# lang_code 'a' = American English, 'b' = British English.
VOICES = {
    "af_heart": "a", "af_bella": "a", "am_michael": "a", "am_fenrir": "a",
    "bf_emma": "b", "bm_george": "b",
}


def silence(secs: float) -> np.ndarray:
    return np.zeros(int(RATE * secs), dtype=np.float32)



def speak(pipeline: KPipeline, voice: str, text: str) -> np.ndarray:
    chunks = [audio.numpy() for _, _, audio in pipeline(text, voice=voice, speed=1.0)]
    return np.concatenate(chunks) if chunks else silence(0)


def card_audio(pipeline: KPipeline, voice: str, question: str, hint: str | None, answer: str) -> np.ndarray:
    # The blank is said in words ("blank", or the hint as a question) and the whole
    # question is spoken in one go, which sounds more natural than separate pieces.
    blank = hint_phrase(hint) if hint else "blank"
    before, after = question.split("{blank}")
    spoken = f"{before.strip()} {blank} {after.strip(' .')}".strip()
    if not spoken.endswith(("?", ".")):
        spoken += "."
    # In the app the answer plays when you press Forward; here it follows a short pause.
    return np.concatenate([speak(pipeline, voice, spoken), silence(1.2), speak(pipeline, voice, answer)])


def to_mp3(audio: np.ndarray, path: Path) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(48)
    encoder.set_in_sample_rate(RATE)
    encoder.set_channels(1)
    encoder.set_quality(2)
    path.write_bytes(encoder.encode(pcm) + encoder.flush())


def main() -> None:
    only = [int(a) for a in sys.argv[1:]]  # optional: card numbers to remake, e.g. "2"
    OUT.mkdir(parents=True, exist_ok=True)
    pipelines: dict[str, KPipeline] = {}
    total_audio = total_time = 0.0
    for voice, lang in VOICES.items():
        pipeline = pipelines.setdefault(lang, KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M"))
        speak(pipeline, voice, "Warm up.")  # load the voice before timing
        started = time.perf_counter()
        audio_secs = 0.0
        for n, card in enumerate(CARDS, start=1):
            if only and n not in only:
                continue
            audio = card_audio(pipeline, voice, *card)
            audio_secs += len(audio) / RATE
            to_mp3(audio, OUT / f"{voice}-{n}.mp3")
        took = time.perf_counter() - started
        total_audio += audio_secs
        total_time += took
        print(f"{voice}: {audio_secs:.1f}s of audio in {took:.1f}s ({audio_secs / took:.1f}x faster than real time)")
    per_card = total_time / (len(VOICES) * len(CARDS))
    print(f"average {per_card:.2f}s to make one card; 200 cards would take about {200 * per_card / 60:.1f} min")


if __name__ == "__main__":
    main()
