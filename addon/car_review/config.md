**decks**: the decks to send to the car, played one after another in this order. Subdecks are included. Example: `["Deck A", "Deck B"]`

**days_ahead**: how many days past today the car list covers (default 3), so the phone has cards even if you don't open Anki for a few days.

**auto_apply**: `false` (default) shows what your car grades will do when Anki opens and waits for you to click Apply. `true` applies them straight away (with a backup first) and shows a summary afterwards.

**auto_voice**: `false` (default) only makes new audio when you use Tools ▸ Car Review ▸ Prepare audio; Anki reminds you when cards need it. `true` also makes missing audio automatically when Anki closes while the laptop is plugged in. Audio is never made on battery.

**python**: leave empty. Only set it if Car Review's voice helper is installed somewhere other than the default `CarReview/venv` folder in your home folder.
