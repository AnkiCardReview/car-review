"""A small window showing what the Car Review helper is doing, so it's never an invisible
background process: progress, time left, an activity log, and Pause / Stop.

The work runs on a background thread; the window runs on the main thread and reads shared
progress a few times a second. If no window can be shown (no display), the work runs without one.

Stop is quick: making one clip (or loading the voice) can take a minute on a slow laptop, so the
worker runs those through Progress.run, which gives up on them as soon as Stop is pressed. The
clip being made is simply not saved; everything finished before it is kept.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque


class Stopped(Exception):
    """Stop was pressed while a clip was being made."""


class Progress:
    """Shared progress between the worker thread and the window (or nothing, when headless)."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.unit = "clips"            # what done/total count
        self.done = 0
        self.total = 0
        self.phase = ""                # what's happening right now, e.g. "Loading the voice"
        self.note = ""                 # a warning line, e.g. "Paused: plug in to continue"
        self.stats = {"made": 0, "ready": None, "uploads": 0}
        self.events: list[tuple[float, str, str]] = []  # (time, text, kind) for the activity log
        self.finished = False
        self.failed = False
        self.result = ""               # final message
        self.auto_close = False        # close the window shortly after finishing (quick sends)
        self.paused = False            # paused by the user
        self.stopped = False           # stop asked by the user
        self.started = time.time()
        self._recent: deque[float] = deque(maxlen=40)  # times of the last finished items
        self._resume = threading.Event()
        self._resume.set()

    # ----- called by the worker -----
    def step(self, done: int, total: int) -> None:
        self.done, self.total = done, total
        self._recent.append(time.time())

    def event(self, text: str, kind: str = "info", to_log: bool = True) -> None:
        """Add a line to the activity log (kind: info, ok, warn, error, quiet). Also printed to
        helper.log unless to_log is False (per-card lines would only clutter it)."""
        self.events.append((time.time(), text, kind))
        if to_log:
            line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}"
            try:
                print(line, flush=True)
            except UnicodeEncodeError:  # a console or pipe that can't show "✓" or "▸"
                print(line.encode("ascii", "replace").decode(), flush=True)
            except (OSError, ValueError, AttributeError):  # no usable output at all
                pass

    def checkpoint(self) -> bool:
        """Block while paused. Returns False when the user asked to stop."""
        self._resume.wait()
        return not self.stopped

    def run(self, fn, *args):
        """fn(*args) on a side thread, given up on as soon as Stop is pressed (raises Stopped).
        A clip abandoned this way finishes unseen and is thrown away, or ends with the process."""
        if self.stopped:
            raise Stopped
        box: dict = {}

        def target() -> None:
            try:
                box["value"] = fn(*args)
            except BaseException as err:  # handed to the worker below
                box["error"] = err

        side = threading.Thread(target=target, daemon=True)
        side.start()
        while side.is_alive():
            side.join(0.1)
            if self.stopped:
                raise Stopped
        if "error" in box:
            raise box["error"]
        return box["value"]

    def finish(self, message: str, failed: bool = False) -> None:
        self.result = message
        self.failed = failed
        self.finished = True
        self.event(message, "error" if failed else ("warn" if self.stopped else "ok"))

    # ----- called by the window -----
    def toggle_pause(self) -> None:
        self.paused = not self.paused
        if self.paused:
            self._resume.clear()
            self.event("Paused by you. The clip being made is finished first.", "warn")
        else:
            self._recent.clear()  # don't count the pause in the speed estimate
            self._resume.set()
            self.event("Carrying on.")

    def stop(self) -> None:
        if not self.stopped:
            self.stopped = True
            self.event("Stopping. Everything made so far is kept.", "warn")
        self._resume.set()

    def seconds_left(self) -> float | None:
        if len(self._recent) < 3 or not self.total:
            return None
        per_item = (self._recent[-1] - self._recent[0]) / (len(self._recent) - 1)
        return per_item * (self.total - self.done)


