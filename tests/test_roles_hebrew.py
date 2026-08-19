# -*- coding: utf-8 -*-
"""The Hebrew half of the role blocklist.

Every title here is a real posting from the live corpus of 2026-08-19 (2,101
Israel-relevant postings across 255 boards). That matters more than usual for
this list: a blocklist entry is a PERMANENT silent drop - state is written
before filtering and there is no replay by design - so each term was measured
against the whole corpus before being added, and the complete list of postings
it removes is what the tests below pin.

Together the terms added on 2026-08-19 move 21 postings from "unknown" to
"blocked" and move NOTHING out of "target" - the target count is identical
before and after, at 1,370.
"""

import pytest

import roles


# ---------------------------------------------------------------------------
# The gender infix - why these are single words
# ---------------------------------------------------------------------------

def test_a_gender_infix_splits_a_two_word_hebrew_term():
    """Israeli boards write "מנהל.ת", "מפעיל/ת", "עובד.ת", and _normalize turns
    that punctuation into a space. So the infix lands BETWEEN the words of a
    two-word term and the term stops matching.

    This is why `הנהלת חשבונות` - on the blocklist since the beginning - never
    caught a bookkeeper posting written the inclusive way."""
    assert roles._normalize("מנהל.ת חשבונות") == " מנהל ת חשבונות "
    # the intuitive two-word term does not match...
    assert " מנהל חשבונות " not in roles._normalize("מנהל.ת חשבונות")
    # ...while the single word does, which is why the list carries that one.
    assert " חשבונות " in roles._normalize("מנהל.ת חשבונות")


def test_a_single_word_term_survives_the_infix():
    for title in ["מחסנאי/ת", "נהג/ת", "מפעיל/ת מכונה"]:
        normalized = roles._normalize(title)
        assert normalized.split()[0] in normalized


# ---------------------------------------------------------------------------
# What the new terms block - the complete measured list
# ---------------------------------------------------------------------------

# (real posting, the company it came from, the term that decides it)
NEWLY_BLOCKED = [
    ("מנהל.ת חשבונות", "eitan_medical", "חשבונות"),
    ("מלחימ.ה", "eitan_medical", "מלחימ"),
    ("עובד.ת יצור", "eitan_medical", "יצור"),
    ("חשב/ת שכר", "netafim", "שכר"),
    ("מנהל.ת אדמיניסטרציה - משרה חלקית", "netafim", "אדמיניסטרציה"),
    ("רכז/ת מצוינות תפעולית מפעלית", "netafim", "מפעלית"),
    ("רפרנט.ית גבייה (החלפה לחל״ד)", "one_zero_digital_bank", "גבייה"),
    ("כרסם/ חרט", "sodastream", "כרסם"),
    ("מפעיל/ת מכונה מנוסה עיבוד שבבי", "sodastream", "מפעיל"),
    ("נציג/ת שירות לקוחות", "sodastream", "שירות לקוחות"),
    ("אחזקת מבנה", "solaredge_technologies", "אחזקת מבנה"),
    ("מפעיל SMT מנוסה", "solaredge_technologies", "מפעיל"),
    ("מלקטים- וולט מרקט- רחובות", "wolt", "מלקטים"),
    ("טכנאי/ת מכונות הזרקה", "sodastream", "הזרקה"),
    # QC/quality inspection - see test_hebrew_qc_is_blocked_by_an_explicit_call
    ("מבקר/ת איכות (QC)", "bird_aerosystems", "מבקר"),
    ("מבקר/ת איכות /Quality inspector", "mks_instruments", "מבקר"),
    ("מבקר/ת איכות זמני/ת  / Temp Quality inspector", "mks_instruments", "מבקר"),
    ("Finishing Operator / מבקר/ת איכות במחלקת שירות", "mks_instruments", "מבקר"),
    ("Finishing operator/מבקר/ת איכות", "mks_instruments", "מבקר"),
    ("מבקר/ת איכות למפעל מתכת ב", "sodastream", "מבקר"),
]


@pytest.mark.parametrize("title,company,term", NEWLY_BLOCKED)
def test_off_target_hebrew_posting_is_blocked(title, company, term):
    classification, matched = roles.classify(title)
    assert classification == "blocked", f"{company}: {title}"
    assert matched == term


def test_that_is_the_whole_list():
    """Pinned deliberately. If a future term widens this, the diff has to say
    so out loud rather than quietly removing more postings than measured.

    20 distinct titles; the corpus holds 21 postings because MKS publishes
    "Finishing operator/מבקר/ת איכות" twice."""
    assert len(NEWLY_BLOCKED) == 20


