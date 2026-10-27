# How Car Review works

## The three pieces

| Piece | Where it runs | What it does |
|---|---|---|
| **The add-on** | Anki on your computer | Picks the cards that are due, makes their audio, sends them to your phone, and applies your car answers |
| **The app** | Your phone, as a Home Screen icon (a web page; no app store) | Reads the cards out loud, takes your button presses, works offline |
| **The mailbox** | A small free relay on Cloudflare | Holds your encrypted cards, audio and answers so the phone and computer never need to be online at the same time |

```
 Computer (Anki + add-on)            Mailbox (relay)              Phone (Car Review app)
 ─────────────────────────           ───────────────              ──────────────────────
 close Anki ──► cards + audio ──►  [ scrambled bytes ]  ──►  download, play in the car
 open Anki  ◄── apply answers  ◄── [ scrambled bytes ]  ◄──  upload your answers
```

## A normal day
| When | You do | What happens by itself |
|---|---|---|
| Leaving | Close Anki | Anki syncs, then Car Review sends the cards due today (and the next few days) to the mailbox |
| Early morning | Nothing | Half an hour after Anki's day starts (4:30 am by default), Car Review sends that day's exact list, without Anki open. You can turn this off in Settings (see below) |
| Before driving | Open Car Review on the phone | It downloads the new cards and audio |
| Driving | Tap Start, then use the steering-wheel buttons | Each answer is saved on the phone right away and uploaded when there's signal |
| Arriving | Open Anki | It syncs, downloads your car answers, checks them, and (after you click Apply) records them through Anki's own scheduler |

**What runs on your computer when Anki is closed.** Car Review never runs in secret; each of these shows in Settings and can be turned off:
- **Making audio and sending**: after you close Anki, with a small progress window you can pause or stop.
- **The morning list** (Settings ▸ "Have each day's list on my phone by morning"): a task called "Car Review morning list" in Windows Task Scheduler runs at 4:30 am and sends the new day's list. Many laptops can't wake up for scheduled tasks, so when you close Anki while plugged in, Car Review also keeps the laptop from sleeping until 4:35 am (the screen still turns off). Unplugging cancels it. Turning the setting off removes the task.

**Your drive stats.** The phone's home screen shows the last drive: how long it was, answers, the share of reviews you got right per deck, and seconds per answer. In Anki, **Tools ▸ Car Review ▸ Car stats** shows up to 10 recent drives, car vs Anki retention per deck over the last 7 and 30 days (mature and young separately), and where the drive's time went: listening, your thinking and the phone's own delay. The phone measures those times for each clip (times only, no card text) and sends them along with your answers.

**The one rule:** after a drive, open Anki on the computer before reviewing on AnkiMobile or AnkiDroid. If you forget, nothing breaks: any card you've already reviewed elsewhere is skipped when the car answers are applied, so nothing is ever counted twice.

Data use is under 1 MB a day once a card's audio has been downloaded.

## Your privacy, simply

- **Your cards stay yours.** Everything that leaves your computer is locked (encrypted with AES-256) *before* it leaves, and only unlocked on your phone, and the other way round for your answers. The mailbox, Cloudflare and the people who run Car Review only ever see scrambled bytes.
- **The key is the QR code.** Scanning it gives your phone the key. The key sits in the part of the link after `#`, which browsers never send to any server. (It stays in the page address so that *Add to Home Screen* can hand it to the Home Screen app; that's also why the QR code and that browser tab should stay private.) If a phone is lost, make a new code and the old one stops working.
- **The voice runs on your computer.** Your cards are never sent to a speech service.
- **No accounts, no tracking.** The app has no analytics, no ads and no third-party scripts. It only talks to your own mailbox.
- **Nothing lingers.** The mailbox deletes everything after 3 days.

More detail: [Privacy policy](../PRIVACY.md).

## Why your scheduling stays safe

Car Review never edits your cards or invents intervals. When you click **Apply**, each answer goes through **Anki's own answer function**, the same one used when you press Again or Good in Anki, with the time you pressed the button in the car. So FSRS (or SM-2), learning steps, the load balancer, leech handling and sibling burying all behave exactly as they would in Anki.

Before anything is applied:
1. **Backup.** Anki makes a backup, and **Edit ▸ Undo** reverts the whole batch in one step.
2. **Sync first.** Car Review waits for Anki's opening sync, so it knows about reviews you did on other devices.
3. **Per-card checks.** A card is skipped if it was reviewed anywhere after the car list was made, if it was deleted, suspended, buried or moved out of your car decks, or if it isn't in the state the car list recorded.
4. **In order, never twice.** Each card's answers are applied oldest first. Applied answers are recorded inside your collection (which syncs), so no computer can apply them twice.
5. **Late answers are flagged.** Answers from an earlier Anki day are only applied if you tick the box, because Anki would measure the next interval from today.
6. **Self-check.** For each card, Car Review notes the interval Anki promised for that button, applies the answer, and checks the new due date matches. Anything unexpected is reported instead of hidden.
7. **Only what's possible.** It accepts only cards that were in your own car list, only Again or Good, and only sensible times.

The car itself works like Anki too: a card you miss comes back after your learning step, cards are played in the order Anki would show them, one deck at a time, and only cards due today are played.

## Which cards are played
- Cloze cards in the decks you chose: reviews due today and learning cards (no new cards).
- One card per note per day, as Anki's sibling burying does.
- **Skipped automatically** (they stay due in Anki): non-cloze cards, cards with an image, table or audio on the front, cards that point at a picture ("shown", "labeled", "arrow"), questions over 60 words, AnKing "one by one" cards, and cards you triple-pressed ⏮ on in the car (tagged `car-review::skip`). New cards aren't played in the car either: which ones Anki shows depends on burying and how many you want that day, so they're best done in Anki.
- **Read naturally:** formatting is removed, symbols become words (↑ "increased", → "leads to"), ranges and units are spoken ("24–28 mg/dL" → "24 to 28 milligrams per deciliter"), and common shorthand is expanded. A blank is said as "blank", or as its hint ("…is *which drug?*").

## The audio
The add-on makes each card's audio with **Kokoro "Heart"**, an open-source voice that runs on your computer. It runs only while the computer is plugged in, at low priority, with a small progress window you can pause or stop. Each clip is saved, so a card is only ever voiced once (unless you edit it). On a typical laptop the first batch takes roughly 10 seconds per card; after that it's only new cards, usually a few minutes a day. Voice speed is set on the phone.
