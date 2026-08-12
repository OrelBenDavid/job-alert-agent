# -*- coding: utf-8 -*-
"""
Reading the minimum required years of experience out of a job description.

This module is pure text analysis: no network, no profile, no state. That
isolation is deliberate - it is the one component in the patch whose bugs
are invisible in production (a bad parse doesn't raise, it silently
withholds a job), so it has to be exhaustively unit-testable on strings
alone. See tests/test_experience.py.

The design rests on four findings from inspecting real postings, each of
which rules out an obvious-looking shortcut:

1. Section headers are written by the recruiter, not the ATS - a real Lever
   posting (Mobileye) titles its requirements block "All you need is:".
   So section detection can NEVER be the mechanism; it is a weak hint only.
2. Hebrew postings mark each bullet inline: "- חובה" / "- יתרון". The signal
   is per-bullet, which is why classification happens per block rather than
   per section.
3. Only ~30% of postings state a number at all (Indeed Hiring Lab, US,
   Apr 2024). "Undetermined" is the majority outcome, not an edge case -
   hence the seniority-signal third state, so the undetermined pile is
   still sortable.
4. Hebrew spelling varies in the wild: both ניסיון and נסיון appear.

Note: the Hebrew strings below are DATA - phrases that actually appear in
Israeli job postings - not comments, and stay in Hebrew regardless of the
project's english-comments convention (same rule as relevance.py).
"""

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from bs4 import BeautifulSoup

# How far a number may sit from an experience keyword and still be read as
# "years of experience". Postings are full of unrelated numbers - company
# age, product versions, team sizes, salary bands - so an unanchored \d+
# would produce confident nonsense.
#
# 50, measured rather than guessed. Swept over 59 real Mobileye postings:
# 40 determined 88% of them, 50 determined 91%, and 60/70/80/100 determined
# no more than 50 did - the curve is completely flat past this point, so
# widening to 50 costs nothing in false rejections. The two postings it
# recovers are both the same real phrasing, where a long qualifier sits
# between the number and the keyword and pushes the gap to 44 chars:
#   "At least 4 years of relevant business analyst or analytical experience"
# At 40 those were sent to the user flagged "no requirement stated" - which
# is precisely the noise this feature exists to remove.
_PROXIMITY_CHARS = 50

# A parsed value above this is not a number of years (it's a calendar year,
# a headcount, a revenue figure). Dropping it yields "undetermined", which
# fails open - the safe direction.
_MAX_PLAUSIBLE_YEARS = 50


BlockKind = Literal["mandatory", "optional", "unknown"]


@dataclass(frozen=True)
class Block:
    """One bullet/paragraph of a description, plus the heading it sits
    under. The heading is carried along only as a weak hint (finding 1);
    everything load-bearing is decided from `text`."""
    text: str
    heading: str = ""


@dataclass(frozen=True)
class ExperienceReading:
    """The whole output of this module.

    min_years is None whenever no mandatory block yielded a number - which
    covers both "the posting says nothing" and "the parser failed". Both
    resolve to PASS upstream, so the two cases don't need distinguishing.
    has_seniority_signals is what separates a genuinely silent posting from
    one that clearly wants a veteran but never wrote a number."""
    min_years: float | None
    has_seniority_signals: bool = False


# --------------------------------------------------------------------------
# C. Structural parsing - parse HTML, THEN strip. Never the reverse.
# --------------------------------------------------------------------------

# Tags that carry one logical bullet/paragraph of prose.
_BLOCK_TAGS = ("p", "li")
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

# A short line ending in a colon is a heading no matter which tag it uses -
# this is what catches "All you need is:" wrapped in <p><strong>, which no
# amount of h1-h6 matching would find.
_HEADING_MAX_CHARS = 90

# Bullet glyphs used when the description turns out not to be HTML at all.
_BULLET_GLYPHS = "•▪◦‣∙*"


def _clean(text: str) -> str:
    """Normalizes one block of text for both matching and storage.

    NFKC folds the full-width/compatibility forms that HTML descriptions
    pick up; the bidi control characters (RLM/LRM/embedding marks) are
    invisible but land *between* a number and its unit in Hebrew text,
    which would break every regex here if left in place."""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[‎‏‪-‮⁦-⁩]", "", text)
    text = text.replace("\xa0", " ")            # &nbsp; is extremely common in ATS HTML
    return re.sub(r"[ \t]+", " ", text).strip()


