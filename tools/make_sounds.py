"""Make the short confirmation sounds played after grading: docs/sounds/good.mp3 and again.mp3.

    python tools/make_sounds.py
"""

from __future__ import annotations

from pathlib import Path

import lameenc
import numpy as np

RATE = 24000
OUT = Path(__file__).resolve().parent.parent / "docs" / "sounds"


def note(freq: float, secs: float, volume: float) -> np.ndarray:
    """A soft bell-like note: a sine plus a quiet octave, with a fast attack and smooth decay."""
    t = np.arange(int(RATE * secs)) / RATE
    tone = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(2 * np.pi * 2 * freq * t)
    envelope = np.minimum(1, t / 0.005) * np.exp(-t / (secs / 3))
    return (volume * tone * envelope).astype(np.float32)


def sequence(notes: list[tuple[float, float]], volume: float) -> np.ndarray:
    return np.concatenate([note(f, s, volume) for f, s in notes])


def to_mp3(audio: np.ndarray, path: Path) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(64)
    encoder.set_in_sample_rate(RATE)
    encoder.set_channels(1)
    encoder.set_quality(2)
    path.write_bytes(encoder.encode(pcm) + encoder.flush())


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # Good: a quick bright rise (E6 -> A6). Again: a softer, lower fall (A4 -> E4).
    to_mp3(sequence([(1318.5, 0.09), (1760.0, 0.16)], 0.35), OUT / "good.mp3")
    to_mp3(sequence([(440.0, 0.11), (329.6, 0.2)], 0.45), OUT / "again.mp3")
    print("wrote", OUT / "good.mp3", "and", OUT / "again.mp3")


if __name__ == "__main__":
    main()