def fmt_duration(secs: float) -> str:
    mins = int(round(secs / 60))
    if mins < 1:
        return "less than a minute"
    if mins < 60:
        return f"{mins} min"
    return f"{mins // 60} h {mins % 60:02d} min"


def fmt_clock(secs: float) -> str:
    secs = int(secs)
    return f"{secs // 3600}:{secs // 60 % 60:02d}:{secs % 60:02d}" if secs >= 3600 else f"{secs // 60}:{secs % 60:02d}"


# Colors and fonts (light, like a system dialog).
BG, PANEL, BORDER = "#ffffff", "#f3f5f8", "#dfe3e9"
TEXT, MUTED = "#1b1f24", "#5f6b7a"
ACCENT, OK, WARN, ERROR = "#2f6fed", "#1f8a4c", "#a8620b", "#c53030"
PILLS = {  # state -> (text color, background)
    "Working": (ACCENT, "#e8f0fe"), "Paused": (WARN, "#fdf3e1"), "Stopping": (MUTED, "#eceff3"),
    "Stopped": (MUTED, "#eceff3"), "Done": (OK, "#e5f4eb"), "Error": (ERROR, "#fde8e8"),
}
if sys.platform == "win32":
    SANS, MONO = "Segoe UI", "Consolas"
elif sys.platform == "darwin":
    SANS, MONO = "Helvetica Neue", "Menlo"
else:
    SANS, MONO = "DejaVu Sans", "DejaVu Sans Mono"
QUIET_NOTE = "Runs at low priority, so you can keep using your laptop."


def confirm_stop(root) -> bool:
    from tkinter import messagebox

    return messagebox.askyesno("Stop making audio?",
                               "Stop now? Everything already made is kept, and next time it carries on from here.",
                               parent=root)