def _looks_like_html(text: str) -> bool:
    """Whether to hand the text to the HTML path. Checked on the content
    itself rather than trusting the profile's content_is_html flag alone -
    a mis-set flag should degrade to the line-splitting fallback, not
    produce zero blocks."""
    return bool(re.search(r"<(p|li|ul|ol|div|br|h[1-6])\b[^>]*>", text or "",
                          re.IGNORECASE))


def _is_heading_text(text: str) -> bool:
    """The tag-independent half of heading detection: short, ends with a
    colon, and isn't itself a requirement sentence."""
    return len(text) <= _HEADING_MAX_CHARS and text.endswith(":")


def _blocks_from_html(html_text: str) -> list[Block]:
    """Walks the parsed document in order, emitting one Block per bullet or
    paragraph and remembering the most recent heading.

    Document order is the whole point: flattening to text and splitting on
    newlines loses the bullet boundaries that per-bullet classification
    (section D) depends on, and ATS HTML frequently has no newlines at all."""
    soup = BeautifulSoup(html_text, "html.parser")

    blocks: list[Block] = []
    current_heading = ""

    for el in soup.find_all(list(_HEADING_TAGS) + list(_BLOCK_TAGS) + ["div"]):
        if el.name == "div":
            # Only leaf-ish divs count. Some boards use <div> per line with no
            # <p>/<li> at all; taking every div would otherwise re-emit each
            # wrapper's entire subtree as a block and turn one bullet into ten.
            if el.find(list(_BLOCK_TAGS) + list(_HEADING_TAGS) + ["div"]):
                continue

        text = _clean(el.get_text(" ", strip=True))
        if not text:
            continue

        if el.name in _HEADING_TAGS or (el.name != "li" and _is_heading_text(text)):
            # Headings are not blocks: a line reading "Requirements:" states no
            # requirement of its own, and emitting it would add noise to the
            # classifier for zero information.
            current_heading = text
            continue

        blocks.append(Block(text=text, heading=current_heading))

    return blocks


def _blocks_from_plain_text(text: str) -> list[Block]:
    """Fallback for descriptions that genuinely aren't HTML. Splits on line
    breaks and on bullet glyphs, since plain-text descriptions routinely put
    several bullets on one line."""
    text = _clean(text).replace("\r", "\n")
    pieces: list[str] = []
    for line in text.split("\n"):
        # Glyph bullets are unambiguous, so they may split mid-line. A DASH
        # MAY NOT: Hebrew postings mark each bullet inline as "... – חובה" /
        # "... – יתרון", so splitting on a mid-line dash would tear the
        # יתרון marker off its own bullet and promote an explicitly-optional
        # "5+ years" line into a mandatory one - the exact false rejection
        # per-bullet classification exists to prevent. A dash is therefore
        # only ever stripped as a leading list marker, never used to split.
        for piece in re.split(rf"[{re.escape(_BULLET_GLYPHS)}]", line):
            pieces.append(re.sub(r"^\s*[-–—]\s+", "", piece))

    blocks: list[Block] = []
    current_heading = ""
    for piece in pieces:
        piece = _clean(piece)
        if not piece:
            continue
        if _is_heading_text(piece):
            current_heading = piece
            continue
        blocks.append(Block(text=piece, heading=current_heading))
    return blocks


def parse_blocks(description: str) -> list[Block]:
    """Description -> ordered blocks. The single entry point for section C."""
    if not description or not description.strip():
        return []
    if _looks_like_html(description):
        return _blocks_from_html(description)
    return _blocks_from_plain_text(description)


# --------------------------------------------------------------------------
# D. Bullet-level classification
# --------------------------------------------------------------------------

# Checked FIRST, and wins over the mandatory markers: a line reading
# "5+ years - יתרון" is an advantage, not a requirement, even though the
# recruiter also wrote the word "required" three bullets up. Discarding
# these outright is what stops a "5+ years preferred" line inside a
# nice-to-have list from disqualifying a genuinely entry-level role.
_OPTIONAL_PATTERNS = [
    r"יתרון",
    r"\bpreferred\b", r"\ba plus\b", r"\bis a plus\b", r"\bbonus\b",
    r"\bnice[ -]to[ -]have\b", r"\ban? advantage\b", r"\badvantage\b",
    r"\bwould be great\b",
]

