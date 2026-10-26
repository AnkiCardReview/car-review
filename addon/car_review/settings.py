"""Car Review's setup wizard, which is also its settings window.

It opens by itself the first time Anki starts with Car Review installed, and from
Tools ▸ Car Review ▸ Settings (or the Config button in Tools ▸ Add-ons). The steps on the
left can be clicked in any order; Next and Back walk through them:

    Welcome · Decks · Options · Voice · Phone · Done

Nothing is saved until Finish/Save, except the voice setup and the phone pairing, which take
effect straight away.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from pathlib import Path

from aqt import mw
from aqt.qt import (QAbstractItemView, QCheckBox, QDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QListWidget,
                    QListWidgetItem, QPixmap, QProgressBar, QPushButton, QSpinBox, QStackedWidget, Qt,
                    QTimer, QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout, QWidget)
from aqt.utils import askUser, openLink, showWarning, tooltip

from . import voice_setup
from .common import APP_URL, PAIRING, RELAY, load_pairing, pairing_link

APP_URL_FOR_SCREENSHOTS = APP_URL  # the app's address without a pairing code

DEFAULTS = {
    "decks": [],          # decks for the car, played in this order
    "days_ahead": 3,      # send today's cards plus this many days ahead
    "auto_apply": False,  # apply car grades on opening without asking (summary shown afterwards)
    "auto_voice": True,   # make missing audio automatically when Anki closes while plugged in
    "python": "",         # Car Review's Python, if not in the default place
    "setup_done": False,  # the setup wizard has been shown once
}
HELP_URL = "https://github.com/AnkiCardReview/car-review#readme"
TROUBLESHOOTING_URL = "https://github.com/AnkiCardReview/car-review/blob/main/help/troubleshooting.md"
STEPS = ["Welcome", "Decks", "Options", "Voice", "Phone", "Done"]


# ---------- Voice setup, kept running if the window closes ----------

@dataclass
class VoiceJob:
    running: bool = False
    label: str = ""
    fraction: float | None = None
    error: str = ""
    finished: bool = False
    cancel: bool = False


job = VoiceJob()


def start_voice_setup() -> None:
    if job.running:
        return
    job.__init__()  # reset
    job.running = True
    job.label = "Starting…"

    def report(label: str, fraction) -> None:
        job.label, job.fraction = label, fraction

    def work() -> None:
        try:
            voice_setup.install(report, lambda: job.cancel)
            job.finished = True
        except voice_setup.Cancelled:
            job.error = "Voice setup was stopped. Press the button again to carry on where it left off."
        except voice_setup.SetupError as err:
            job.error = str(err)
        except Exception as err:  # anything unexpected: say so plainly, keep the details in the log
            voice_setup._log(f"unexpected error: {err!r}")
            job.error = f"Voice setup stopped unexpectedly ({err}). Try again; details are in CarReview/setup.log."
        finally:
            job.running = False

    threading.Thread(target=work, daemon=True, name="car-review-voice-setup").start()


# ---------- Pairing ----------

def qr_pixmap(text: str) -> QPixmap:
    from .vendor import segno

    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=7, border=3)
    pixmap = QPixmap()
    pixmap.loadFromData(buf.getvalue(), "PNG")
    return pixmap


def repair_pairing() -> None:
    """Forget the old phone: delete its mailbox on the relay and make a new secret."""
    import requests

    if PAIRING.exists():
        old = load_pairing(create=False)
        try:
            requests.delete(f"{RELAY}/{old.box_id}", headers={"X-Write-Key": old.write_key}, timeout=30)
        except requests.RequestException:
            pass  # offline: the old box is deleted by the relay after 3 days anyway
        PAIRING.unlink()
    load_pairing()


# ---------- The window ----------

def text(html: str) -> QLabel:
    label = QLabel(html)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setOpenExternalLinks(True)
    label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    return label


def heading(title: str) -> QLabel:
    label = QLabel(f"<h2 style='margin:0'>{title}</h2>")
    label.setTextFormat(Qt.TextFormat.RichText)
    return label


class SettingsDialog(QDialog):
    def __init__(self, config: dict, defaults: dict, first_run: bool, start_at: str = "Welcome") -> None:
        super().__init__(mw)
        self.config = dict(defaults, **config)
        self.first_run = first_run
        self.setWindowTitle("Car Review setup" if first_run else "Car Review settings")
        self.resize(820, 600)

        outer = QHBoxLayout(self)
        self.nav = QListWidget()
        self.nav.setFixedWidth(150)
        for i, name in enumerate(STEPS, 1):
            self.nav.addItem(f"{i}. {name}")
        outer.addWidget(self.nav)

        right = QVBoxLayout()
        self.pages = QStackedWidget()
        for build in (self.page_welcome, self.page_decks, self.page_options, self.page_voice,
                      self.page_phone, self.page_done):
            page = QWidget()
            layout = QVBoxLayout(page)
            build(layout)
            self.pages.addWidget(page)
        right.addWidget(self.pages, 1)

        buttons = QHBoxLayout()
        help_button = QPushButton("Help")
        help_button.clicked.connect(lambda: openLink(HELP_URL))
        buttons.addWidget(help_button)
        buttons.addStretch(1)
        self.back_button = QPushButton("Back")
        self.next_button = QPushButton("Next")
        self.save_button = QPushButton("Finish" if first_run else "Save")
        cancel = QPushButton("Cancel")
        self.back_button.clicked.connect(lambda: self.go(self.pages.currentIndex() - 1))
        self.next_button.clicked.connect(lambda: self.go(self.pages.currentIndex() + 1))
        self.save_button.clicked.connect(self.save)
        cancel.clicked.connect(self.reject)
        for b in (self.back_button, self.next_button, self.save_button, cancel):
            buttons.addWidget(b)
        right.addLayout(buttons)
        outer.addLayout(right, 1)

        self.nav.currentRowChanged.connect(self.go)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_voice)
        self.timer.start(400)
        self.check_engine()
        self.go(STEPS.index(start_at))

    # ----- navigation -----
    def go(self, index: int) -> None:
        index = max(0, min(len(STEPS) - 1, index))
        if self.nav.currentRow() != index:
            self.nav.setCurrentRow(index)  # calls go() again
            return
        self.pages.setCurrentIndex(index)
        self.back_button.setEnabled(index > 0)
        self.next_button.setVisible(index < len(STEPS) - 1)
        self.next_button.setDefault(index < len(STEPS) - 1)
        self.save_button.setDefault(index == len(STEPS) - 1)
        name = STEPS[index]
        if name == "Voice":
            self.check_engine()
        elif name == "Phone":
            self.show_qr()
        elif name == "Done":
            self.fill_summary()

    # ----- 1. Welcome -----
    def page_welcome(self, layout: QVBoxLayout) -> None:
        layout.addWidget(heading("Review your Anki cards while you drive"))
        layout.addWidget(text(
            "Car Review reads your due <b>cloze</b> cards out loud on your phone, and you answer with your "
            "car's steering-wheel buttons (or your headphones' next/previous track buttons). "
            "Your answers go back into Anki through Anki's own scheduler, exactly as if you had "
            "pressed the buttons in Anki.<br><br>"
            "<b>Setup takes about 10 minutes:</b><ol>"
            "<li>Choose which decks to review in the car.</li>"
            "<li>Pick a few options (the defaults are fine).</li>"
            "<li>Install the natural voice (a one-time download).</li>"
            "<li>Scan a QR code with your phone.</li></ol>"
            "After that there's nothing to remember: close Anki before you leave, open it when you arrive.<br><br>"
            "<b>Safety first:</b> set everything up and press Start <i>before</i> you drive. "
            "While driving, use only the steering-wheel buttons; never look at or touch the phone.<br><br>"
            f"<a href='{HELP_URL}'>Read the guide</a> · free and open source, no account needed."))
        layout.addStretch(1)

    # ----- 2. Decks -----
    def page_decks(self, layout: QVBoxLayout) -> None:
        layout.addWidget(heading("Which decks?"))
        layout.addWidget(text(
            "Tick the decks to review in the car. A ticked deck includes its subdecks. "
            "The car plays one deck at a time, in the order on the right, and within each deck in the "
            "same order Anki would. Only cloze cards are played (other cards stay for Anki)."))
        row = QHBoxLayout()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Deck", "Due today"])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        row.addWidget(self.tree, 3)
        order_box = QVBoxLayout()
        order_box.addWidget(QLabel("<b>Play order</b>"))
        self.order = QListWidget()
        self.order.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        order_box.addWidget(self.order, 1)
        arrows = QHBoxLayout()
        up, down = QPushButton("↑ Up"), QPushButton("↓ Down")
        up.clicked.connect(lambda: self.move_deck(-1))
        down.clicked.connect(lambda: self.move_deck(1))
        arrows.addWidget(up)
        arrows.addWidget(down)
        order_box.addLayout(arrows)
        row.addLayout(order_box, 2)
        layout.addLayout(row, 1)
        self.fill_decks()
        self.tree.itemChanged.connect(self.deck_ticked)

    def fill_decks(self) -> None:
        chosen = list(self.config.get("decks") or [])
        tree = mw.col.sched.deck_due_tree()
        if not chosen and self.first_run:
            # Default: every top-level deck with cards due, in deck-list order.
            chosen = [n.name for n in tree.children if n.review_count + n.learn_count + n.new_count]
        self._filling = True

        def add(node, parent, prefix: str) -> None:
            for child in node.children:
                full = prefix + child.name
                due = child.review_count + child.learn_count + child.new_count
                item = QTreeWidgetItem([child.name, str(due) if due else ""])
                item.setData(0, Qt.ItemDataRole.UserRole, full)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked if full in chosen else Qt.CheckState.Unchecked)
                (parent.addChild(item) if isinstance(parent, QTreeWidgetItem) else self.tree.addTopLevelItem(item))
                add(child, item, full + "::")

        add(tree, self.tree, "")
        self.tree.expandAll()
        self.order.clear()
        existing = {self.tree_item_name(i) for i in self.all_items()}
        for name in chosen:
            if name in existing:
                self.order.addItem(name)
        self._filling = False

    def all_items(self):
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            yield it.value()
            it += 1

    @staticmethod
    def tree_item_name(item) -> str:
        return item.data(0, Qt.ItemDataRole.UserRole)

    def deck_ticked(self, item, column: int) -> None:
        if self._filling or column != 0:
            return
        name = self.tree_item_name(item)
        listed = [self.order.item(i).text() for i in range(self.order.count())]
        if item.checkState(0) == Qt.CheckState.Checked and name not in listed:
            self.order.addItem(name)
        elif item.checkState(0) != Qt.CheckState.Checked and name in listed:
            self.order.takeItem(listed.index(name))

    def move_deck(self, step: int) -> None:
        row = self.order.currentRow()
        if row < 0 or not 0 <= row + step < self.order.count():
            return
        item = self.order.takeItem(row)
        self.order.insertItem(row + step, item)
        self.order.setCurrentRow(row + step)

    def chosen_decks(self) -> list[str]:
        return [self.order.item(i).text() for i in range(self.order.count())]

    # ----- 3. Options -----
    def page_options(self, layout: QVBoxLayout) -> None:
        layout.addWidget(heading("Options"))
        layout.addWidget(text("The defaults suit most people. You can change these any time in "
                              "Tools ▸ Car Review ▸ Settings."))
        days_row = QHBoxLayout()
        days_row.addWidget(QLabel("Also send cards due in the next"))
        self.days = QSpinBox()
        self.days.setRange(0, 7)
        self.days.setValue(int(self.config.get("days_ahead", 3)))
        days_row.addWidget(self.days)
        days_row.addWidget(QLabel("days"))
        days_row.addStretch(1)
        layout.addLayout(days_row)
        layout.addWidget(text("<small>So the phone still has cards if you don't open Anki for a day or two. "
                              "Cards are only ever played on the day they're due.</small>"))
        layout.addSpacing(10)
        self.auto_voice = QCheckBox("Make new audio by itself when I close Anki while my computer is plugged in")
        self.auto_voice.setChecked(bool(self.config.get("auto_voice")))
        layout.addWidget(self.auto_voice)
        layout.addWidget(text("<small>Recommended. The audio is made in the background at low priority, with a "
                              "small progress window, and pauses on battery. Without this, use "
                              "Tools ▸ Car Review ▸ Prepare audio.</small>"))
        layout.addSpacing(10)
        self.auto_apply = QCheckBox("Apply my car answers as soon as Anki opens, without asking first")
        self.auto_apply.setChecked(bool(self.config.get("auto_apply")))
        layout.addWidget(self.auto_apply)
        layout.addWidget(text("<small>Off: Anki shows what your car answers will do and waits for you to click "
                              "Apply. On: they're applied straight away and a summary is shown. Either way Anki "
                              "makes a backup first, and Edit ▸ Undo reverts them in one step.</small>"))
        layout.addStretch(1)

    # ----- 4. Voice -----
    def page_voice(self, layout: QVBoxLayout) -> None:
        layout.addWidget(heading("The voice"))
        layout.addWidget(text(
            "Car Review uses <b>Kokoro “Heart”</b>, a natural-sounding voice that runs on this computer. "
            "Your cards are never sent anywhere to be read out.<br><br>"
            f"<b>One-time download: about {voice_setup.DOWNLOAD_MB} MB</b> (about {voice_setup.DISK_MB} MB on disk), "
            "all into a <i>CarReview</i> folder in your home folder. Nothing is installed system-wide and no "
            "administrator password is needed. It takes about 3–10 minutes, depending on your internet."
            "<br><small>What's downloaded: uv (a small Python installer), Python 3.12, ONNX Runtime with kokoro-onnx "
            "and misaki (the voice engine) and the Kokoro-82M voice model. Each file is checked against a "
            "fixed fingerprint before it's used.</small>"))
        self.voice_status = text("Checking…")
        layout.addWidget(self.voice_status)
        self.voice_bar = QProgressBar()
        self.voice_bar.setVisible(False)
        layout.addWidget(self.voice_bar)
        row = QHBoxLayout()
        self.voice_button = QPushButton("Set up the voice")
        self.voice_button.clicked.connect(self.voice_clicked)
        row.addWidget(self.voice_button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(text("<small>You can carry on with the next steps while it downloads.</small>"))
        layout.addStretch(1)
        self.engine: str | None = None
        self.engine_checked = False

    def check_engine(self) -> None:
        if job.running:
            return

        custom = self.config.get("python")
        python = Path(custom) if custom else voice_setup.helper_python()

        def work() -> None:
            self.engine = voice_setup.installed_engine(python)
            self.engine_checked = True

        threading.Thread(target=work, daemon=True).start()

    def voice_clicked(self) -> None:
        if job.running:
            job.cancel = True
            return
        reason = voice_setup.supported()
        if reason:
            showWarning(reason, parent=self)
            return
        start_voice_setup()

    def refresh_voice(self) -> None:
        if job.running:
            self.voice_status.setText(f"<b>{job.label}</b>")
            self.voice_bar.setVisible(True)
            if job.fraction is None:
                self.voice_bar.setRange(0, 0)  # busy, can't be measured
            else:
                self.voice_bar.setRange(0, 1000)
                self.voice_bar.setValue(int(job.fraction * 1000))
            self.voice_button.setText("Stop" if not job.cancel else "Stopping…")
            return
        self.voice_bar.setVisible(False)
        if job.finished:
            job.finished = False
            self.engine, self.engine_checked = "onnx", True
        if job.error:
            self.voice_status.setText(f"<span style='color:#c0392b'><b>{job.error}</b></span> "
                                      f"<a href='{TROUBLESHOOTING_URL}'>Troubleshooting</a>")
            self.voice_button.setText("Try again")
            return
        if not self.engine_checked:
            return
        if self.engine:
            self.voice_status.setText("<span style='color:#27ae60'><b>✓ The voice is ready.</b></span>"
                                      + (" (PyTorch engine)" if self.engine == "torch" else ""))
            self.voice_button.setText("Reinstall the voice")
        else:
            self.voice_status.setText("<b>Not set up yet.</b>")
            self.voice_button.setText("Set up the voice")

    # ----- 5. Phone -----
    def page_phone(self, layout: QVBoxLayout) -> None:
        layout.addWidget(heading("Connect your phone"))
        row = QHBoxLayout()
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr.setMinimumSize(260, 260)
        self.qr.setFrameShape(QFrame.Shape.StyledPanel)
        row.addWidget(self.qr)
        row.addWidget(text(
            "<ol style='margin-left:-20px'>"
            "<li><b>Open your phone's Camera</b> and point it at this code. Tap the link that appears.</li>"
            "<li><b>Add it to your Home Screen</b><br>"
            "iPhone (Safari): tap Share <b>□↑</b> ▸ <b>Add to Home Screen</b> ▸ Add.<br>"
            "Android (Chrome): tap <b>⋮</b> ▸ <b>Add to Home screen</b>.</li>"
            "<li><b>Open Car Review from your Home Screen.</b> It shows “Paired ✓”, then a short "
            "walk-through of the buttons.</li></ol>"
            "No account, no app store. Your cards and answers are encrypted on this computer and on "
            "the phone, so nobody else (not even the mailbox they pass through) can read them.<br><br>"
            "<b>Keep this code private</b>: it's the key to your car list."), 1)
        layout.addLayout(row, 1)
        repair = QPushButton("Phone lost or replaced? Make a new code (the old phone stops working)")
        repair.clicked.connect(self.repair_clicked)
        layout.addWidget(repair)

    def show_qr(self) -> None:
        self.qr.setPixmap(qr_pixmap(pairing_link(load_pairing())))

    def repair_clicked(self) -> None:
        if not askUser("Make a new pairing code? The phone that's paired now stops working until it scans "
                       "the new code.", parent=self):
            return
        repair_pairing()
        self.show_qr()
        tooltip("New code made. Scan it with your phone.", parent=self)

    # ----- 6. Done -----
    def page_done(self, layout: QVBoxLayout) -> None:
        self.done_heading = heading("You're all set")
        layout.addWidget(self.done_heading)
        self.summary = text("")
        layout.addWidget(self.summary)
        row = QHBoxLayout()
        self.prepare_button = QPushButton("Make audio and send to my phone now")
        self.prepare_button.clicked.connect(self.prepare_now)
        row.addWidget(self.prepare_button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)

    def fill_summary(self) -> None:
        decks = self.chosen_decks()
        voice_ok = bool(self.engine) or job.running
        problems = []
        if not decks:
            problems.append("<li><b>No decks chosen yet</b>: go back to step 2.</li>")
        if not voice_ok:
            problems.append("<li><b>The voice isn't set up yet</b>: go back to step 4.</li>")
        todo = f"<p style='color:#c0392b'>Still to do:</p><ul>{''.join(problems)}</ul>" if problems else ""
        self.done_heading.setText(f"<h2 style='margin:0'>{'Almost done' if problems else 'You’re all set'}</h2>")
        self.summary.setText(
            todo +
            "<b>From now on</b><ul>"
            "<li><b>When you close Anki</b>, your due cards are sent to your phone. "
            + ("New cards get their audio while your computer is plugged in.</li>" if self.auto_voice.isChecked()
               else "New cards need audio first: Tools ▸ Car Review ▸ Prepare audio.</li>") +
            "<li><b>The first time</b>, making audio takes a while (roughly 5–15 seconds per card on a "
            "laptop, much faster on newer Macs), so start it now, ideally plugged in. You can keep using "
            "the computer; a small window shows progress.</li>"
            "<li><b>Before you drive</b>, open Car Review on your phone (on Wi-Fi if you can) so it "
            "downloads the cards, then tap <b>Start</b>.</li>"
            "<li><b>In the car</b>: ⏭ next track = show the answer, then ⏭ again = Good. ⏮ previous "
            "track = Again (or repeat the question before the answer).</li>"
            "<li><b>When you open Anki</b> again, it shows your car answers"
            + (" and applies them." if self.auto_apply.isChecked() else " with an Apply button.") + "</li></ul>"
            f"Questions? <a href='{HELP_URL}'>Read the guide</a>.")
        self.prepare_button.setEnabled(bool(decks) and bool(self.engine))

    def prepare_now(self) -> None:
        from . import anki_addon

        self.save(close=False)
        anki_addon.prepare_audio()

    # ----- saving -----
    def collect(self) -> dict:
        cfg = dict(self.config)
        cfg.update({
            "decks": self.chosen_decks(),
            "days_ahead": self.days.value(),
            "auto_voice": self.auto_voice.isChecked(),
            "auto_apply": self.auto_apply.isChecked(),
            "setup_done": True,
        })
        return cfg

    def save(self, close: bool = True) -> None:
        cfg = self.collect()
        mw.addonManager.writeConfig(__name__, cfg)
        self.config = cfg
        if close:
            self.timer.stop()
            self.accept()
            if not cfg["decks"]:
                tooltip("Car Review: no decks chosen, so nothing is sent to your phone yet.", period=6000)
            else:
                tooltip("Car Review is set up. Close Anki before you leave and your cards go to your phone.",
                        period=6000)

    def reject(self) -> None:
        self.timer.stop()
        super().reject()


def open_settings(start_at: str = "Welcome", first_run: bool = False) -> None:
    if not mw.col:
        return
    existing = getattr(mw, "_car_review_settings", None)
    if existing is not None and existing.isVisible():
        existing.go(STEPS.index(start_at))
        existing.raise_()
        existing.activateWindow()
        return
    config = mw.addonManager.getConfig(__name__) or {}
    if not first_run and start_at == "Welcome":
        start_at = "Decks"
    dialog = SettingsDialog(config, DEFAULTS, first_run=first_run, start_at=start_at)
    dialog.show()  # not modal, so Anki stays usable while the voice downloads
    mw._car_review_settings = dialog  # keep a reference
