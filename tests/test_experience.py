# -*- coding: utf-8 -*-
"""
Exhaustive tests for the experience parser.

This module gets the heaviest test coverage in the project on purpose: it is
the one component whose bugs are invisible in production. A bad parse doesn't
raise and doesn't show up in any log - it silently withholds a job, and the
user simply never learns the posting existed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from experience import (Block, classify_block, extract_years,
                        has_seniority_signals, parse_blocks, read_experience)


# ---------------------------------------------------------------------------
# E. Year extraction - English, numeric
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("3+ years of experience", 3.0),
    ("2 + years of experience", 2.0),              # stray space around the +
    ("3-5 years of experience", 3.0),              # range -> LOWER endpoint
    ("3 to 5 years of experience", 3.0),
    ("at least 2 years of experience", 2.0),
    ("minimum of 4 years experience", 4.0),
    ("min. 3 years of experience", 3.0),
    ("2 yrs experience", 2.0),
    ("2 yrs. of experience", 2.0),
    ("2 years' experience", 2.0),
    ("1 year of experience", 1.0),
    ("Experience: 5+", 5.0),                       # bare +, no unit word
    ("12+ years of experience", 12.0),             # two digits - the case an
                                                    # enumerated 2+..10+ list misses
])
def test_english_numeric_forms(text, expected):
    assert extract_years(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("two years of experience", 2.0),
    ("three years of experience", 3.0),
    ("ten years of experience", 10.0),
])
def test_english_word_quantities(text, expected):
    assert extract_years(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("24 months of experience", 2.0),
    ("18 months of experience", 1.5),
    ("6 months of experience", 0.5),
])
def test_months_convert_to_years(text, expected):
    assert extract_years(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("1-3 years of experience", 1.0),   # passes a 1-year threshold
    ("2-4 years of experience", 2.0),   # rejects it
])
def test_ranges_always_take_the_lower_endpoint(text, expected):
    assert extract_years(text) == expected


# ---------------------------------------------------------------------------
# E. Year extraction - the numbers that must NOT be read as years
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "We are a team of 200 people",                  # no experience keyword
    "Experience with Python 3 and Django 4",        # version numbers... near a
                                                     # keyword, but no unit word
    "MS Office experience",
    "Experience working with 10+ engineers on the team",   # headcount, not tenure
    "Our product serves 5000 customers daily",
    "השנה פתחנו מחלקה חדשה",                        # "this year", not "a year"
    "ניסיון בפיתוח",                                 # experience, no quantity
])
def test_unrelated_numbers_are_not_read_as_years(text):
    assert extract_years(text) is None


def test_absurd_values_are_dropped_rather_than_trusted():
    """A calendar year next to an experience keyword must yield undetermined
    (which passes), never a confident rejection."""
    assert extract_years("Experience since 2015 years") is None


def test_number_far_from_any_keyword_is_ignored():
    far = ("5 years " + "x" * 80 + " experience")
    assert extract_years(far) is None


def test_a_long_qualifier_between_the_number_and_the_keyword_still_counts():
    """Taken verbatim from a live Mobileye posting. The gap here is 44
    characters, which a 40-char window missed - the job was being sent
    flagged "no requirement stated" while plainly demanding 4 years."""
    text = ("At least 4 years of relevant business analyst or analytical "
            "experience, ideally in automotive or commercial analysis")
    assert extract_years(text) == 4.0


# ---------------------------------------------------------------------------
# E. Year extraction - Hebrew, BOTH spellings throughout
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("ניסיון של שנה", 1.0),
    ("נסיון של שנה", 1.0),                  # no yud
    ("ניסיון של שנתיים", 2.0),
    ("נסיון של שנתיים", 2.0),
    ("לפחות שנה ניסיון", 1.0),
    ("לפחות שנתיים ניסיון", 2.0),
    ("כשנה ניסיון", 1.0),                   # prefix כ = "about"
    ("כשנתיים ניסיון", 2.0),
    ("3 שנות ניסיון", 3.0),
    ("5 שנות נסיון", 5.0),
    ("ניסיון של שלוש שנים", 3.0),
    ("ניסיון של שלושה שנים", 3.0),          # masculine form appears too
    ("ניסיון של חמש שנים", 5.0),
    ("3-5 שנות ניסיון", 3.0),
    ("ניסיון של 6 חודשים", 0.5),
])
def test_hebrew_forms(text, expected):
    assert extract_years(text) == expected


def test_hebrew_verbal_range_takes_the_minimum():
    assert extract_years("ניסיון של שנה עד שנתיים") == 1.0


@pytest.mark.parametrize("text", [
    "ללא ניסיון",
    "ללא נסיון",
    "ללא צורך בניסיון קודם",
    "לא נדרש ניסיון",
    "בוגרי קורס",
    "no experience required",
    "no prior experience",
    "no previous experience needed",
])
def test_zero_experience_phrases_are_a_confident_zero(text):
    """0.0, not None: "we checked and it needs nothing" is a different fact
    from "we don't know", and the user should see the difference."""
    assert extract_years(text) == 0.0


def test_entry_level_label_alone_is_not_a_zero():
    """~35% of postings labelled entry-level still demand 3+ years, so the
    label must not manufacture a confident 0.0."""
    assert extract_years("This is an entry-level position") is None


# ---------------------------------------------------------------------------
# C. Structural parsing
# ---------------------------------------------------------------------------

def test_html_bullets_become_separate_blocks_under_their_heading():
    html = ("<h3>Requirements</h3><ul>"
            "<li>3+ years of experience</li>"
            "<li>B.Sc. in Computer Science</li></ul>")
    blocks = parse_blocks(html)
    assert [b.text for b in blocks] == ["3+ years of experience",
                                        "B.Sc. in Computer Science"]
    assert all(b.heading == "Requirements" for b in blocks)


def test_a_bolded_colon_line_is_a_heading_not_a_bullet():
    """The real Lever case: the requirements block is titled "All you need
    is:" in a <p><strong>, which no h1-h6 match would ever find."""
    html = ("<p><strong>All you need is:</strong></p>"
            "<ul><li>2+ years of experience</li></ul>")
    blocks = parse_blocks(html)
    assert [b.text for b in blocks] == ["2+ years of experience"]
    assert blocks[0].heading == "All you need is:"


