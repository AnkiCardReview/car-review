"""A small window showing what the Car Review helper is doing, so it's never an invisible
background process: progress, time left, and Pause / Stop.

The work runs on a background thread; the window runs on the main thread and reads shared
progress every half second. If no window can be shown (no display), the work runs without one.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class Progress:
    """Shared progress between the worker thread and the window (or nothing, when headless)."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.unit = "clips"            # what done/total count
        self.done = 0
        self.total = 0
        self.note = ""                 # extra line, e.g. "Paused: plug in to continue"
        self.finished = False
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

    def checkpoint(self) -> bool:
        """Block while paused. Returns False when the user asked to stop."""
        self._resume.wait()
        return not self.stopped

    def finish(self, message: str) -> None:
        self.result = message
        self.finished = True

    # ----- called by the window -----
    def toggle_pause(self) -> None:
        self.paused = not self.paused
        if self.paused:
            self._resume.clear()
        else:
            self._recent.clear()  # don't count the pause in the speed estimate
            self._resume.set()

    def stop(self) -> None:
        self.stopped = True
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


def run_with_window(progress: Progress, work) -> None:
    """Run work(progress) on a thread while showing the progress window."""
    thread = threading.Thread(target=work, args=(progress,), daemon=True)
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk

        root = tk.Tk()
    except Exception:
        thread.start()
        thread.join()
        return

    root.title(progress.title)
    root.geometry("440x230")
    root.resizable(False, False)
    pad = {"padx": 16, "pady": 4}
    heading = tk.Label(root, text=progress.title, font=("Segoe UI", 13, "bold"), anchor="w")
    heading.pack(fill="x", padx=16, pady=(14, 4))
    bar = ttk.Progressbar(root, length=400, mode="determinate")
    bar.pack(**pad)
    counts = tk.Label(root, text="Starting… (loading the voice takes about a minute)", anchor="w")
    counts.pack(fill="x", **pad)
    eta = tk.Label(root, text="", anchor="w")
    eta.pack(fill="x", **pad)
    note = tk.Label(root, text="Runs at low priority, so you can keep using your laptop.",
                    anchor="w", fg="#555", wraplength=400, justify="left")
    note.pack(fill="x", **pad)
    buttons = tk.Frame(root)
    buttons.pack(fill="x", padx=16, pady=(8, 12))
    pause = ttk.Button(buttons, text="Pause", command=lambda: (progress.toggle_pause(), refresh()))
    pause.pack(side="left")
    stop = ttk.Button(buttons, text="Stop", command=lambda: ask_stop())
    stop.pack(side="left", padx=8)

    def ask_stop() -> None:
        if progress.finished:
            root.destroy()
            return
        if messagebox.askyesno("Stop making audio?",
                               "Stop now? Everything already made is kept, and next time it carries on from here.",
                               parent=root):
            progress.stop()  # the next refresh shows "Stopping…", then closes the window

    def refresh() -> None:
        heading.config(text=progress.title)
        root.title(progress.title)
        if progress.finished:
            bar["value"] = 100
            counts.config(text=progress.result, wraplength=400, justify="left")
            eta.config(text=f"Took {fmt_duration(time.time() - progress.started)}.")
            note.config(text="This window closes by itself in a few seconds." if progress.auto_close or progress.stopped
                        else "You can close this window.")
            pause.pack_forget()
            stop.config(text="Close", state="normal")
            if progress.auto_close or progress.stopped:
                root.after(3000 if progress.stopped else 6000, root.destroy)
            return
        if progress.stopped:
            # Stopping waits for the clip being made (or the voice still loading) to finish.
            counts.config(text="Stopping after the current clip… everything made so far is kept.")
            eta.config(text="This window closes by itself.")
            pause.config(state="disabled")
            stop.config(state="disabled")
            root.after(500, refresh)
            return
        if progress.total:
            bar["value"] = 100 * progress.done / progress.total
            counts.config(text=f"{progress.done:,} of {progress.total:,} {progress.unit} · {int(100 * progress.done / progress.total)}%")
        left = progress.seconds_left()
        if progress.paused:
            eta.config(text="Paused by you.")
            pause.config(text="Resume")
        else:
            pause.config(text="Pause")
            eta.config(text=f"About {fmt_duration(left)} left" if left is not None else "Working out time left…")
        note.config(text=progress.note or "Runs at low priority, so you can keep using your laptop.")
        root.after(500, refresh)

    root.protocol("WM_DELETE_WINDOW", ask_stop)
    thread.start()
    root.after(500, refresh)
    root.mainloop()
    # Window closed: if the work is still going (closing asked to stop), let it wrap up.
    thread.join(timeout=120)
