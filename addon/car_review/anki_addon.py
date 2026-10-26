"""Anki side of Car Review: Tools ▸ Car Review menu.

- Pair phone: shows the QR code the phone scans once.
- Send to Car: builds tomorrow's car list from your decks (read-only) and hands it to the
  background helper, which makes any missing audio, encrypts and uploads it. The helper keeps
  running after Anki closes.
- Status: how far the last send got.

This version never changes your collection. Applying car grades comes in a later version.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from anki.collection import OpChanges
from aqt import mw
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import (QAction, QCheckBox, QDialog, QDialogButtonBox, QLabel, QMenu, QPixmap, QPushButton,
                    QScrollArea, Qt, QVBoxLayout)
from aqt.utils import showInfo, showWarning, tooltip

from .apply import Plan, apply_plan, plan_grades
from .builder import build_pack

ADDON_DIR = Path(__file__).resolve().parent
HOME = Path.home() / "CarReview"
PACK = HOME / "outbox" / "pack.json"
STATUS = HOME / "status.json"


def config() -> dict:
    return mw.addonManager.getConfig(__name__) or {}


def helper_python() -> Path:
    custom = config().get("python")
    if custom:
        return Path(custom)
    venv = HOME / "venv"
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def run_helper(*args: str, wait: bool = True) -> subprocess.CompletedProcess | None:
    python = helper_python()
    if not python.exists():
        showWarning(f"Car Review's voice helper isn't set up on this computer yet.\n\nExpected it at:\n{python}")
        return None
    cmd = [str(python), "-m", "car_review.helper", *args]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | (0 if wait else subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    kwargs = dict(cwd=str(ADDON_DIR.parent), creationflags=flags)
    if wait:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kwargs)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open(HOME / "send.log", "w"), **kwargs)
    return None


# ---------- Pair phone ----------

def pair_phone() -> None:
    out = Path(tempfile.gettempdir()) / "car-review-pair.png"
    result = run_helper("qr", "--out", str(out))
    if result is None:
        return
    if result.returncode != 0:
        showWarning(f"Couldn't make the pairing code:\n{result.stderr[-800:]}")
        return
    dialog = QDialog(mw)
    dialog.setWindowTitle("Car Review: pair your phone")
    layout = QVBoxLayout(dialog)
    text = QLabel(
        "Scan this with your phone's camera and open the link.\n"
        "Car Review opens already paired. Then tap Share ▸ Add to Home Screen.\n\n"
        "Keep this code private: it's the key to your car list."
    )
    text.setWordWrap(True)
    layout.addWidget(text)
    image = QLabel()
    image.setPixmap(QPixmap(str(out)))
    image.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(image)
    repair = QPushButton("Lost or new phone? Make a new code (the old phone stops working)")
    layout.addWidget(repair)
    close = QPushButton("Done")
    close.clicked.connect(dialog.accept)
    layout.addWidget(close)

    def do_repair() -> None:
        r = run_helper("repair")
        if r and r.returncode == 0:
            dialog.accept()
            tooltip("New pairing made. Scan the new code.")
            pair_phone()

    repair.clicked.connect(do_repair)
    dialog.exec()
    out.unlink(missing_ok=True)


# ---------- Send to Car ----------

def send_to_car() -> None:
    decks = config().get("decks") or []
    if not decks:
        showInfo("Choose your decks first: Tools ▸ Add-ons ▸ car_review ▸ Config, and list them under \"decks\".")
        return

    def build(col):
        return build_pack(col, decks)

    def done(pack: dict) -> None:
        PACK.parent.mkdir(parents=True, exist_ok=True)
        PACK.write_text(json.dumps(pack), encoding="utf-8")
        run_helper("send", "--pack", str(PACK), wait=False)
        skipped = sum(pack["skipped"].values())
        tooltip(
            f"Sending {len(pack['cards'])} cards to the car ({skipped} skipped as not audio-friendly). "
            "New audio is made in the background; check Tools ▸ Car Review ▸ Status.",
            period=8000,
        )

    QueryOp(parent=mw, op=build, success=done).with_progress("Building car list…").run_in_background()


# ---------- Status ----------

def show_status() -> None:
    if not STATUS.exists():
        showInfo("Nothing sent to the car yet.")
        return
    s = json.loads(STATUS.read_text())
    if s.get("state") == "error":
        showWarning(f"The last send stopped with an error:\n\n{s.get('error')}\n\nDetails are in {HOME / 'send.log'}")
        return
    mins = round(s.get("elapsed", 0) / 60)
    state = "finished" if s.get("state") == "done" else "still working"
    showInfo(
        f"Last send: {state}.\n\n"
        f"Cards ready on the phone: {s.get('ready', 0)} of {s.get('total', 0)}\n"
        f"New audio made: {s.get('made', 0)} clips in {mins} min"
    )


# ---------- Apply car grades ----------

INBOX = HOME / "inbox"


def apply_car_grades() -> None:
    result = run_helper("fetch-grades")
    if result is None:
        return
    if result.returncode != 0:
        showWarning(f"Couldn't download your car grades:\n{result.stderr[-800:]}")
        return
    names = json.loads(result.stdout.strip() or "[]")
    files = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(INBOX.glob("*.json"))]
    if not files:
        tooltip("No car grades waiting.")
        return
    QueryOp(parent=mw, op=lambda col: plan_grades(col, files), success=lambda plan: confirm_apply(plan, names)) \
        .with_progress("Checking car grades…").run_in_background()


def confirm_apply(plan: Plan, names: list[str]) -> None:
    goods = sum(1 for c in plan.cards for g in c.grades if g["rating"] == 3)
    agains = plan.grade_count - goods
    if not plan.cards and not plan.skip_tags:
        reasons = "\n".join(f"• {k}: {v}" for k, v in plan.skipped.items()) or "• already applied"
        showInfo(f"Nothing to apply.\n\n{reasons}")
        run_helper("delete-grades", *names)
        return

    dialog = QDialog(mw)
    dialog.setWindowTitle("Car Review: apply car grades")
    layout = QVBoxLayout(dialog)
    lines = [f"<b>{plan.grade_count} grades for {len(plan.cards)} cards</b> ({goods} Good, {agains} Again)."]
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
        late = QCheckBox("Some grades are from an earlier day. Apply them anyway "
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
        return

    allow_late = bool(late and late.isChecked())
    report_box: dict = {}

    def op(col):
        mw.create_backup_now()  # waits until the backup is written
        report = apply_plan(col, plan, allow_late=allow_late)
        report_box["report"] = report
        return report.changes or OpChanges()  # nothing changed when late grades were held back

    def done(_changes) -> None:
        r = report_box["report"]
        summary = [f"Applied {sum(r.applied.values())} grades to {r.cards} cards "
                   f"({r.applied.get('Good', 0)} Good, {r.applied.get('Again', 0)} Again)."]
        if r.tagged:
            summary.append(f"Tagged {r.tagged} card(s) as not audio-friendly.")
        if r.skipped:
            summary.append("Not applied: " + ", ".join(f"{v} {k}" for k, v in r.skipped.items()) + ".")
        if r.mismatches or r.failures:
            summary.append("\nProblems (please report these):\n" + "\n".join(r.mismatches + r.failures))
            showWarning("\n".join(summary))
        else:
            showInfo("\n".join(summary) + "\n\nSync when you're ready so your phone and AnkiWeb see them.")
        if not r.failures and not (plan.late_days and not allow_late):
            run_helper("delete-grades", *names)

    CollectionOp(parent=mw, op=op).success(done).run_in_background()


def setup_menu() -> None:
    menu = QMenu("Car Review", mw)
    for label, fn in (("Send to Car", send_to_car), ("Apply car grades…", apply_car_grades),
                      ("Pair phone…", pair_phone), ("Status", show_status)):
        action = QAction(label, mw)
        action.triggered.connect(fn)
        menu.addAction(action)
    mw.form.menuTools.addMenu(menu)


setup_menu()
