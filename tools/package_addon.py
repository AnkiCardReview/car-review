"""Build the add-on file for AnkiWeb or for installing from a file.

    python tools/package_addon.py            -> dist/car_review.ankiaddon

The .ankiaddon file is a zip of addon/car_review (without caches or your meta.json, which
holds your own settings). It includes a manifest.json so double-clicking the file installs it
as "car_review"; AnkiWeb ignores the manifest and uses its own add-on number.
Nothing is uploaded: publishing on AnkiWeb is done by hand at https://ankiweb.net/shared/upload.
"""

from __future__ import annotations

import json
import subprocess
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "addon" / "car_review"
OUT = ROOT / "dist" / "car_review.ankiaddon"
VERSION = "0.1.0"
MIN_POINT_VERSION = 241100  # Anki 24.11 (tested with 26.09)
SKIP_DIRS = {"__pycache__"}
SKIP_FILES = {"meta.json", "manifest.json"}  # your settings; the manifest is written fresh
SKIP_SUFFIXES = {".pyc", ".pyo"}


def commit_time() -> int:
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=ROOT, capture_output=True, text=True, check=True)
        return int(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return int(time.time())


def files() -> list[Path]:
    found = []
    for path in sorted(SOURCE.rglob("*")):
        rel = path.relative_to(SOURCE)
        if path.is_dir() or any(p in SKIP_DIRS for p in rel.parts):
            continue
        if rel.name in SKIP_FILES or path.suffix in SKIP_SUFFIXES:
            continue
        found.append(path)
    return found


def main() -> None:
    manifest = {
        "package": "car_review",
        "name": "Car Review: hands-free reviews while you drive",
        "mod": commit_time(),
        "min_point_version": MIN_POINT_VERSION,
        "human_version": VERSION,
        "homepage": "https://github.com/AnkiCardReview/car-review",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for path in files():
            z.write(path, path.relative_to(SOURCE).as_posix())
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
    names = zipfile.ZipFile(OUT).namelist()
    assert "__init__.py" in names and "config.json" in names and not any("meta.json" in n for n in names)
    print(f"{OUT} ({OUT.stat().st_size / 1e3:.0f} KB, {len(names)} files, version {VERSION})")


if __name__ == "__main__":
    main()
