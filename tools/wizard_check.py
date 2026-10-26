"""Check the setup wizard without Anki running: a throwaway collection, an off-screen window,
and a stand-in for Anki's main window. Saves a screenshot of every step.

    python tools/wizard_check.py [--shots help/images]

Needs Anki's GUI package from PyPI in the Python that runs it (`pip install "aqt[qt6]"`). Your
real collection and CarReview folder are never touched (CAR_REVIEW_HOME points to a temp folder).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="car-review-wizard-"))
os.environ["CAR_REVIEW_HOME"] = str(TMP / "home")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
sys.path.insert(0, str(ROOT / "addon"))

from anki.collection import Collection  # noqa: E402

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def make_collection() -> Collection:
    col = Collection(str(TMP / "collection.anki2"))
    cloze = col.models.by_name("Cloze")
    for deck, count in (("Medicine", 3), ("Medicine::Cardio", 2), ("Pharmacology", 4), ("Empty", 0)):
        did = col.decks.id(deck)
        for i in range(count):
            note = col.new_note(cloze)
            note["Text"] = f"{deck} fact {i} is {{{{c1::answer {i}}}}}."
            col.add_note(note, did)
    return col


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", type=Path, default=TMP / "shots")
    args = ap.parse_args()
    args.shots.mkdir(parents=True, exist_ok=True)

    import car_review  # noqa: F401  (with no Anki window yet, this skips the menu setup)
    import aqt
    from aqt.qt import QApplication, QMainWindow, Qt

    app = QApplication(sys.argv)
    if sys.platform == "win32":
        from aqt.qt import QFont

        app.setFont(QFont("Segoe UI", 9))  # the off-screen renderer doesn't pick the system font itself

    class Addons:
        def __init__(self) -> None:
            self.config: dict = {}
            self.writes = 0

        def getConfig(self, _name):  # noqa: N802 (Anki's name)
            return dict(self.config)

        def writeConfig(self, _name, config):  # noqa: N802
            self.config = dict(config)
            self.writes += 1

    class MainWindow(QMainWindow):
        pass

    mw = MainWindow()
    mw.col = make_collection()
    mw.addonManager = Addons()
    aqt.mw = mw

    from car_review import settings

    settings.tooltip = lambda *a, **k: print("tooltip:", a[0])
    dialog = settings.SettingsDialog({}, settings.DEFAULTS, first_run=True)
    dialog.show()

    def settle(secs: float = 0.3) -> None:
        end = time.time() + secs
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    # Wait for the voice check (no voice in the temp folder, so "not set up").
    for _ in range(200):
        settle(0.05)
        if dialog.engine_checked:
            break
    check(dialog.engine_checked and dialog.engine is None, "voice check reports 'not set up' in an empty folder")

    for i, name in enumerate(settings.STEPS):
        dialog.go(i)
        settle()
        if name == "Phone":
            # Don't put a real pairing code in screenshots.
            dialog.qr.setPixmap(settings.qr_pixmap(settings.APP_URL_FOR_SCREENSHOTS))
        dialog.grab().save(str(args.shots / f"wizard-{name.lower()}.png"))
    check(dialog.pages.count() == len(settings.STEPS), "all six steps are built")

    # Decks: the default is every top-level deck with cards due, in deck-list order.
    check(dialog.chosen_decks() == ["Medicine", "Pharmacology"], f"default decks {dialog.chosen_decks()}")
    items = {dialog.tree_item_name(i): i for i in dialog.all_items()}
    # Anki hides the empty Default deck, as in its deck list.
    check(set(items) == {"Empty", "Medicine", "Medicine::Cardio", "Pharmacology"}, f"deck tree {sorted(items)}")
    check(items["Medicine"].text(1) == "5", f"due count includes subdecks ({items['Medicine'].text(1)})")
    items["Medicine::Cardio"].setCheckState(0, Qt.CheckState.Checked)
    check(dialog.chosen_decks() == ["Medicine", "Pharmacology", "Medicine::Cardio"], "ticking adds to the play order")
    dialog.order.setCurrentRow(2)
    dialog.move_deck(-1)
    dialog.move_deck(-1)
    check(dialog.chosen_decks() == ["Medicine::Cardio", "Medicine", "Pharmacology"], "Up moves a deck up")
    dialog.move_deck(-1)
    check(dialog.chosen_decks()[0] == "Medicine::Cardio", "Up at the top does nothing")
    items["Pharmacology"].setCheckState(0, Qt.CheckState.Unchecked)
    check(dialog.chosen_decks() == ["Medicine::Cardio", "Medicine"], "unticking removes from the play order")

    # Phone: a QR code for a pairing link, made without the helper.
    dialog.go(settings.STEPS.index("Phone"))
    settle()
    check(not dialog.qr.pixmap().isNull(), "QR code shown")
    pairing = settings.load_pairing()
    check(settings.pairing_link(pairing).startswith(settings.APP_URL_FOR_SCREENSHOTS + "#pair="), "pairing link")
    old = pairing.code
    settings.repair_pairing()
    check(settings.load_pairing().code != old, "a new code replaces the old one")

    # Done: says what's missing.
    dialog.go(settings.STEPS.index("Done"))
    settle()
    check("voice isn't set up" in dialog.summary.text(), "Done page says the voice is missing")
    check(not dialog.prepare_button.isEnabled(), "Make audio is disabled without the voice")

    # Options and saving.
    dialog.days.setValue(5)
    dialog.auto_apply.setChecked(True)
    dialog.save()
    cfg = mw.addonManager.config
    check(cfg.get("decks") == ["Medicine::Cardio", "Medicine"] and cfg.get("days_ahead") == 5
          and cfg.get("auto_apply") is True and cfg.get("auto_voice") is True and cfg.get("setup_done") is True,
          f"saved config {cfg}")

    # Reopening as settings shows the saved choices.
    again = settings.SettingsDialog(cfg, settings.DEFAULTS, first_run=False, start_at="Decks")
    check(again.chosen_decks() == ["Medicine::Cardio", "Medicine"], "reopened window keeps the deck order")
    check(again.days.value() == 5 and again.auto_apply.isChecked(), "reopened window keeps the options")
    check(again.save_button.text() == "Save", "settings mode says Save")

    mw.col.close(downgrade=False)
    print(f"\nScreenshots: {args.shots}")
    print("All passed" if not failures else f"{len(failures)} failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
