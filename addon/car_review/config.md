**Use Tools ▸ Car Review ▸ Settings instead of editing this.** The Config button opens the same settings window; this page only describes what it stores.

**decks**: the decks to send to the car, played one after another in this order. Subdecks are included.

**days_ahead**: how many days past today the car list covers (default 3), so the phone has cards even if you don't open Anki for a few days. Cards are only played on the day they're due.

**auto_apply**: `false` (default) shows what your car answers will do when Anki opens and waits for you to click Apply. `true` applies them straight away (with a backup first) and shows a summary afterwards.

**auto_voice**: `true` (default) makes missing audio automatically when Anki closes while the computer is plugged in. `false` only makes audio when you use Tools ▸ Car Review ▸ Prepare audio. Audio is never made on battery.

**python**: leave empty. Only set it if Car Review's voice helper runs from a Python other than the `CarReview/venv` folder that voice setup makes.

**setup_done**: set once the setup window has opened by itself, so it only does that once.
