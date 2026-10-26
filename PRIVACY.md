# Privacy policy

*Last updated: 26 October 2026*

Car Review is a free, open-source project. It's built so that nobody but you can read your cards or your answers, including the people who run it. This page explains, in plain words, what data exists, where it goes and who can see it.

## The short version
- Your cards and answers are **encrypted on your own computer or phone** before they go anywhere. The relay that passes them between your devices only stores scrambled bytes, and deletes them after 3 days.
- There are **no accounts**, no analytics, no ads, no tracking and no third-party scripts.
- The voice is made **on your computer**. Your cards are never sent to a speech service.
- We never sell, share or look at your data. We couldn't read it even if we wanted to.

## What data exists, and where

| Data | Where it's kept | Who can read it |
|---|---|---|
| Your Anki collection | Your computer (and AnkiWeb, if you use Anki's own sync, under Anki's own privacy policy) | You. Car Review never uploads it |
| Car list: the spoken text of your due cards, their audio, and scheduling details (card IDs, due days, learning steps) | Encrypted, in your own mailbox on the relay, for at most 3 days. Decrypted copies on your phone | Only your devices |
| Your car answers: card ID, Again or Good, the time, how long you took | On your phone until uploaded; encrypted in your mailbox until applied (at most 3 days); then as normal reviews in your Anki collection | Only your devices |
| The pairing key (the QR code) | Your computer (`CarReview/pairing.json` in your home folder) and your phone's browser storage | Only you |
| Audio clips and the voice | Your computer (`CarReview` folder) | Only you |
| Car Review settings | Your Anki profile (add-on config) | Only you |

## How the encryption works
When you set up Car Review, your computer makes a random 256-bit secret and shows it as a QR code. Scanning it gives your phone the same secret. From it, each device works out:
- a **mailbox name**: a one-way fingerprint, so the relay can't work back to your secret;
- a **write key**, so nobody else can put files in or delete files from your mailbox;
- an **encryption key** that never leaves your devices. Every file is sealed with AES-256-GCM before upload. Audio files are named with a keyed fingerprint, so even the file names reveal nothing about your cards.

The secret travels in the part of the link after `#`, which browsers never send to any server. If you lose a phone, **Tools ▸ Car Review ▸ Pair phone ▸ Make a new code** deletes your old mailbox and makes a new secret; the old phone can no longer read or write anything.

## Services involved

| Service | Why | What it can see |
|---|---|---|
| **GitHub Pages** (GitHub, Inc.) | Hosts the phone app (a web page) | That a device loaded the page: IP address, browser type and time, as in any web server's logs. Not your key or cards. [GitHub's privacy statement](https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement) |
| **Cloudflare Workers and D1** (Cloudflare, Inc.) | Runs the relay ("mailbox") | Encrypted files, their sizes and times, your mailbox name, and the IP addresses of requests, as Cloudflare processes them to run the service. Car Review doesn't log or keep IP addresses. [Cloudflare's privacy policy](https://www.cloudflare.com/privacypolicy/) |
| **GitHub, PyPI and Astral** | One-time voice setup downloads uv, Python, the voice engine and the voice model | Ordinary download requests (IP address, time). Nothing about you or your cards |

The phone app talks only to the relay. It loads nothing from anywhere else. (The one exception is an experimental developer page, `voice-test.html`, which isn't part of the app: it downloads an open-source voice engine from jsDelivr and a model from Hugging Face, and sends them nothing but the download requests.)

## How long data is kept
- On the relay: **at most 3 days**, then deleted automatically. Mailboxes with no files are forgotten after 30 days.
- On your phone: until a newer car list replaces it, or until you tap **Unpair this phone** or delete the app's website data.
- On your computer: audio clips are kept so each card is only voiced once. Delete the `CarReview` folder in your home folder to remove everything.

## Your choices
- **Unpair the phone:** in the app, More ▸ *Unpair this phone* (deletes everything on the phone).
- **Delete your mailbox:** Tools ▸ Car Review ▸ Pair phone ▸ *Make a new code*.
- **Remove Car Review completely:** see [Removing Car Review](help/setup.md#removing-car-review).

## Children
Car Review is meant for adult learners who drive. It doesn't knowingly collect anything from anyone, of any age.

## Self-hosting
If you'd rather trust nobody, you can host your own copy of the phone app and relay; the code is all public. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Changes and contact
Changes to this policy are made in the public repository, so every version is visible in its history. Questions: [open an issue](https://github.com/AnkiCardReview/car-review/issues). Never post your QR code or pairing link.
