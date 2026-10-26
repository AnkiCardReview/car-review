"""One-click voice setup: installs the helper's own Python, the voice engine and the voice.

Runs inside Anki (only needs `requests`, which Anki includes), on a background thread, and
reports progress through a callback. Everything goes into the CarReview folder in your home
folder; nothing is installed system-wide and no administrator rights are needed:

    CarReview/tools/uv     uv, a small installer for Python packages (from Astral, open source)
    CarReview/python/      a private copy of Python 3.12, downloaded by uv
    CarReview/venv/        the voice engine (ONNX Runtime, kokoro-onnx, misaki) and helpers
    CarReview/voice/       the Kokoro-82M model and the Heart voice

Every download is checked against a fixed SHA-256 fingerprint (packages through
voice-requirements.txt), so a tampered file is refused. Running it again repairs or finishes
an interrupted setup; finished parts are skipped.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Callable

from .common import HOME, VENV, VOICE_DIR
from .voice import MODEL_FILE, VOICES_FILE

ADDON_DIR = Path(__file__).resolve().parent
REQUIREMENTS = ADDON_DIR / "voice-requirements.txt"
TOOLS = HOME / "tools"

UV_VERSION = "0.12.24"
UV_ASSETS = {  # (platform, machine) -> (file, sha256)
    ("win32", "amd64"): ("uv-x86_64-pc-windows-msvc.zip", "7c38608c8a18ee137d748a1773053b07ec8f3a30fab49aebaa6f4e4efeceb019"),
    ("win32", "arm64"): ("uv-aarch64-pc-windows-msvc.zip", "4b783bda5cc44bbae0651a837223873a7acee31152381aef5fc86ef6bef25997"),
    ("darwin", "arm64"): ("uv-aarch64-apple-darwin.tar.gz", "0c4346de7abdb49495b393b9ec809fe387aa43e586be20fecb972216c1e71732"),
    ("darwin", "x86_64"): ("uv-x86_64-apple-darwin.tar.gz", "4fa82e37cb94767661f532b001e470b67a186c7260e305bd84ddb78fd545c0b6"),
    ("linux", "x86_64"): ("uv-x86_64-unknown-linux-gnu.tar.gz", "b4dfaef47d491a7296981f8374a4595f55dbf84e8937c8ecd2983574d8bb3da6"),
    ("linux", "aarch64"): ("uv-aarch64-unknown-linux-gnu.tar.gz", "5231be65f496304623895dacdbf1de8504fec90303684bdf05805aa34414dd21"),
}
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
MODEL_FILES = {  # file -> (sha256, size in bytes)
    # Half precision: the same speed on a laptop processor as the full model, at half the size,
    # and closer to it than the 8-bit version.
    MODEL_FILE: ("c1610a859f3bdea01107e73e50100685af38fff88f5cd8e5c56df109ec880204", 177464787),
    VOICES_FILE: ("bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d", 28214398),
}
# Shown before anything is downloaded, so nobody is surprised (measured on Windows; Macs are similar).
DOWNLOAD_MB = 340  # uv 16 + Python 25 + packages 92 + model 177 + voices 28
DISK_MB = 650

# Progress callback: (step label, fraction done 0..1 or None when it can't be measured).
Report = Callable[[str, "float | None"], None]


class Cancelled(Exception):
    pass


class SetupError(Exception):
    """A problem explained in plain words (the details are in CarReview/setup.log)."""


def helper_python(windowless: bool = False) -> Path:
    if sys.platform == "win32":
        # pythonw.exe runs without a console window; used for the helper's own progress window.
        return VENV / ("Scripts/pythonw.exe" if windowless else "Scripts/python.exe")
    return VENV / "bin/python"


def _platform_key() -> tuple[str, str]:
    system = "win32" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
    machine = platform.machine().lower()
    machine = {"x86_64": "amd64" if system == "win32" else "x86_64", "aarch64": "arm64" if system != "linux" else "aarch64",
               "arm64": "arm64" if system != "linux" else "aarch64", "amd64": "amd64" if system == "win32" else "x86_64"}.get(machine, machine)
    return system, machine


def supported() -> str | None:
    """None if voice setup works on this computer, else why not."""
    if _platform_key() not in UV_ASSETS:
        return f"Voice setup doesn't support this computer yet ({platform.system()} {platform.machine()})."
    return None


def _uv_path() -> Path:
    return TOOLS / ("uv.exe" if sys.platform == "win32" else "uv")


def _log(text: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / "setup.log", "a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")


def _download(url: str, sha256: str, label: str, report: Report, cancelled: Callable[[], bool], size: int = 0) -> bytes:
    import requests

    digest = hashlib.sha256()
    buf = io.BytesIO()
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or size or 0)
            for chunk in r.iter_content(1 << 16):
                if cancelled():
                    raise Cancelled()
                buf.write(chunk)
                digest.update(chunk)
                if total:
                    report(f"{label} ({buf.tell() / 1e6:.0f} of {total / 1e6:.0f} MB)", buf.tell() / total)
    except requests.RequestException as err:
        _log(f"download failed: {url}: {err}")
        raise SetupError("Couldn't download the voice files. Check your internet connection and try again.") from err
    if digest.hexdigest() != sha256:
        _log(f"fingerprint mismatch for {url}: {digest.hexdigest()}")
        raise SetupError("A downloaded file didn't match its expected fingerprint, so it wasn't used. Try again later.")
    return buf.getvalue()


def _download_to_file(url: str, path: Path, sha256: str, size: int, label: str, report: Report,
                      cancelled: Callable[[], bool]) -> None:
    """Large files go straight to disk (under a temporary name until they check out)."""
    import requests

    if path.exists() and _file_sha256(path) == sha256:
        return
    part = path.with_name(path.name + ".part")
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    done = 0
    try:
        with requests.get(url, stream=True, timeout=60) as r, open(part, "wb") as f:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or size or 0)
            for chunk in r.iter_content(1 << 20):
                if cancelled():
                    raise Cancelled()
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if total:
                    report(f"{label} ({done / 1e6:.0f} of {total / 1e6:.0f} MB)", done / total)
    except requests.RequestException as err:
        _log(f"download failed: {url}: {err}")
        raise SetupError("Couldn't download the voice. Check your internet connection and try again.") from err
    if digest.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        _log(f"fingerprint mismatch for {url}: {digest.hexdigest()}")
        raise SetupError("The downloaded voice didn't match its expected fingerprint, so it wasn't used. Try again later.")
    os.replace(part, path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _env() -> dict:
    """Keep uv's Python and settings inside the CarReview folder, away from anything else."""
    env = dict(os.environ)
    env.update({
        "UV_PYTHON_INSTALL_DIR": str(HOME / "python"),
        "UV_NO_CACHE": "1",          # nothing extra left behind on disk
        "UV_NO_CONFIG": "1",         # ignore any uv settings on this computer
        "VIRTUAL_ENV": "",
    })
    return env