def test_nested_wrappers_do_not_duplicate_a_bullet():
    html = "<div><div><ul><li>3+ years of experience</li></ul></div></div>"
    assert len(parse_blocks(html)) == 1


def test_plain_text_fallback_splits_on_line_breaks():
    text = "Requirements:\n- 4+ years of experience\n- Java"
    blocks = parse_blocks(text)
    assert [b.text for b in blocks] == ["4+ years of experience", "Java"]
    assert blocks[0].heading == "Requirements:"


def test_plain_text_does_not_split_a_bullet_on_its_inline_hebrew_marker():
    """Hebrew postings mark each bullet inline as "... – יתרון". Splitting on
    that dash would tear the marker off its own bullet and promote an
    explicitly-optional line into a mandatory one."""
    blocks = parse_blocks("- 5 שנות ניסיון ב-AWS – יתרון")
    assert len(blocks) == 1
    assert classify_block(blocks[0]) == "optional"


def test_empty_description_yields_no_blocks():
    assert parse_blocks("") == []
    assert parse_blocks("   ") == []


# ---------------------------------------------------------------------------
# D. Bullet-level classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "5+ years of experience preferred",
    "Kubernetes is a plus",
    "AWS - an advantage",
    "Go - nice to have",
    "Rust would be great",
    "5 שנות ניסיון - יתרון",
])
def test_optional_markers(text):
    assert classify_block(Block(text=text)) == "optional"


@pytest.mark.parametrize("text", [
    "3+ years of experience required",
    "Must have a B.Sc.",
    "ניסיון של שנתיים - חובה",
])
def test_mandatory_markers(text):
    assert classify_block(Block(text=text)) == "mandatory"


def test_optional_wins_over_mandatory_in_the_same_block():
    """A bullet carrying both markers is an advantage - that reading is what
    stops a "required... preferred" line from disqualifying a junior role."""
    block = Block(text="Experience with Kubernetes required - but a plus only")
    assert classify_block(block) == "optional"


def test_unknown_block_is_promoted_by_a_requirements_heading():
    assert classify_block(Block(text="3+ years of experience",
                                heading="What you'll need:")) == "mandatory"


def test_unknown_block_under_an_unrecognized_heading_stays_unknown():
    assert classify_block(Block(text="We offer stock options",
                                heading="About us")) == "unknown"


