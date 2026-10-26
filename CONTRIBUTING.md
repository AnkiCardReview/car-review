# For developers and contributors

Thanks for helping. Car Review is small on purpose: plain Python in Anki, one static web page, and a tiny relay. The guiding rules are **simple**, **automatic**, **never mess up scheduling**, **private**, and **free to run**.

## How the pieces fit

```
addon/car_review/      Anki add-on (runs inside Anki), plus the helper it starts
  __init__.py          loads anki_addon only inside Anki, so the helper/tools can import the package
  anki_addon.py        menu, hooks: send on close, check/apply grades on open
  settings.py          the setup wizard / settings window (decks, options, voice, phone QR)
  voice_setup.py       one-click voice install: uv -> Python 3.12 -> pinned packages -> model
  voice-requirements.txt  hash-pinned packages for the helper (generated, see below)
  builder.py           builds the car list from Anki's own queue (read-only)
  speech.py            card text -> spoken text (pure Python)
  apply.py             applies car grades through Anki's scheduler, with all the safety checks
  common.py            paths (CAR_REVIEW_HOME), relay/app URLs, pairing file
  pairing.py           pairing secret, HKDF key derivation (stdlib), AES-GCM (cryptography)
  helper.py            background process in the helper's own Python: voice, encrypt, upload
  voice.py             Kokoro "Heart": ONNX engine (default) or PyTorch engine (old setups)
  ui.py                the helper's small Tk progress window
  power.py             plugged in or on battery (Windows, macOS, Linux)
  vendor/segno         QR codes (BSD-3), so pairing works before the voice is installed
docs/                  the phone app (GitHub Pages): index.html + app.js; test pages
relay/                 Cloudflare Worker + D1 "mailbox" (encrypted bytes only)
tools/                 checks and developer scripts (below)
help/                  user guides
```

**Two Pythons.** Code in `anki_addon.py`, `settings.py`, `builder.py`, `apply.py`, `speech.py` and `voice_setup.py` runs in **Anki's** Python and may use only the standard library, `aqt`/`anki` and `requests`. `helper.py` and `voice.py` run in the **helper's** Python (`~/CarReview/venv`), which has the voice engine and `cryptography`; Anki starts it as `python -m <addon folder>.helper ...` so it keeps running after Anki closes. The folder is `car_review` from GitHub but a number when installed from AnkiWeb, so never hard-code the package name.

**Data flow.** Closing Anki builds the car list (`builder.py`) and writes `~/CarReview/outbox/pack.json`; the helper voices missing clips (cached by text fingerprint in `audio-cache/`), encrypts everything and uploads it to the relay. The phone downloads and decrypts with WebCrypto, plays the cards and uploads encrypted grades. Opening Anki runs `helper fetch-grades`, then `apply.plan_grades` (read-only) and `apply.apply_plan` as one undoable operation.

## Running things locally
Developer tools use a Python 3.12 environment with `anki` (and, for the wizard check, `aqt[qt6]`), plus the helper's packages:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv "aqt[qt6]==26.9.3" -r addon/car_review/voice-requirements.txt --no-deps
```

| Check | What it proves |
|---|---|
| `python tools/wizard_check.py` | The setup wizard: decks, order, options, QR, pairing reset, saving. Runs off-screen with a throwaway collection. `--shots help/images` refreshes the screenshots |
| `python tools/apply_check.py --profile "P" --decks "A" "B"` | Every grade-applying safety rule, on a temporary **copy** of a collection (25 checks) |
| `python tools/relay_check.py` | The live relay: encryption, write keys, size limits (throwaway box) |
| `python tools/speech_preview.py --profile "P" --decks "A"` | What the car would say for real cards. Writes `local/`, which git ignores |
| `python tools/make_pack.py ...` | Builds a car list from a collection copy, for testing the helper |

`collection_copy.py` reads profiles from `%APPDATA%\Anki2` (Windows); adjust the path on other systems. To test the add-on without touching your own setup, set `CAR_REVIEW_HOME` to a scratch folder: the voice, pairing and audio all go there.

To try the phone app, serve `docs/` on `http://localhost:8765` (the relay allows that origin). **Mute the browser first**; the app plays audio.