def _run(args: list, label: str, report: Report, cancelled: Callable[[], bool]) -> None:
    report(label, None)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    _log("$ " + " ".join(str(a) for a in args))
    proc = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=_env(), cwd=str(HOME), creationflags=flags, encoding="utf-8", errors="replace")
    for line in proc.stdout:
        _log(line)
        if cancelled():
            proc.kill()
            raise Cancelled()
    if proc.wait() != 0:
        raise SetupError(f"{label.rstrip('…')} didn't finish. Try again; if it keeps failing, see CarReview/setup.log.")


def install_uv(report: Report, cancelled: Callable[[], bool]) -> Path:
    uv = _uv_path()
    if uv.exists():
        return uv
    name, sha = UV_ASSETS[_platform_key()]
    data = _download(f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{name}", sha,
                     "Downloading the installer", report, cancelled)
    TOOLS.mkdir(parents=True, exist_ok=True)
    exe = uv.name
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(m for m in z.namelist() if m.rsplit("/", 1)[-1] == exe)
            uv.write_bytes(z.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = next(m for m in t.getmembers() if m.name.rsplit("/", 1)[-1] == exe)
            uv.write_bytes(t.extractfile(member).read())
        uv.chmod(0o755)
    return uv


def installed_engine(python: Path | None = None) -> str | None:
    """Which voice engine the helper's Python has ("onnx" or "torch"), or None if not set up.
    Takes a second or two (it starts that Python), so call it off the main thread."""
    py = python or helper_python()
    if not py.exists():
        return None
    try:
        r = subprocess.run([str(py), "-m", f"{ADDON_DIR.name}.helper", "engine"], capture_output=True, text=True,
                           timeout=60, cwd=str(ADDON_DIR.parent),
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    engine = r.stdout.strip()
    return engine if r.returncode == 0 and engine in ("onnx", "torch") else None


def install(report: Report, cancelled: Callable[[], bool] = lambda: False) -> None:
    """Install (or repair) everything the voice needs. Raises SetupError or Cancelled."""
    reason = supported()
    if reason:
        raise SetupError(reason)
    HOME.mkdir(parents=True, exist_ok=True)
    _log("---- voice setup")
    uv = install_uv(report, cancelled)
    if not helper_python().exists():
        _run([uv, "venv", "--python", "3.12", "--managed-python", VENV],
             "Downloading Python (about 25 MB)…", report, cancelled)
    _run([uv, "pip", "install", "--python", helper_python(), "--require-hashes", "--no-deps", "--only-binary", ":all:",
          "-r", REQUIREMENTS], "Installing the voice engine (about 1–3 minutes)…", report, cancelled)
    for i, (name, (sha, size)) in enumerate(MODEL_FILES.items(), 1):
        _download_to_file(MODEL_URL + name, VOICE_DIR / name, sha, size,
                          f"Downloading the Heart voice, part {i} of {len(MODEL_FILES)}", report, cancelled)
    report("Testing the voice…", None)
    check = subprocess.run([str(helper_python()), "-m", f"{ADDON_DIR.name}.helper", "voice-check"],
                           capture_output=True, text=True, timeout=600, cwd=str(ADDON_DIR.parent),
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    _log(check.stdout + check.stderr)
    if check.returncode != 0:
        raise SetupError("The voice was installed but didn't pass its test. See CarReview/setup.log for details.")
    report("Voice ready ✓", 1.0)


def remove() -> None:
    """Delete the voice engine and model (audio already made and the pairing are kept)."""
    for path in (VENV, HOME / "python", VOICE_DIR, TOOLS):
        shutil.rmtree(path, ignore_errors=True)