_MANDATORY_PATTERNS = [
    r"חובה",
    r"\brequired\b", r"\brequirements?\b", r"\bmust have\b", r"\bmust\b",
]

# Weak, best-effort heading heuristic - deliberately last in the chain and
# never the primary mechanism (finding 1). "All you need is:" is matched by
# the "you need" alternative; a header the recruiter invented from scratch
# will simply leave the block "unknown", which fails open.
_REQUIREMENTS_HEADING_PATTERNS = [
    r"requirements?", r"qualifications?", r"you need", r"you'?ll need",
    r"what you bring", r"you bring", r"who you are", r"must have",
    r"skills", r"experience",
    r"דרישות", r"מה נדרש", r"דרישות התפקיד", r"הדרישות",
]

_OPTIONAL_RE = re.compile("|".join(_OPTIONAL_PATTERNS), re.IGNORECASE)
_MANDATORY_RE = re.compile("|".join(_MANDATORY_PATTERNS), re.IGNORECASE)
_REQ_HEADING_RE = re.compile("|".join(_REQUIREMENTS_HEADING_PATTERNS), re.IGNORECASE)


def classify_block(block: Block) -> BlockKind:
    """mandatory / optional / unknown for a single block."""
    if _OPTIONAL_RE.search(block.text):
        return "optional"
    if _MANDATORY_RE.search(block.text):
        return "mandatory"
    if block.heading and _REQ_HEADING_RE.search(block.heading):
        # Promotion by heading, the weak path: an unmarked bullet sitting
        # under a requirements-looking header is treated as a requirement.
        return "mandatory"
    return "unknown"


# --------------------------------------------------------------------------
# E. Year extraction
# --------------------------------------------------------------------------

# The anchor every number must sit near. Both Hebrew spellings, per finding 4.
# \bexp\b matches the "exp." abbreviation without also firing inside
# "expert"/"expertise" (there is no word boundary after "exp" in those).
_EXPERIENCE_KEYWORD_RE = re.compile(
    r"experience|\bexp\b\.?|ניסיון|נסיון", re.IGNORECASE)

# "year" / "years" / "yr" / "yrs" / "yrs." / "year's" / "years'". The \b sits
# immediately after the letters rather than at the very end, so the trailing
# apostrophe and period stay optional without breaking the boundary check
# ("24 mos. of..." has no word boundary after the dot).
_YEAR_UNIT = r"y(?:ea)?rs?\b(?:['’]s?)?\.?"
_MONTH_UNIT = r"(?:months?\b|mos?\b\.?|חודשים\b|חודש\b)"

# The generic numeric form. A single \d+ pattern by design - enumerating
# "2+|3+|4+...10+" as literals silently misses "12+", and misses it in
# exactly the direction that matters (a job that should be rejected).
# The optional middle group swallows the upper end of a range ("3-5 years",
# "3 to 5 years") so the captured value is always the LOWER endpoint.
_RANGE_TAIL = r"(?:\s*(?:[-–—]|to|up to)\s*\d+(?:\.\d+)?)?"
_NUM_YEARS_RE = re.compile(
    rf"(\d+(?:\.\d+)?)\s*\+?{_RANGE_TAIL}\s*{_YEAR_UNIT}", re.IGNORECASE)
_NUM_MONTHS_RE = re.compile(
    rf"(\d+(?:\.\d+)?)\s*\+?{_RANGE_TAIL}\s*{_MONTH_UNIT}", re.IGNORECASE)

# A bare "5+" sitting next to an experience keyword with no unit word at all
# ("Experience: 5+"). This one gets a much tighter proximity budget than the
# rest: with nothing but a "+" to identify it, a 40-char window would also
# swallow "10+ engineers on the team" one clause away from the word
# "experience" and reject a job over a headcount.
_BARE_PLUS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+")
# Measured against the real false positive it has to exclude: "Experience
# working with 10+ engineers on the team" leaves a 14-char gap, while the form
# this pattern exists for ("Experience: 5+") leaves 2. 8 sits between them.
_BARE_PLUS_PROXIMITY_CHARS = 8

_ENGLISH_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_WORD_YEARS_RE = re.compile(
    rf"\b({'|'.join(_ENGLISH_WORD_NUMBERS)})\s+{_YEAR_UNIT}", re.IGNORECASE)

# Hebrew numeric: "3 שנות ניסיון", "3-5 שנים", "שנתיים" is handled separately
# because it is a dual form with no separate number word.
_HEB_NUM_YEARS_RE = re.compile(
    rf"(\d+(?:\.\d+)?)\s*\+?{_RANGE_TAIL}\s*שנ(?:ות|ים|ה)")

