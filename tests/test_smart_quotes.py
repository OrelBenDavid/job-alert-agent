# -*- coding: utf-8 -*-
"""The smart-quote fold in experience._clean.

Found 2026-08-18 on a posting the user had actually received. Cognyte's
"Support Continuous Improvement Specialist" states its requirement plainly -
"1-2 years' hands-on experience" - under the heading "For that mission you'll
need:", written with a curly apostrophe the way any rich-text editor produces
it. `_REQUIREMENTS_HEADING_PATTERNS` spells that alternative `you'?ll need`
with a straight apostrophe, so the heading never registered, the bullet under
it stayed "unknown", and the stated number was never read. The alert went out
labelled "no experience requirement stated".

The failure mode is the dangerous one precisely because it is quiet and always
fails OPEN: a posting demanding five years reads as undetermined and is sent.
"""

import re

import pytest

from experience import read_experience, _REQ_HEADING_RE, _clean

CURLY = "’"
EN_DASH = "–"

_REQ = f"1{EN_DASH}2 years{CURLY} hands-on experience in a technical support role"


def _posting(intro: str) -> str:
    """The exact HTML shape Comeet served for the Cognyte posting: an <h3>,
    then a bold/underlined intro line ending in a colon - which becomes the
    current heading and displaces "Requirements" - then bullets as <p>."""
    return ("<h3>Requirements</h3>"
            f"<p><strong><u>{intro}</u></strong></p>"
            f"<p>• {_REQ}</p>")


def test_the_real_cognyte_posting_now_yields_its_number():
    assert read_experience(_posting(f"For that mission you{CURLY}ll need:")
                           ).min_years == 1.0


def test_the_straight_apostrophe_spelling_was_never_broken():
    """Confirms the bug was the character and not the heading logic."""
    assert read_experience(_posting("For that mission you'll need:")
                           ).min_years == 1.0


@pytest.mark.parametrize("heading", [
    f"What you{CURLY}ll need:", "What you'll need:",
    f"Here{CURLY}s what you need:", "Requirements:",
])
def test_requirements_headings_match_either_apostrophe(heading):
    assert _REQ_HEADING_RE.search(_clean(heading))


@pytest.mark.parametrize("raw,expected", [
    ("‘single’", "'single'"),
    ("“double”", '"double"'),
    ("5′ years", "5' years"),
    ("years׳", "years'"),          # Hebrew geresh
])
def test_smart_quotes_fold_to_ascii(raw, expected):
    assert _clean(raw) == expected


def test_folding_does_not_disturb_hebrew_requirements():
    hebrew = ("<h3>דרישות</h3><ul>"
              "<li>3 שנות ניסיון בפיתוח - חובה</li></ul>")
    assert read_experience(hebrew).min_years == 3.0


@pytest.mark.parametrize("dash", ["-", EN_DASH, "—"])
def test_year_ranges_take_the_lower_bound_whatever_the_dash(dash):
    html = f"<h3>Requirements</h3><ul><li>3{dash}5 years of experience</li></ul>"
    assert read_experience(html).min_years == 3.0


def test_a_number_of_years_that_is_not_experience_is_still_ignored():
    """Rapyd's "NOC - Student Position": "At least 3 semesters (1.5 years) left
    until graduation" is a graduation date, not a requirement. Reading 1.5 from
    it would REJECT a genuinely junior role - the exact loss this project
    exists to prevent."""
    html = ("<h3>Requirements</h3><ul>"
            "<li>At least 3 semesters (1.5 years) left until graduation</li>"
            "<li>Knowledge in working with APIs, HTTP requests, etc.</li></ul>")
    assert read_experience(html).min_years is None
