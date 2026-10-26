# Car Review

Hands-free flashcard reviews while you drive. Your iPhone reads your due cards out loud, and you answer with your car's steering-wheel buttons (next = reveal / Good, previous = repeat / Again). The results go back into your collection through the desktop app's own scheduler, as if you had pressed the buttons yourself. Works with Anki.

**Status:** early testing. Right now this repository only contains a button test page (`docs/`), which checks that your phone sends next/previous track to a web page.

## Privacy
- This repository holds only app code. It never contains cards, review history, keys or personal information.
- Each user's car lists and results are stored only in that user's own private storage and are deleted after 3 days.
- The web page sends no analytics and loads no third-party scripts. The one exception is the experimental phone voice test (`docs/voice-test.html`): it loads the open-source voice engine from jsDelivr and the voice model from Hugging Face, and sends them nothing but the download requests.

## Safety
Set it up and press Start before you drive. Everything after that is designed to be done with the steering-wheel buttons and your voice only; never look at or touch the phone while driving.

## License
[GNU AGPL v3](LICENSE). Free to use, copy and change; changed versions must stay open source.
