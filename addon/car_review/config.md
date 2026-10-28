**Use Tools ▸ Car Review ▸ Settings instead of editing this.** The Config button opens the same settings window; this page only describes what it stores.

**decks**: the decks to send to the car, played one after another in this order. Subdecks are included.

**days_ahead**: how many days past today the car list covers (default 3), so the phone has cards even if you don't open Anki for a few days. Cards are only played on the day they're due.

**auto_apply**: car answers are picked up when Anki opens and after each sync. `false` (default) shows what they will do and waits for you to click Apply. `true` applies them straight away (with a backup first) and shows a summary afterwards.

**auto_voice**: `true` (default) makes missing audio automatically when Anki closes while the computer is plugged in. `false` only makes audio when you use Tools ▸ Car Review ▸ Prepare audio. Audio is never made on battery.

**morning_job**: `true` (default) sends the new day's exact list half an hour after Anki's day starts (4:30 am by default), without Anki open. On Windows it adds a task called "Car Review morning list" to Task Scheduler. Because many laptops can't wake up for it, closing Anki while plugged in also keeps the laptop from sleeping until then (the screen still turns off); unplugging cancels that. `false` removes the task: the phone gets a new list only when you close Anki.

**python**: leave empty. Only set it if Car Review's voice helper runs from a Python other than the `CarReview/venv` folder that voice setup makes.

**setup_done**: set once the setup window has opened by itself, so it only does that once.
