# -*- coding: utf-8 -*-
"""A structured country code is read from its own field, never matched in prose.

"IL" cannot go in ISRAEL_KEYWORDS. Matching there is whole-word on a
space-padded string, and two letters that short appear as standalone tokens in
real location text - which is exactly why the keyword lists carry no short
codes. Reading the value straight out of a field that is DECLARED to hold a
country code is a different operation, and is what makes it safe.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from relevance import (is_israel_country_code, is_israel_location,
                       is_qualified_remote, is_relevant_location, names_remote)


@pytest.mark.parametrize("code", ["IL", "il", " il ", "Il", "ISR", "isr", "376"])
def test_israeli_codes_are_recognised(code):
    assert is_israel_country_code(code) is True


@pytest.mark.parametrize("code", ["US", "GB", "DE", "BR", "", None, "ILS", "I"])
def test_everything_else_is_not(code):
    assert is_israel_country_code(code) is False


@pytest.mark.parametrize("code,expected", [(376, True), (840, False),
                                           (0, False), (True, False),
                                           ([], False), ({}, False)])
def test_a_non_string_value_is_answered_rather_than_raised(code, expected):
    """This reads whatever a profile's field map points at, and the numeric ISO
    form is a plain integer in JSON. An AttributeError here would raise out of
    the fetcher and cost the company its entire run."""
    assert is_israel_country_code(code) is expected


def test_a_code_is_never_matched_inside_a_location_string():
    """The whole reason this is a separate function: these strings contain the
    letters and must not read as Israel."""
    for text in ["Il-de-France", "Vilnius", "Wilmington", "Silicon Valley"]:
        assert is_israel_location(text) is False
        assert is_israel_country_code(text) is False


def test_names_remote_matches_what_is_qualified_remote_uses():
    """names_remote was extracted from is_qualified_remote so a fetcher can ask
    the same question about a label it is not deciding relevance from. The two
    must not drift, which is what this pins."""
    for text in ["Remote", "Hybrid - Tel Aviv", "Work from home", "עבודה מהבית"]:
        assert names_remote(text) is True
        # A remote string with nothing foreign attached still qualifies.
        assert is_qualified_remote(text) is is_relevant_location(text)

    for text in ["Tel Aviv", "New York", "", "Herzliya"]:
        assert names_remote(text) is False


def test_extracting_names_remote_did_not_change_the_qualified_remote_rule():
    assert is_qualified_remote("Remote") is True
    assert is_qualified_remote("Remote - EMEA") is True
    assert is_qualified_remote("Remote - US") is False
    assert is_qualified_remote("Remote", "Regional Sales Manager (UK)") is False
    assert is_qualified_remote("Tel Aviv") is False       # not remote at all