# ---------------------------------------------------------------------------
# F. Decision - minimum across MANDATORY blocks only
# ---------------------------------------------------------------------------

def test_preferred_years_do_not_disqualify_an_entry_level_role():
    """The single most important behaviour in the module: a "5+ years
    preferred" line in a nice-to-have list must not reject a junior job."""
    html = ("<h3>Requirements</h3><ul>"
            "<li>Up to 1 year of experience - required</li></ul>"
            "<h3>Nice to have</h3><ul>"
            "<li>5+ years of experience with Kubernetes preferred</li></ul>")
    assert read_experience(html).min_years == 1.0


def test_optional_block_alone_yields_undetermined_not_a_number():
    html = "<ul><li>5+ years of experience - an advantage</li></ul>"
    assert read_experience(html).min_years is None


def test_minimum_is_taken_across_mandatory_blocks():
    html = ("<h3>Requirements</h3><ul>"
            "<li>5+ years of experience with Java</li>"
            "<li>2+ years of experience with SQL</li></ul>")
    assert read_experience(html).min_years == 2.0


def test_hebrew_per_bullet_markers_decide_independently():
    html = ("<ul>"
            "<li>ניסיון של שנתיים בפיתוח - חובה</li>"
            "<li>5 שנות ניסיון ב-AWS - יתרון</li></ul>")
    assert read_experience(html).min_years == 2.0


def test_lever_style_posting_with_an_invented_heading():
    """End-to-end on the shape of a real Mobileye/Lever posting."""
    html = ("<div><p>Mobileye is looking for a Data Analyst.</p>"
            "<p><strong>All you need is:</strong></p><ul>"
            "<li>B.Sc. in Industrial Engineering</li>"
            "<li>2+ years of experience with SQL</li>"
            "<li>Experience with Python - an advantage</li></ul></div>")
    reading = read_experience(html)
    assert reading.min_years == 2.0


def test_greenhouse_escaped_html_is_unescaped_then_parsed():
    """Greenhouse's `content` is both HTML and HTML-escaped. detail.py does
    the unescaping; this asserts the parser handles what comes out of it."""
    import html as html_lib
    raw = ("&lt;p&gt;&lt;strong&gt;Requirements:&lt;/strong&gt;&lt;/p&gt;"
           "&lt;ul&gt;&lt;li&gt;3+ years of experience&lt;/li&gt;&lt;/ul&gt;")
    assert read_experience(html_lib.unescape(raw)).min_years == 3.0


def test_a_posting_that_states_nothing_is_undetermined():
    html = "<p>Join our team. We build great things and value curiosity.</p>"
    reading = read_experience(html)
    assert reading.min_years is None
    assert reading.has_seniority_signals is False


def test_none_and_empty_descriptions_are_undetermined():
    assert read_experience(None).min_years is None
    assert read_experience("").min_years is None


# ---------------------------------------------------------------------------
# G. Seniority signals - the third state
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Proven experience leading teams",
    "Extensive experience with distributed systems",
    "Deep expertise in compilers",
    "Demonstrated experience shipping products",
    "Track record of delivery",
    "Solid background in statistics",
    "Ph.D. in Computer Science",
    "M.Sc. in Electrical Engineering",
    "An advanced degree is required",
    "ניסיון מוכח בניהול",
])
def test_seniority_signals_are_detected(text):
    assert has_seniority_signals(text) is True


@pytest.mark.parametrize("text", [
    "Experience with Python",
    "B.Sc. in Computer Science",
    "MS Office and Excel",          # must NOT read as an M.S. degree
])
def test_non_signals(text):
    assert has_seniority_signals(text) is False


def test_signals_without_a_number_produce_the_third_state():
    html = ("<h3>Requirements</h3><ul>"
            "<li>Proven experience with large-scale systems</li></ul>")
    reading = read_experience(html)
    assert reading.min_years is None            # still fails open
    assert reading.has_seniority_signals is True


def test_a_number_and_a_signal_together_still_report_the_number():
    html = ("<h3>Requirements</h3><ul>"
            "<li>Proven experience - 3+ years of experience</li></ul>")
    reading = read_experience(html)
    assert reading.min_years == 3.0
    assert reading.has_seniority_signals is True
