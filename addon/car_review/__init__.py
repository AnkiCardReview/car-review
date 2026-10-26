"""Car Review add-on for Anki.

This package is also imported outside Anki by the Car Review helper and tools, so the Anki
parts only load when Anki's GUI is present.
"""

try:
    from aqt import mw
except ImportError:  # running outside Anki (helper or tools)
    mw = None

if mw is not None:
    from . import anki_addon  # noqa: F401  (registers the Tools menu)
