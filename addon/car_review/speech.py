"""Turn a cloze card into what Car Review says out loud.

Pure Python with no Anki imports, so it can be tested and previewed outside Anki.

    spoken = to_speech(text_field_html, cloze_number)
    spoken.question  -> list of Segment: plain text to say, or a blank (with an optional hint)
    spoken.answers   -> one string per blank, in order
    spoken.skip      -> reason the card doesn't work as audio, or None
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

MAX_QUESTION_WORDS = 60


@dataclass
class Segment:
    text: str = ""
    blank: bool = False
    hint: str | None = None


@dataclass
class Spoken:
    question: list[Segment] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    skip: str | None = None
    # Which conversions fired, for the preview report.
    changes: set[str] = field(default_factory=set)

    def question_text(self) -> str:
        """The question as one readable line, with blanks shown as [blank] or [which drug?]."""
        out = []
        for seg in self.question:
            out.append(f"[{hint_phrase(seg.hint) if seg.hint else 'blank'}]" if seg.blank else seg.text)
        return " ".join(s for s in out if s).strip()


def hint_phrase(hint: str) -> str:
    """How a cloze hint is said in place of the blank: 'drug' -> 'which drug?', '2' -> 'name two'."""
    h = hint.strip().rstrip("?.").strip()
    if re.fullmatch(r"\d+", h):
        n = int(h)
        return f"name {NUMBER_WORDS[n] if n < len(NUMBER_WORDS) else h}"
    if re.match(r"(?i)(which|what|how|who|where|when|why|name|list)\b", h) or re.match(r"\d+(st|nd|rd|th)\b", h):
        return h + "?"
    if not h[:2].isupper():  # keep acronyms like "MOA" as they are
        h = h[0].lower() + h[1:]
    return f"which {h}?"


# --- Skipping -----------------------------------------------------------------------

VISUAL_WORDS = re.compile(
    r"\b(shown|pictured|picture|image|labeled|labelled|this structure|the structure indicated|"
    r"identify the|arrow points?|indicated by)\b",
    re.I,
)


def skip_reason(front_html: str, answers_html: list[str]) -> str | None:
    if re.search(r"<img\b", front_html, re.I):
        return "image on front"
    if re.search(r"<table\b", front_html, re.I):
        return "table on front"
    if "[sound:" in front_html:
        return "audio on front"
    if any(re.search(r"<img\b", a, re.I) for a in answers_html):
        return "answer is an image"
    if VISUAL_WORDS.search(_strip_tags(front_html)):
        return "refers to a picture"
    return None


# --- Cloze parsing ------------------------------------------------------------------

CLOZE = re.compile(r"\{\{c(\d+)::((?:(?!\}\}).)*?)(?:::((?:(?!\}\}).)*?))?\}\}", re.S)
BLANK_MARK = "\u0001{}\u0001"  # survives the text clean-up untouched
BLANK_SPLIT = re.compile("\u0001(\\d+)\u0001")


def to_speech(front_html: str, cloze_number: int) -> Spoken:
    spoken = Spoken()
    answers_html: list[str] = []
    hints: list[str | None] = []

    def replace(m: re.Match) -> str:
        if int(m.group(1)) != cloze_number:
            return m.group(2)  # other clozes are read as normal text
        answers_html.append(m.group(2))
        hints.append(m.group(3) or None)
        return f" {BLANK_MARK.format(len(answers_html) - 1)} "

    with_marks = CLOZE.sub(replace, front_html)
    if not answers_html:
        spoken.skip = "no blank for this card"
        return spoken
    spoken.skip = skip_reason(front_html, answers_html)

    text = speakable(with_marks, spoken.changes)
    for i, piece in enumerate(BLANK_SPLIT.split(text)):
        if i % 2 == 0:
            piece = piece.lstrip(" ,.;").rstrip(" ,;")
            if piece:
                spoken.question.append(Segment(text=piece))
        else:
            hint = hints[int(piece)]
            spoken.question.append(
                Segment(blank=True, hint=speakable(hint, spoken.changes) if hint else None)
            )
    spoken.answers = [speakable(a, spoken.changes) for a in answers_html]

    words = len(spoken.question_text().split())
    if spoken.skip is None and words > MAX_QUESTION_WORDS:
        spoken.skip = f"question too long ({words} words)"
    return spoken


# --- HTML to speakable text ---------------------------------------------------------

IONS = {
    "Na+": "sodium", "K+": "potassium", "Cl-": "chloride", "Ca2+": "calcium", "Mg2+": "magnesium",
    "HCO3-": "bicarbonate", "H+": "hydrogen ion", "Fe2+": "ferrous iron", "Fe3+": "ferric iron",
    "Zn2+": "zinc", "Cu2+": "copper", "NH4+": "ammonium", "PO43-": "phosphate", "Li+": "lithium",
}

# Whole-word replacements. Keys are matched case-sensitively unless marked otherwise.
WORDS = {
    "Tx": "treatment", "Dx": "diagnosis", "Sx": "symptoms", "Hx": "history", "Fx": "fracture",
    "DDx": "differential diagnosis", "Rx": "prescription", "pt": "patient", "pts": "patients",
    "yo": "year old", "y/o": "year old", "w/": "with", "w/o": "without", "b/c": "because",
    "s/p": "status post", "h/o": "history of", "c/o": "complaining of", "vs": "versus",
    "vs.": "versus", "e.g.": "for example", "e.g.,": "for example,", "i.e.": "that is",
    "i.e.,": "that is,", "approx.": "approximately", "AKA": "also known as", "aka": "also known as",
    "MCC": "most common cause", "DOC": "drug of choice", "MOA": "mechanism of action",
    "FiO2": "F I O 2", "PaO2": "P A O 2", "PaCO2": "P A C O 2", "SpO2": "S P O 2",
    "CO2": "C O 2", "O2": "oxygen", "H2O": "water", "HbA1c": "hemoglobin A 1 C",
}

UNITS = [
    (r"mg/dL", "milligrams per deciliter"), (r"g/dL", "grams per deciliter"),
    (r"mEq/L", "milliequivalents per liter"), (r"mmol/L", "millimoles per liter"),
    (r"mm ?Hg", "millimeters of mercury"), (r"cm ?H2O", "centimeters of water"),
    (r"mcg|µg|μg", "micrograms"), (r"mg/kg", "milligrams per kilogram"), (r"mL/min", "milliliters per minute"),
    (r"U/L", "units per liter"), (r"IU", "international units"), (r"bpm", "beats per minute"),
    (r"/min", "per minute"), (r"kg", "kilograms"), (r"mg", "milligrams"), (r"mL", "milliliters"),
]

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9,
         "X": 10, "XI": 11, "XII": 12, "XIII": 13}
NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                "ten", "eleven", "twelve", "thirteen"]
ROMAN_CONTEXT = re.compile(
    r"\b((?i:type|types|class|factor|factors|phase|stage|grade|complex|collagen|Mobitz|zone|lamina|"
    r"cranial nerve|Le Fort|degree)|CN)\s+((?:XIII|XII|XI|X|IX|VIII|VII|VI|V|IV|III|II|I)"
    r"(?:\s*(?:,|and|or|-|–|/)\s*(?:XIII|XII|XI|X|IX|VIII|VII|VI|V|IV|III|II|I))*)\b"
)

SYMBOLS = [
    ("+/-", " with or without "), ("±", " with or without "), ("↑↑", " greatly increased "),
    ("↓↓", " greatly decreased "), ("↑", " increased "), ("↓", " decreased "),
    ("→", " leads to "), ("⟶", " leads to "), ("⇒", " leads to "), ("←", " from "),
    ("≥", " greater than or equal to "), ("≤", " less than or equal to "),
    ("≈", " about "), ("~", " about "), ("&", " and "), ("%", " percent"), ("°C", " degrees Celsius"),
    ("°F", " degrees Fahrenheit"), ("°", " degrees"), ("×", " times "), ("−", "-"), ("–", "-"), ("—", ", "),
    ("α", "alpha"), ("β", "beta"), ("γ", "gamma"), ("δ", "delta"), ("κ", "kappa"), ("λ", "lambda"),
    ("μ", "mu"), ("µ", "mu"),
]


def _strip_tags(h: str) -> str:
    return re.sub(r"<[^>]+>", " ", h)


def _roman_to_words(m: re.Match, changes: set[str]) -> str:
    changes.add("roman numerals")
    word = "cranial nerve" if m.group(1) == "CN" else m.group(1)
    numerals = re.split(r"(\s*(?:,|and|or|-|–|/)\s*)", m.group(2))
    spoken = []
    for part in numerals:
        p = part.strip()
        if p in ROMAN:
            spoken.append(NUMBER_WORDS[ROMAN[p]])
        elif p in ("-", "–"):
            spoken.append(" to ")
        elif p == "/":
            spoken.append(" or ")
        else:
            spoken.append(part)
    return f"{word} {''.join(spoken)}"


def _drop_repeated_abbreviation(text: str, changes: set[str]) -> str:
    """'creatine kinase (CK)' -> 'creatine kinase' when the letters match the words before."""

    def check(m: re.Match) -> str:
        abbr = m.group(2)
        words = re.findall(r"[A-Za-z][A-Za-z-]*", m.group(1))[-(len(abbr) + 3):]
        initials = "".join(w[0].upper() for w in words)
        letters = [c for c in abbr.upper() if c.isalpha()]
        it = iter(initials)
        if letters and all(c in it for c in letters) and letters[-1] == initials[-1:]:
            changes.add("repeated abbreviation")
            return m.group(1)
        return m.group(0)

    return re.sub(r"((?:\b[\w-]+\s+){0,8}[\w-]+)\s*\(([A-Za-z0-9]{2,7})\)", check, text)


def speakable(h: str, changes: set[str] | None = None) -> str:
    changes = changes if changes is not None else set()
    t = h
    # Superscripts/subscripts become plain characters so ions and formulas can be matched.
    t = re.sub(r"<sup>\s*(.*?)\s*</sup>", lambda m: _sup(m.group(1), changes), t, flags=re.S | re.I)
    t = re.sub(r"<sub>\s*(.*?)\s*</sub>", r"\1", t, flags=re.S | re.I)
    # Line breaks and block ends become sentence pauses.
    t = re.sub(r"(?i)<br\s*/?>|</?div[^>]*>|</?p[^>]*>|</li>|<li[^>]*>", " ; ", t)
    t = _strip_tags(t)
    t = html.unescape(t).replace("\xa0", " ")

    for ion, name in IONS.items():
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(ion)}(?![A-Za-z0-9])", t):
            changes.add("ions")
            t = re.sub(rf"(?<![A-Za-z0-9]){re.escape(ion)}(?![A-Za-z0-9])", name, t)
    for unit, name in UNITS:
        pattern = rf"(?<=\d)\s*(?:{unit})\b|(?<![A-Za-z/])(?:{unit})\b"
        if re.search(pattern, t):
            changes.add("units")
            t = re.sub(pattern, f" {name}", t)
    if re.search(r"\d\s*[-–]\s*\d", t):
        changes.add("ranges")
        t = re.sub(r"(\d)\s*[-–]\s*(\d)", r"\1 to \2", t)
    t = ROMAN_CONTEXT.sub(lambda m: _roman_to_words(m, changes), t)
    for sym, words in SYMBOLS:
        if sym in t:
            changes.add("symbols")
            t = t.replace(sym, words)
    # Tags are already gone, so any remaining < or > is a comparison.
    if "<" in t or ">" in t:
        changes.add("symbols")
        t = t.replace(">", " greater than ").replace("<", " less than ")
    # A dash starting a line is a list bullet, not a word.
    t = re.sub(r"(^|;)\s*[-•*]\s+", r"\1 ", t)

    def word(m: re.Match) -> str:
        w = m.group(0)
        if w in WORDS:
            changes.add("abbreviations")
            return WORDS[w]
        if w.endswith(".") and w[:-1] in WORDS:  # "Tx." at the end of a sentence
            changes.add("abbreviations")
            return WORDS[w[:-1]] + "."
        return w

    t = re.sub(r"(?<![\w/])[A-Za-z][\w./]*(?![\w])", word, t)
    # A slash between two words reads as "or"; leftover slashes elsewhere as "per".
    t = re.sub(r"(?<=[A-Za-z])\s*/\s*(?=[A-Za-z])", " or ", t)
    t = t.replace("/", " per ")

    t = _drop_repeated_abbreviation(t, changes)
    # Parentheses become short pauses.
    t = re.sub(r"\s*\(\s*", ", ", t)
    t = re.sub(r"\s*\)\s*", ", ", t)
    # Tidy punctuation and spacing.
    t = re.sub(r"\s*;\s*(;\s*)*", ". ", t)
    t = re.sub(r"\s+([,.?!:])", r"\1", t)
    t = re.sub(r"([,.?!:])(?:\s*[,.])+", r"\1", t)
    t = re.sub(r",\s*([?.!:])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" ,.;")


def _sup(content: str, changes: set[str]) -> str:
    c = _strip_tags(content).strip()
    if re.fullmatch(r"\d*[+-−]", c):
        return c.replace("−", "-")  # ion charge, e.g. Ca<sup>2+</sup> -> Ca2+
    if c in ("st", "nd", "rd", "th"):
        return c  # 1<sup>st</sup> -> 1st
    changes.add("superscripts")
    return f" to the {c}" if c.isdigit() else f" {c}"