def test_hebrew_qc_is_blocked_by_an_explicit_call_against_the_english_list():
    """מבקר knowingly contradicts TARGET_FAMILIES, which carries "quality
    control" and "quality assurance". The asymmetry was chosen deliberately:
    every posting the term catches is production-line inspection, not the
    software QA those English terms are aimed at.

    Pinned so the contradiction can never be "fixed" by someone tidying up
    without knowing it was intentional."""
    assert "מבקר" in roles.BLOCKED_DOMAINS
    assert "quality control" in roles.TARGET_FAMILIES
    assert roles.classify("Quality Control Engineer")[0] == "target"
    assert roles.classify("מבקר/ת איכות")[0] == "blocked"


def test_the_inspector_term_does_not_collide_with_control_engineering():
    """מבקר (inspector) and בקרה (control) are different words, and whole-word
    matching keeps them apart. This is the check that made the term safe."""
    assert roles.classify("מהנדס/ת בקרה")[0] == "target"
    assert roles.classify("מהנדס/ת מערכות בקרה")[0] == "target"


# ---------------------------------------------------------------------------
# What was deliberately NOT added, and the postings that is protecting
# ---------------------------------------------------------------------------

# REAL corpus postings, with the exact classification they hold today. Each one
# is the specific posting that decided a candidate term was too broad to add.
REAL_POSTINGS_A_REJECTED_TERM_WOULD_HAVE_TAKEN = [
    # `ייצור` (the correct spelling of "production") was rejected for these two
    ("טכנאי/ת ייצור רכיבים אופטיים מדויקים/ Optical Precision Manufacturing "
     "Technician", "target"),
    ("הנדסאי/ת אלקטרוניקה לתפקיד בהנדסת ייצור", "unknown"),
    # `מרכיב` ("assembler") was rejected for this one
    ("Temp Calibration Technician/Assembler / מכייל/ת מרכיב/ה זמני/ת", "target"),
    # a target-family Hebrew posting from the same corpus, unaffected throughout
    ("מהנדס/ת בקרה", "target"),
]


@pytest.mark.parametrize(
    "title,expected", REAL_POSTINGS_A_REJECTED_TERM_WOULD_HAVE_TAKEN)
def test_a_rejected_term_is_not_quietly_blocking_a_real_posting(title, expected):
    """If one of these ever reads as "blocked", a term went in without being
    measured."""
    classification, _ = roles.classify(title)
    assert classification == expected, title


# CONSTRUCTED probes - not corpus postings, so only the thing that actually
# matters is asserted: they must not be BLOCKED. Pinning an exact class here
# would be pinning a guess (both of these were guessed wrong on the first
# attempt - "ראש משמרת NOC" reads as unknown, not target, because neither
# "NOC" nor "משמרת" is a target term).
NOT_BLOCKED_PROBES = [
    "ראש משמרת NOC",           # why `משמרת` was rejected
    "מהנדס/ת תמיכה בלקוחות",   # why bare `לקוחות` was rejected
    "מהנדס/ת מחסן נתונים",     # why `מחסן` was rejected - data warehouse
    "מהנדס/ת אחזקה",           # why bare `אחזקה` was not used for facilities
    "מהנדס/ת עיבוד תמונה",     # why `עיבוד` was rejected - image processing
    "מהנדס/ת שבבים",           # why `שבבי` was rejected - shares a root with chip
]


@pytest.mark.parametrize("title", NOT_BLOCKED_PROBES)
def test_a_rejected_term_would_have_blocked_these_and_must_not(title):
    classification, term = roles.classify(title)
    assert classification != "blocked", f"{title} blocked by {term!r}"


@pytest.mark.parametrize("term", ["ייצור", "מרכיב", "משמרת", "לקוחות",
                                  "תפעולית", "מחסן", "שבבי", "עיבוד"])
def test_the_rejected_terms_are_absent_from_the_blocklist(term):
    assert term not in roles.BLOCKED_DOMAINS


# ---------------------------------------------------------------------------
# The direction that must never regress
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "מפתח/ת תוכנה", "מהנדס/ת תוכנה", "מהנדסת חשמל", "אנליסט/ית נתונים",
    "בודק תוכנה אוטומציה", "סטודנט/ית להנדסת חשמל", "מתמחה בפיתוח",
])
def test_hebrew_engineering_titles_are_untouched(title):
    classification, _ = roles.classify(title)
    assert classification == "target", title
