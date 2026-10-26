"""The Kokoro "Heart" voice, used by the helper to make audio clips.

Two engines give the same voice, so clips made by either are interchangeable (the audio cache
is keyed by the spoken text and voice, not by engine):

- "onnx": kokoro-onnx on ONNX Runtime with misaki's pronunciation rules. This is what voice
  setup installs: about a third of the size of the PyTorch version and no slower on a laptop
  processor. Model files live in CarReview/voice.
- "torch": the original PyTorch `kokoro` package, kept for computers set up by hand before
  voice setup existed.

The engine is picked automatically: ONNX when its files are there, otherwise PyTorch.
"""

from __future__ import annotations

import re

from .common import VOICE_DIR

RATE = 24000
VOICE = "af_heart"
THREADS = 3  # leaves the rest of the processor free while studying
MODEL_FILE = "kokoro-v1.0.fp16.onnx"
VOICES_FILE = "voices-v1.0.bin"


def available_engine() -> str | None:
    """Which engine this Python can run, without loading it."""
    import importlib.util

    if importlib.util.find_spec("kokoro_onnx") and (VOICE_DIR / MODEL_FILE).exists() and (VOICE_DIR / VOICES_FILE).exists():
        return "onnx"
    if importlib.util.find_spec("kokoro"):
        return "torch"
    return None


class Voice:
    """Kokoro Heart, loaded on first use."""

    def __init__(self) -> None:
        self.engine = available_engine()
        self._speak = None

    def _load(self) -> None:
        if self.engine == "onnx":
            self._speak = _onnx_engine()
        elif self.engine == "torch":
            self._speak = _torch_engine()
        else:
            raise RuntimeError("The voice isn't set up on this computer yet (Tools ▸ Car Review ▸ Settings ▸ Voice).")

    def samples(self, text: str):
        """The spoken text as float samples at 24 kHz."""
        import numpy as np

        if self._speak is None:
            self._load()
        chunks = self._speak(text)
        return np.concatenate(chunks) if chunks else np.zeros(RATE // 10, dtype=np.float32)

    def mp3(self, text: str) -> bytes:
        import lameenc
        import numpy as np

        audio = self.samples(text)
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
        enc = lameenc.Encoder()
        enc.set_bit_rate(48)
        enc.set_in_sample_rate(RATE)
        enc.set_channels(1)
        enc.set_quality(2)
        return enc.encode(pcm) + enc.flush()


def _torch_engine():
    import torch
    from kokoro import KPipeline

    torch.set_num_threads(THREADS)
    pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
    return lambda text: [a.numpy() for _, _, a in pipeline(text, voice=VOICE, speed=1.0)]


def _onnx_engine():
    import numpy as np
    import onnxruntime as ort
    from kokoro_onnx import Kokoro
    from misaki import en, espeak

    # The same pronunciation rules (and settings) as the PyTorch pipeline for American English.
    g2p = en.G2P(trf=False, british=False, fallback=espeak.EspeakFallback(british=False), unk="")
    options = ort.SessionOptions()
    options.intra_op_num_threads = THREADS
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(VOICE_DIR / MODEL_FILE), options, providers=["CPUExecutionProvider"])
    model = Kokoro.from_session(session, str(VOICE_DIR / VOICES_FILE))

    def speak(text: str):
        out = []
        # Like the PyTorch pipeline: each line is said on its own, which gives a short pause.
        for line in re.split(r"\n+", text.strip()):
            if not line.strip():
                continue
            phonemes, _ = g2p(line)
            if phonemes:
                audio, _ = model.create(phonemes, voice=VOICE, speed=1.0, is_phonemes=True)
                out.append(np.asarray(audio, dtype=np.float32))
        return out

    return speak