def run_with_window(progress: Progress, work) -> None:
    """Run work(progress) on a thread while showing the progress window."""
    thread = threading.Thread(target=work, args=(progress,), daemon=True)
    try:
        if sys.platform == "win32":
            import ctypes

            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled screens
            except (AttributeError, OSError):
                pass
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
    except Exception:
        thread.start()
        thread.join()
        return

    scale = root.winfo_fpixels("1i") / 96

    def px(n: float) -> int:
        return int(round(n * scale))

    root.title(progress.title)
    root.configure(bg=BG)
    root.geometry(f"{px(560)}x{px(500)}")
    root.minsize(px(460), px(400))
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Bar.Horizontal.TProgressbar", troughcolor=PANEL, background=ACCENT, bordercolor=PANEL,
                    lightcolor=ACCENT, darkcolor=ACCENT, thickness=px(8))
    style.configure("TButton", font=(SANS, 10), padding=(px(14), px(5)), background=PANEL, foreground=TEXT,
                    bordercolor=BORDER, lightcolor=PANEL, darkcolor=PANEL, focuscolor=PANEL)
    style.map("TButton", background=[("disabled", BG), ("active", "#e6eaf0")],
              foreground=[("disabled", "#a0a8b3")])
    style.configure("Primary.TButton", background=ACCENT, foreground="#ffffff", bordercolor=ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, focuscolor=ACCENT)
    style.map("Primary.TButton", background=[("active", "#285fcc")])
    style.configure("Log.Vertical.TScrollbar", background="#d5dbe3", troughcolor=PANEL, bordercolor=PANEL,
                    arrowcolor=MUTED, lightcolor="#d5dbe3", darkcolor="#d5dbe3", gripcount=0)

    page = tk.Frame(root, bg=BG, padx=px(20), pady=px(16))
    page.pack(fill="both", expand=True)
    page.columnconfigure(0, weight=1)

    top = tk.Frame(page, bg=BG)
    top.grid(row=0, column=0, sticky="ew")
    top.columnconfigure(0, weight=1)
    heading = tk.Label(top, font=(SANS, 14, "bold"), bg=BG, fg=TEXT, anchor="w")
    heading.grid(row=0, column=0, sticky="w")
    pill = tk.Label(top, text="Working", font=(SANS, 9, "bold"), padx=px(10), pady=px(2))
    pill.grid(row=0, column=1, sticky="e")
    note = tk.Label(page, text=QUIET_NOTE, font=(SANS, 9), bg=BG, fg=MUTED, anchor="w", justify="left")
    note.grid(row=1, column=0, sticky="ew", pady=(px(2), px(12)))

    bar = ttk.Progressbar(page, style="Bar.Horizontal.TProgressbar", mode="determinate", maximum=100)
    bar.grid(row=2, column=0, sticky="ew")
    line = tk.Frame(page, bg=BG)
    line.grid(row=3, column=0, sticky="ew", pady=(px(6), px(12)))
    line.columnconfigure(0, weight=1)
    counts = tk.Label(line, text="Starting…", font=(SANS, 10), bg=BG, fg=TEXT, anchor="w")
    counts.grid(row=0, column=0, sticky="w")
    eta = tk.Label(line, text="", font=(SANS, 10), bg=BG, fg=MUTED, anchor="e")
    eta.grid(row=0, column=1, sticky="e")

    tiles = tk.Frame(page, bg=BG)
    tiles.grid(row=4, column=0, sticky="ew")
    tile_values = {}
    for col, (key, label) in enumerate((("made", "clips made"), ("ready", "cards on phone"),
                                        ("uploads", "audio uploads"), ("elapsed", "elapsed"))):
        tiles.columnconfigure(col, weight=1, uniform="tile")
        tile = tk.Frame(tiles, bg=PANEL, padx=px(12), pady=px(8))
        tile.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else px(4), 0 if col == 3 else px(4)))
        tile_values[key] = tk.Label(tile, text="–", font=(SANS, 15, "bold"), bg=PANEL, fg=TEXT, anchor="w")
        tile_values[key].pack(fill="x")
        tk.Label(tile, text=label, font=(SANS, 9), bg=PANEL, fg=MUTED, anchor="w").pack(fill="x")

    tk.Label(page, text="Activity", font=(SANS, 10, "bold"), bg=BG, fg=TEXT, anchor="w").grid(
        row=5, column=0, sticky="ew", pady=(px(14), px(4)))
    box = tk.Frame(page, bg=BG, highlightbackground=BORDER, highlightthickness=1)
    box.grid(row=6, column=0, sticky="nsew")
    page.rowconfigure(6, weight=1)
    activity = tk.Text(box, font=(MONO, 9), bg=BG, fg=TEXT, relief="flat", wrap="word", height=8,
                       padx=px(10), pady=px(8), borderwidth=0, cursor="arrow", spacing1=px(1), spacing3=px(1))
    scroll = ttk.Scrollbar(box, orient="vertical", command=activity.yview, style="Log.Vertical.TScrollbar")
    activity.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    activity.pack(side="left", fill="both", expand=True)
    activity.tag_configure("time", foreground="#98a1ad")
    for kind, color in (("info", TEXT), ("ok", OK), ("warn", WARN), ("error", ERROR), ("quiet", MUTED)):
        activity.tag_configure(kind, foreground=color)
    activity.configure(state="disabled")

    buttons = tk.Frame(page, bg=BG)
    buttons.grid(row=7, column=0, sticky="ew", pady=(px(14), 0))
    pause = ttk.Button(buttons, text="Pause", command=lambda: (progress.toggle_pause(), refresh(once=True)))
    pause.pack(side="left")
    stop = ttk.Button(buttons, text="Stop", command=lambda: ask_stop())
    stop.pack(side="left", padx=(px(8), 0))
    close = ttk.Button(buttons, text="Close", style="Primary.TButton", command=root.destroy)
    keep = ttk.Button(buttons, text="Keep open", command=lambda: keep_open())

    shown = 0
    ended = False       # the finished state has been drawn
    closing_at = None   # when the window closes by itself

    def keep_open() -> None:
        nonlocal closing_at
        closing_at = None
        keep.pack_forget()

    def ask_stop() -> None:
        if progress.finished:
            root.destroy()
        elif progress.stopped or confirm_stop(root):
            progress.stop()
            refresh(once=True)

    def set_pill(state: str) -> None:
        fg, bg = PILLS[state]
        pill.config(text=state, fg=fg, bg=bg)

    def add_events() -> None:
        nonlocal shown
        new = progress.events[shown:]
        if not new:
            return
        shown += len(new)
        at_end = activity.yview()[1] > 0.999
        activity.configure(state="normal")
        for when, text, kind in new:
            activity.insert("end", time.strftime("%H:%M:%S  ", time.localtime(when)), "time")
            activity.insert("end", text + "\n", kind)
        lines = int(activity.index("end-1c").split(".")[0])
        if lines > 600:
            activity.delete("1.0", f"{lines - 500}.0")
        activity.configure(state="disabled")
        if at_end:
            activity.see("end")

    def refresh(once: bool = False) -> None:
        nonlocal closing_at, ended
        root.title(progress.title)
        heading.config(text=progress.title.removeprefix("Car Review: ").capitalize())
        add_events()
        stats = progress.stats
        tile_values["made"].config(text=f"{stats['made']:,}")
        tile_values["ready"].config(text="–" if stats["ready"] is None else f"{stats['ready']:,}")
        tile_values["uploads"].config(text=f"{stats['uploads']:,}")
        if progress.total:
            bar["value"] = 100 * progress.done / progress.total
        if progress.finished:
            if not ended:
                ended = True
                bar["value"] = 100 if not progress.stopped else bar["value"]
                set_pill("Error" if progress.failed else "Stopped" if progress.stopped else "Done")
                counts.config(text=progress.result.split(". ")[0].rstrip(".") + ".")
                eta.config(text=f"Took {fmt_duration(time.time() - progress.started)}")
                tile_values["elapsed"].config(text=fmt_clock(time.time() - progress.started))
                note.config(text=QUIET_NOTE if not progress.failed else "Details are in CarReview/helper.log.",
                            fg=MUTED)
                pause.pack_forget()
                stop.pack_forget()
                close.pack(side="right")
                if progress.stopped:
                    closing_at = time.time() + 1.5  # Stop means "close": just show it stopped
                elif progress.auto_close and not progress.failed:
                    closing_at = time.time() + 8
                    keep.pack(side="right", padx=(0, px(8)))
            if closing_at is not None:
                left = closing_at - time.time()
                if left <= 0:
                    root.destroy()
                    return
                if not progress.stopped:
                    eta.config(text=f"Closing in {int(left) + 1} s")
            if not once:
                root.after(250, refresh)
            return
        tile_values["elapsed"].config(text=fmt_clock(time.time() - progress.started))
        if progress.total:
            counts.config(text=f"{progress.done:,} of {progress.total:,} {progress.unit} · "
                               f"{int(100 * progress.done / progress.total)}%")
        elif progress.phase:
            counts.config(text=progress.phase)
        if progress.stopped:
            set_pill("Stopping")
            eta.config(text="Finishing up…")
            pause.config(state="disabled")
            stop.config(state="disabled")
        elif progress.paused:
            set_pill("Paused")
            eta.config(text="Paused by you")
            pause.config(text="Resume")
        else:
            set_pill("Paused" if progress.note else "Working")
            pause.config(text="Pause")
            left = progress.seconds_left()
            if progress.phase and progress.total:
                eta.config(text=progress.phase)
            elif left is not None:
                eta.config(text=f"About {fmt_duration(left)} left")
            else:
                eta.config(text="Working out time left…" if progress.total else "")
        note.config(text=progress.note or QUIET_NOTE, fg=WARN if progress.note else MUTED)
        if not once:
            root.after(250, refresh)

    root.protocol("WM_DELETE_WINDOW", ask_stop)
    thread.start()
    root.after(100, refresh)
    root.mainloop()
    # Window closed: if the work is still going (closing asked to stop), let it wrap up.
    thread.join(timeout=60)
