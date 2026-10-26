# AnkiWeb listing (draft, not published)

Paste these into https://ankiweb.net/shared/upload when publishing. **Ask the project owner before uploading.**

- **File:** `dist/car_review.ankiaddon` (build it with `python tools/package_addon.py`)
- **Title:** Car Review: hands-free reviews while you drive
- **Tags:** audio driving commute hands-free cloze tts
- **Support page:** https://github.com/AnkiCardReview/car-review/issues
- **Supported Anki versions:** 24.11 and newer (tested with 26.09)

## Description (AnkiWeb accepts simple HTML)

```html
<b>Review your cloze cards in the car, hands-free.</b> Your phone reads your due cards out loud and you answer with your car's steering-wheel buttons (or your headphones' next/previous track buttons). Your answers go back into Anki through Anki's own scheduler, exactly as if you had pressed the buttons in Anki.

<b>How it works</b>
<ul>
<li>A setup window opens after installing: choose your decks, set up the natural voice (a one-time download, nothing installed system-wide) and scan a QR code with your phone. About 10 minutes.</li>
<li>When you close Anki, your due cards are sent to your phone. The voice (Kokoro "Heart") runs on your computer while it's plugged in.</li>
<li>In the car: ⏭ = hear the answer, ⏭ again = Good, ⏮ = Again.</li>
<li>When you open Anki, it shows your car answers and applies them, with a backup first and one-step Undo.</li>
</ul>

<b>Safe for your scheduling:</b> every answer goes through Anki's own answer function with the time you pressed the button. Cards reviewed elsewhere in the meantime are skipped, nothing is applied twice, and each result is checked against what Anki promised.

<b>Private:</b> cards and answers are encrypted on your computer and phone before they're sent, and the relay deletes them after 3 days. No account, no tracking.

<b>Phone:</b> iPhone or Android, as a Home Screen web app (no app store).
<b>Limits:</b> cloze cards only; English voice; cards with images on the front are skipped.

<b>Safety:</b> set up and press Start before you drive. Never look at or touch your phone while driving.

Free and open source (AGPL v3). Guide, privacy policy and source: <a href="https://github.com/AnkiCardReview/car-review">github.com/AnkiCardReview/car-review</a>

Car Review is an independent project, not affiliated with Anki or Ankitects.
```

## Before publishing (checklist)
- [ ] Decide the product name (see "Open decisions" in CONTRIBUTING.md).
- [ ] Test install from `dist/car_review.ankiaddon` in a clean Anki profile, on Windows and on a Mac.
- [ ] Add 2–3 screenshots (help/images) to the listing.
- [ ] After the first upload, put the AnkiWeb code into README.md and help/setup.md.
