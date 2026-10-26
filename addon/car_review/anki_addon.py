"""Anki side of Car Review.

Automatic:
- First run: the setup wizard (settings.py) opens by itself, once.
- When Anki opens (after its opening sync): if there are car grades, show what they'd do,
  with an Apply button. Then remind you if upcoming car cards still need audio.
- When Anki closes: send the car list (today plus the next few days) to your phone. This is
  quick: only cards that already have audio are sent. If "auto_voice" is on and the laptop is
  plugged in, missing audio is made too, after Anki has closed.

Tools ▸ Car Review menu: Settings, Send to Car, Prepare audio, Apply car grades, Pair phone,
Status, Help.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from anki.collection import OpChanges
from aqt import gui_hooks, mw
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import QAction, QCheckBox, QDialog, QDialogButtonBox, QLabel, QMenu, QScrollArea, QTimer, QVBoxLayout
from aqt.utils import askUser, openLink, showInfo, showWarning, tooltip

from . import voice_setup
from .apply import Plan, apply_plan, plan_grades
from .builder import build_pack
from .common import CACHE, HOME, INBOX, PACK, STATUS
from .power import on_charger
from .settings import DEFAULTS, HELP_URL, open_settings

ADDON_DIR = Path(__file__).resolve().parent


def setting(key: str):
    return (mw.addonManager.getConfig(__name__) or {}).get(key, DEFAULTS[key])


# ---------- Running the helper ----------

def helper_python(windowless: bool = False) -> Path:
    if setting("python"):
        return Path(setting("python"))
    return voice_setup.helper_python(windowless)


def helper_command(*args: str, windowless: bool = False) -> list[str]:
    python = helper_python(windowless)
    if not python.exists():
        raise RuntimeError("Car Review's voice isn't set up on this computer yet. "
                           "Open Tools ▸ Car Review ▸ Settings ▸ Voice and press “Set up the voice”.")
    # The add-on's folder is "car_review" when copied from GitHub, or a number when installed from AnkiWeb.
    return [str(python), "-m", f"{ADDON_DIR.name}.helper", *args]


def _flags(detached: bool) -> int:
    if sys.platform != "win32":
        return 0
    return subprocess.CREATE_NO_WINDOW | (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if detached else 0)


def run_helper(*args: str) -> subprocess.CompletedProcess:
    """Run a short helper command and wait for it (safe to call from a background op)."""
    return subprocess.run(helper_command(*args), capture_output=True, text=True, timeout=180,
                          cwd=str(ADDON_DIR.parent), creationflags=_flags(False))


def start_helper(*args: str) -> None:
    """Start the helper on its own; it keeps running (with its own window) after Anki closes."""
    subprocess.Popen(helper_command(*args, windowless=True), cwd=str(ADDON_DIR.parent), creationflags=_flags(True),
                     start_new_session=sys.platform != "win32")


# ---------- Sending ----------

def missing_audio(pack: dict) -> int:
    return sum(1 for c in pack["cards"]
               if not (CACHE / f"{c['q_audio']}.mp3").exists() or not (CACHE / f"{c['a_audio']}.mp3").exists())


def start_send(pack: dict, voice: bool) -> None:
    pack["voice_missing"] = voice
    PACK.parent.mkdir(parents=True, exist_ok=True)
    PACK.write_text(json.dumps(pack), encoding="utf-8")
    start_helper("send", "--pack", str(PACK))


def build(col) -> dict:
    return build_pack(col, setting("decks"), days_ahead=int(setting("days_ahead")))


def need_setup() -> bool:
    """True (after offering the settings window) while decks or the voice are still missing."""
    if not setting("decks"):
        if askUser("Choose which decks to review in the car first. Open Car Review settings?"):
            open_settings("Decks")
        return True
    if not helper_python().exists():
        if askUser("The voice isn't set up on this computer yet (a one-time download). Set it up now?"):
            open_settings("Voice")
        return True
    return False


def no_cards_message(pack: dict) -> str:
    skipped = pack.get("skipped") or {}
    if skipped.get("not a cloze card"):
        return ("No cards to send: Car Review only plays cloze cards, and the chosen decks have none due. "
                "Choose other decks in Tools ▸ Car Review ▸ Settings.")
    return "No cards are due in your car decks today or in the next few days."


def send_to_car() -> None:
    """Quick send: only cards that already have audio."""
    if need_setup():
        return

    def done(pack: dict) -> None:
        if not pack["cards"]:
            showInfo(no_cards_message(pack))
            return
        start_send(pack, voice=False)
        missing = missing_audio(pack)
        tooltip(f"Sending {len(pack['cards']) - missing} cards to your phone."
                + (f" {missing} still need audio (Tools ▸ Car Review ▸ Prepare audio)." if missing else ""),
                period=8000)

    QueryOp(parent=mw, op=build, success=done).with_progress("Building car list…").run_in_background()


def prepare_audio() -> None:
    """Make missing audio for the coming days, then send everything."""
    if need_setup():
        return
    if not on_charger() and not askUser(
            "Your computer is on battery. Audio is only made while it's plugged in, so it will wait "
            "until you plug in. Start anyway?"):
        return

    def done(pack: dict) -> None:
        if not pack["cards"]:
            showInfo(no_cards_message(pack))
            return
        missing = missing_audio(pack)
        start_send(pack, voice=True)
        tooltip(f"Making audio for {missing} cards, then sending {len(pack['cards'])} to your phone. "
                "Progress shows in its own window.", period=8000)

    QueryOp(parent=mw, op=build, success=done).with_progress("Building car list…").run_in_background()


def on_close() -> None:
    """Send the list when Anki closes. Never blocks closing for more than the few seconds the
    list takes to build, and never raises."""
    try:
        if not setting("decks") or not mw.col or not helper_python().exists():
            return
        pack = build(mw.col)
        start_send(pack, voice=bool(setting("auto_voice")) and on_charger())
    except Exception as err:  # closing Anki must never fail because of Car Review
        print(f"Car Review: couldn't send on close: {err}")


# ---------- When Anki opens ----------

_startup_pending = False


def on_profile_open() -> None:
    global _startup_pending
    config = mw.addonManager.getConfig(__name__) or {}
    if not config.get("decks") and not config.get("setup_done"):
        # First run: open the setup wizard once Anki has settled. Only once; after that it's in
        # Tools ▸ Car Review ▸ Settings.
        config["setup_done"] = True
        mw.addonManager.writeConfig(__name__, config)
        QTimer.singleShot(3_000, first_run)
        return
    _startup_pending = True
    # If Anki syncs on open, wait for that sync so phone reviews are known; otherwise go now.
    syncs_on_open = mw.pm.auto_syncing_enabled() and bool(mw.pm.sync_auth())
    QTimer.singleShot(60_000 if syncs_on_open else 2_000, startup_check)


def first_run() -> None:
    open_settings(first_run=True)


def on_sync_finished() -> None:
    if _startup_pending:
        startup_check()


def startup_check() -> None:
    global _startup_pending
    if not _startup_pending:
        return
    _startup_pending = False
    if not setting("decks") or not helper_python().exists():
        return
    check_grades(on_startup=True)


def remind_about_audio() -> None:
    if setting("auto_voice"):
        return  # made automatically when Anki closes while plugged in

    def done(pack: dict) -> None:
        missing = missing_audio(pack)
        if missing:
            tooltip(f"Car Review: {missing} cards in the next {int(setting('days_ahead')) + 1} days have no audio yet. "
                    "When plugged in: Tools ▸ Car Review ▸ Prepare audio.", period=12000)

    QueryOp(parent=mw, op=build, success=done).run_in_background()


# ---------- Applying car grades ----------

def check_grades(on_startup: bool = False) -> None:
    def fetch_and_plan(col):
        result = run_helper("fetch-grades")
        if result.returncode != 0:
            detail = (result.stderr.strip().splitlines() or [""])[-1][:300]
            raise RuntimeError("Couldn't download your car answers. Check your internet connection, then try "
                               f"Tools ▸ Car Review ▸ Apply car grades.\n\nDetails: {detail}")
        names = json.loads(result.stdout.strip() or "[]")
        files = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(INBOX.glob("*.json"))]
        return (plan_grades(col, files) if files else None), names

    def done(result) -> None:
        plan, names = result
        if plan is None or (not plan.cards and not plan.skip_tags):
            if names:
                subprocess.Popen(helper_command("delete-grades", *names), cwd=str(ADDON_DIR.parent),
                                 creationflags=_flags(True))
            if not on_startup:
                tooltip("No car answers waiting. After a drive, check that the phone shows “✓ all” "
                        "next to Grades uploaded.", period=8000)
            else:
                remind_about_audio()
            return
        if on_startup and setting("auto_apply") and not plan.late_days:
            apply_now(plan, names, allow_late=False, then=remind_about_audio)
        else:
            confirm_apply(plan, names, then=remind_about_audio if on_startup else None)

    def failed(err: Exception) -> None:
        if on_startup:
            tooltip("Car Review: couldn't check for car answers (are you offline?). "
                    "Try Tools ▸ Car Review ▸ Apply car grades later.", period=8000)
            print(f"Car Review: {err}")
        else:
            showWarning(str(err))

    op = QueryOp(parent=mw, op=fetch_and_plan, success=done).failure(failed)
    (op if on_startup else op.with_progress("Checking car grades…")).run_in_background()


def apply_car_grades() -> None:
    if need_setup():
        return
    check_grades(on_startup=False)


def confirm_apply(plan: Plan, names: list[str], then=None) -> None:
    goods = sum(1 for c in plan.cards for g in c.grades if g["rating"] == 3)
    agains = plan.grade_count - goods

    dialog = QDialog(mw)
    dialog.setWindowTitle("Car Review: apply car grades")
    layout = QVBoxLayout(dialog)
    lines = [f"<b>{plan.grade_count} answers from the car for {len(plan.cards)} cards</b> ({goods} Good, {agains} Again)."]
    if plan.skip_tags:
        lines.append(f"{len(plan.skip_tags)} card(s) you skipped as not audio-friendly will be tagged "
                     "<i>car-review::skip</i> and left out of future car lists.")
    if plan.skipped:
        lines.append("Not applied: " + ", ".join(f"{v} {k}" for k, v in plan.skipped.items()) + ".")
    lines.append("Anki makes a backup first, and <b>Edit ▸ Undo</b> reverts all of it in one step.")
    text = QLabel("<br><br>".join(lines))
    text.setWordWrap(True)
    layout.addWidget(text)

    preview = QLabel("<br>".join(f"{c.preview}" for c in plan.cards[:40])
                     + (f"<br>… and {len(plan.cards) - 40} more" if len(plan.cards) > 40 else ""))
    scroll = QScrollArea()
    scroll.setWidget(preview)
    scroll.setMaximumHeight(220)
    layout.addWidget(QLabel("First answer for each card (what Anki will do):"))
    layout.addWidget(scroll)

    late = None
    if plan.late_days:
        late = QCheckBox("Some answers are from an earlier day. Apply them anyway "
                         "(their next interval is counted from today, so it lands about a day later).")
        late.setChecked(False)
        layout.addWidget(late)

    buttons = QDialogButtonBox()
    apply_button = buttons.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
    buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    apply_button.setDefault(True)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        if then:
            then()
        return
    apply_now(plan, names, allow_late=bool(late and late.isChecked()), then=then)


def apply_now(plan: Plan, names: list[str], allow_late: bool, then=None) -> None:
    report_box: dict = {}

    def op(col):
        mw.create_backup_now()  # waits until the backup is written
        report = apply_plan(col, plan, allow_late=allow_late)
        report_box["report"] = report
        return report.changes or OpChanges()  # nothing changed when late grades were held back

    def done(_changes) -> None:
        r = report_box["report"]
        summary = [f"Applied {sum(r.applied.values())} car answers to {r.cards} cards "
                   f"({r.applied.get('Good', 0)} Good, {r.applied.get('Again', 0)} Again)."]
        if r.tagged:
            summary.append(f"Tagged {r.tagged} card(s) as not audio-friendly.")
        if r.skipped:
            summary.append("Not applied: " + ", ".join(f"{v} {k}" for k, v in r.skipped.items()) + ".")
        if r.mismatches or r.failures:
            summary.append("\nProblems (please report these):\n" + "\n".join(r.mismatches + r.failures))
            showWarning("\n".join(summary))
        else:
            tooltip("<br>".join(summary) + "<br>Edit ▸ Undo reverts it. Sync when you're ready.", period=10000)
        if not r.failures and not (plan.late_days and not allow_late):
            subprocess.Popen(helper_command("delete-grades", *names), cwd=str(ADDON_DIR.parent),
                             creationflags=_flags(True))
        if then:
            then()

    CollectionOp(parent=mw, op=op).success(done).run_in_background()


# ---------- Pair phone ----------

def pair_phone() -> None:
    open_settings("Phone")


# ---------- Status ----------

def show_status() -> None:
    if not STATUS.exists():
        showInfo("Nothing has been sent to your phone yet. Close Anki (or use Tools ▸ Car Review ▸ Send to Car) "
                 "to send your cards.")
        return
    s = json.loads(STATUS.read_text())
    if s.get("state") == "error":
        showWarning(f"The last send stopped with an error:\n\n{s.get('error')}\n\n"
                    f"Try again with Tools ▸ Car Review ▸ Send to Car. Details are in {HOME / 'helper.log'}")
        return
    mins = round(s.get("elapsed", 0) / 60)
    lines = [f"Last run: {s.get('state', '?')}."]
    if "ready" in s:
        lines.append(f"Cards on the phone: {s.get('ready', 0)} of {s.get('total', 0)}")
    if s.get("unvoiced"):
        lines.append(f"Cards still without audio: {s['unvoiced']} (Tools ▸ Car Review ▸ Prepare audio)")
    lines.append(f"New audio made: {s.get('made', 0)} clips in {mins} min")
    showInfo("\n".join(lines))


# ---------- Setup ----------

def open_settings_menu() -> None:
    open_settings()


def setup() -> None:
    menu = QMenu("Car Review", mw)
    days = int(setting("days_ahead"))
    for label, fn in (("Settings…", open_settings_menu),
                      (None, None),
                      ("Send to Car", send_to_car),
                      (f"Prepare audio (next {days} days)", prepare_audio),
                      ("Apply car grades…", apply_car_grades),
                      ("Pair phone…", pair_phone),
                      ("Status", show_status),
                      (None, None),
                      ("Help", lambda: openLink(HELP_URL))):
        if label is None:
            menu.addSeparator()
            continue
        action = QAction(label, mw)
        action.triggered.connect(fn)
        menu.addAction(action)
    mw.form.menuTools.addMenu(menu)
    # Tools ▸ Add-ons ▸ Config opens the settings window instead of the raw JSON.
    mw.addonManager.setConfigAction(__name__, open_settings_menu)
    gui_hooks.profile_did_open.append(on_profile_open)
    gui_hooks.sync_did_finish.append(on_sync_finished)
    gui_hooks.profile_will_close.append(on_close)


setup()
