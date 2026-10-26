"""Open a temporary copy of an Anki profile's collection with Anki's own engine.

The original collection is never opened. Use as a context manager:

    with collection_copy("My Profile") as col:
        ...
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path

from anki.collection import Collection


@contextlib.contextmanager
def collection_copy(profile: str):
    source = Path(os.environ["APPDATA"]) / "Anki2" / profile
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("collection.anki2", "collection.anki2-wal"):
            if (source / name).exists():
                shutil.copy2(source / name, Path(tmp) / name)
        col = Collection(str(Path(tmp) / "collection.anki2"))
        try:
            yield col
        finally:
            col.close(downgrade=False)