# The single-word quantities. The optional [כלב] prefix is why these can't be
# plain \bשנה\b: Hebrew glues prepositions onto the word, so "כשנה" ("about a
# year") and "לשנה" are one token each. ה is excluded on purpose - "השנה"
# means "this year", not "a year of experience".
_HEB_SIMPLE_YEARS_RE = re.compile(r"\b[כלב]?(שנתיים|שנה)\b")
_HEB_SIMPLE_VALUES = {"שנה": 1.0, "שנתיים": 2.0}

# Spelled-out Hebrew counts: "שלוש שנים", "שלושה שנים". Both the feminine and
# masculine forms appear in postings even though only one is grammatical.
_HEB_COUNT_VALUES = {
    "שתי": 2.0, "שתיים": 2.0, "שלוש": 3.0, "שלושה": 3.0, "ארבע": 4.0,
    "ארבעה": 4.0, "חמש": 5.0, "חמישה": 5.0, "שש": 6.0, "שישה": 6.0,
    "שבע": 7.0, "שבעה": 7.0, "שמונה": 8.0, "תשע": 9.0, "תשעה": 9.0,
    "עשר": 10.0, "עשרה": 10.0,
}
_HEB_COUNT_YEARS_RE = re.compile(
    rf"\b[כלב]?({'|'.join(sorted(_HEB_COUNT_VALUES, key=len, reverse=True))})\s+שנים\b")

# Explicit "no experience needed" phrasing. These resolve to 0.0 WITH
# confidence - not to None - because "we checked and it needs nothing" is a
# different fact from "we don't know", and the user should see it as such.
# Exempt from the proximity rule: they are self-anchoring.
_ZERO_EXPERIENCE_PATTERNS = [
    r"ללא ניסיון", r"ללא נסיון", r"ללא צורך בניסיון", r"ללא צורך בנסיון",
    r"לא נדרש ניסיון", r"לא נדרש נסיון", r"אין צורך בניסיון",
    r"בוגרי קורס", r"בוגר קורס",
    r"\bno experience\b", r"\bno prior experience\b",
    r"\bno previous experience\b", r"\bexperience not required\b",
    # "entry-level" is deliberately absent: ~35% of postings labelled
    # entry-level still demand 3+ years (LinkedIn analysis), so the label
    # carries no information and would manufacture a confident 0.0.
]
_ZERO_EXPERIENCE_RE = re.compile("|".join(_ZERO_EXPERIENCE_PATTERNS),
                                 re.IGNORECASE)

# G. Seniority signals: phrasing that clearly wants a veteran but states no
# number. These never produce a value - they only move a posting from
# "nothing said" to "something said, just not a number", so the undetermined
# pile stays skimmable.
_SENIORITY_SIGNAL_PATTERNS = [
    r"\bproven experience\b", r"\bextensive experience\b",
    r"\bdeep expertise\b", r"\bdeep experience\b",
    r"\bdemonstrated experience\b", r"\btrack record\b",
    r"\bsolid background\b", r"\bstrong background\b",
    # Degree markers are spelled out tightly on purpose: a loose \bms\b would
    # fire on "MS Office" / "MS SQL" and flag half the postings as senior.
    r"\bph\.?\s?d\b", r"\bm\.s\.?\b", r"\bm\.?sc\b", r"\bmaster'?s degree\b",
    r"\badvanced degree\b", r"\bgraduate degree\b",
    r"ניסיון מוכח", r"נסיון מוכח", r"ניסיון עשיר", r"ותק",
]
_SENIORITY_SIGNAL_RE = re.compile("|".join(_SENIORITY_SIGNAL_PATTERNS),
                                  re.IGNORECASE)


def _keyword_spans(text: str) -> list[tuple[int, int]]:
    """Every position an experience keyword occupies in the block."""
    return [(m.start(), m.end()) for m in _EXPERIENCE_KEYWORD_RE.finditer(text)]


def _is_near_keyword(span: tuple[int, int], keywords: list[tuple[int, int]],
                     budget: int) -> bool:
    """Whether a candidate number sits within `budget` characters of any
    experience keyword. Overlap counts as distance 0 (the keyword can be
    inside the matched phrase, as in 'ניסיון של שנתיים')."""
    start, end = span
    for k_start, k_end in keywords:
        if k_end <= start:
            gap = start - k_end
        elif end <= k_start:
            gap = k_start - end
        else:
            gap = 0
        if gap <= budget:
            return True
    return False