## The voice
- `voice_setup.py` downloads **uv** (pinned version and SHA-256 per platform), makes `~/CarReview/venv` with a uv-managed **Python 3.12**, installs `voice-requirements.txt` with `--require-hashes --no-deps --only-binary :all:`, downloads the **Kokoro-82M v1.0 fp16 ONNX** model and voices (pinned SHA-256), and runs `helper voice-check`.
- **Pronunciation** comes from misaki with the same settings as the PyTorch `kokoro` package, so clips are interchangeable between engines (checked: identical phonemes on 40 real cards). The cache key is the spoken text and voice, not the engine.
- **Speed** (Intel i5-1135G7, 3 threads): fp32, fp16 and int8 ONNX all ran at about 0.8–0.9× real time, similar to PyTorch. fp16 was chosen for size (177 MB) with near-full quality.
- **Changing packages:** edit `tools/voice/requirements.in`, then `python tools/voice/lock.py --uv path/to/uv`, and run voice setup into a scratch `CAR_REVIEW_HOME` to test.

### Planned: faster voice on Apple Silicon Macs
Kokoro reaches 15–80× real time on M-series Macs with Apple's Neural Engine/GPU, versus a few times real time on their CPU. Plan:
1. Add a third engine to `voice.py`, picked automatically on `darwin/arm64` when installed: either the **MLX** port (`mlx-audio`, Kokoro-82M) or a **Core ML** conversion of Kokoro (e.g. the FluidAudio/kokoro-coreml models).
2. Feed it misaki phonemes, as the ONNX engine does, so clips stay identical in pronunciation and the cache stays valid.
3. In `voice_setup.py`, install the extra package and model only on Apple Silicon, and fall back to the ONNX engine if its `voice-check` fails.
4. Measure on a real M-series Mac before making it the default.
The current ONNX engine already works on Macs (CPU), so this is a speed-up, not a requirement. macOS support as a whole (uv tarball, `pmset`, `caffeinate`, the Tk window) hasn't been tested on a real Mac yet.

## The relay
`relay/` is a Cloudflare Worker with a D1 database, on the free plan with **no payment method**, so going over a limit fails safely instead of costing money. Deploy with `npx wrangler deploy` from `relay/` (the maintainer's account). Self-hosting: create your own D1 database, put its id in `wrangler.toml`, deploy, and change `RELAY` in `addon/car_review/common.py` and `docs/app.js` (and `APP_URL` if you host the page yourself).

## Releasing the add-on
1. Run the checks above.
2. `python tools/package_addon.py` builds `dist/car_review.ankiaddon` (no caches, no `meta.json`, with a manifest).
3. Test it: Tools ▸ Add-ons ▸ Install from file, in a fresh Anki profile.
4. Publishing on AnkiWeb (listing text in `ankiweb/listing.md`) and GitHub Releases is done by the project owner, by hand.

## Rules for contributions
- **Never commit card content, collections, keys, pairing links or anything from `~/CarReview`.** Previews go in `local/` (git-ignored).
- Scheduling changes must keep every rule in [how it works](help/how-it-works.md#why-your-scheduling-stays-safe) and pass `apply_check.py`.
- No analytics, trackers or third-party scripts in the phone app.
- Write user-facing text in plain words; say what to do next.
- Commit with your GitHub noreply email. The license is AGPL v3.

## Open decisions
- **Product name.** "Car Review" is fine, but the GitHub org (`AnkiCardReview`) and its Pages address contain "Anki", which is a trademark. Renaming would change the app URL, so pairings would need redoing. Not decided yet.
- **Defaults for new users:** audio is made automatically when plugged in (`auto_voice` on); car answers are applied only after you click Apply (`auto_apply` off).
- Whether to keep about a week of audio on the phone, and spoken "next card in N minutes" cues.