def _candidates(text: str) -> list[tuple[tuple[int, int], float, int]]:
    """All (span, years, proximity_budget) triples the patterns can find,
    before proximity is applied. Patterns are allowed to overlap - "5+ years"
    is matched by both the unit form and the bare-plus form, yielding the same
    value twice, which the eventual min() makes harmless."""
    found: list[tuple[tuple[int, int], float, int]] = []

    for regex in (_NUM_YEARS_RE, _HEB_NUM_YEARS_RE):
        for m in regex.finditer(text):
            found.append(((m.start(), m.end()), float(m.group(1)),
                          _PROXIMITY_CHARS))

    for m in _BARE_PLUS_RE.finditer(text):
        found.append(((m.start(), m.end()), float(m.group(1)),
                      _BARE_PLUS_PROXIMITY_CHARS))

    for m in _NUM_MONTHS_RE.finditer(text):
        # "24 months" is a 2-year requirement written the long way.
        found.append(((m.start(), m.end()), float(m.group(1)) / 12.0,
                      _PROXIMITY_CHARS))

    for m in _WORD_YEARS_RE.finditer(text):
        found.append(((m.start(), m.end()),
                      float(_ENGLISH_WORD_NUMBERS[m.group(1).lower()]),
                      _PROXIMITY_CHARS))

    for m in _HEB_SIMPLE_YEARS_RE.finditer(text):
        found.append(((m.start(), m.end()), _HEB_SIMPLE_VALUES[m.group(1)],
                      _PROXIMITY_CHARS))

    for m in _HEB_COUNT_YEARS_RE.finditer(text):
        found.append(((m.start(), m.end()), _HEB_COUNT_VALUES[m.group(1)],
                      _PROXIMITY_CHARS))

    return found


def extract_years(text: str) -> float | None:
    """The minimum number of years this single block demands, or None.

    Minimum rather than maximum within a block, on purpose: a bullet listing
    several figures ("5 years in X, 3 years in Y") is ambiguous, and the
    lower reading is the one that fails open. Ranges reduce to their lower
    endpoint by the same rule - "1-3" passes, "2-4" rejects."""
    text = _clean(text)
    if not text:
        return None

    values: list[float] = []

    if _ZERO_EXPERIENCE_RE.search(text):
        # Self-anchoring, and 0.0 will win any min() anyway.
        values.append(0.0)

    keywords = _keyword_spans(text)
    if keywords:
        for span, value, budget in _candidates(text):
            if value > _MAX_PLAUSIBLE_YEARS:
                continue          # a calendar year or a headcount, not a duration
            if _is_near_keyword(span, keywords, budget):
                values.append(value)

    return min(values) if values else None


def has_seniority_signals(text: str) -> bool:
    """Whether the block wants a veteran without saying how many years."""
    return bool(_SENIORITY_SIGNAL_RE.search(_clean(text)))


# --------------------------------------------------------------------------
# F. Decision
# --------------------------------------------------------------------------

def read_experience(description: str | None) -> ExperienceReading:
    """Description -> ExperienceReading. The entry point the filter calls.

    The minimum is taken across MANDATORY blocks only. Taking it across the
    whole text was an earlier, cruder workaround for the preferred-vs-required
    problem; per-bullet classification supersedes it, and mixing the two would
    re-admit exactly the "5+ years preferred" false rejection that D exists
    to prevent."""
    if not description:
        return ExperienceReading(min_years=None, has_seniority_signals=False)

    blocks = parse_blocks(description)

    mandatory_years: list[float] = []
    signals = False

    for block in blocks:
        kind = classify_block(block)
        if kind == "optional":
            continue          # discarded entirely, before year extraction

        # Signals are collected from everything that survived the optional
        # cut, not just mandatory blocks: the flag is purely informational
        # (all three states still send), so a wider net costs nothing and
        # makes the undetermined pile more useful.
        if has_seniority_signals(block.text):
            signals = True

        if kind != "mandatory":
            continue

        years = extract_years(block.text)
        if years is not None:
            mandatory_years.append(years)

    return ExperienceReading(
        min_years=min(mandatory_years) if mandatory_years else None,
        has_seniority_signals=signals,
    )
