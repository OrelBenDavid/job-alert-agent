# -*- coding: utf-8 -*-
"""Regression tests for the four relevance leaks found on 2026-08-18 by
replaying a live snapshot of all 145 boards through the filter chain.

Every case below is a REAL location/title string that was published by a real
company and delivered as an alert. They are kept as literals, with the company
named, so a future change to the marker lists has to confront the actual data
rather than a paraphrase of it.
"""

import pytest

from relevance import (is_relevant_location, is_qualified_remote,
                       is_israel_location, names_foreign_region)


# (location, title, expected relevant?, who published it)
LEAKS = [
    # 1. ':' was not normalized, so " us " never matched inside "US: Remote".
    ("US: Remote", "Customer Success Manager", False, "fullpath"),
    ("US: Remote", "Regional Sales Manager", False, "fullpath"),
    # 2. NFKC left the tilde composed, so "sao paulo" never matched.
    ("Remote, São Paulo", "Backend Engineer - São Paulo", False, "axonius"),
    # 3. The region lives in the TITLE; the location is a bare remote.
    ("Remote", "Technical Account Manager - UK", False, "island"),
    ("Remote", "Technical Account Manager- APAC", False, "island"),
    ("EMEA Remote", "Regional Sales Manager (United Kingdom)", False, "zafran"),
    ("EMEA Remote", "Regional Sales Manager (Nordics & Benelux)", False, "zafran"),
    # 4. ';' is Greenhouse's multi-location separator.
    ("Boston, Massachusetts, USA; New York, New York, USA",
     "Senior Software Engineer", False, "datadog"),
]


@pytest.mark.parametrize("location,title,expected,company", LEAKS)
def test_known_leaks_are_closed(location, title, expected, company):
    assert is_relevant_location(location, title) is expected, (
        f"{company}: {location!r} / {title!r}")


# The other half of the trade: these must all still be KEPT. Every entry added
# to FOREIGN_REGION_MARKERS, and every normalization change, risks this side.
KEEPERS = [
    ("Remote", "Backend Engineer"),
    ("Remote - EMEA", "Data Scientist"),
    ("Remote - Global", "QA Engineer"),
    ("Europe (Remote)", "DevOps Engineer"),
    ("Tel Aviv, Israel", "Sales Engineer, DACH region"),   # foreign word, Israeli desk
    ("Herzliya", "Account Manager - US Market"),           # ditto
    ("Be'er Sheva", "Algorithm Developer"),
    ("Ra'anana", "Student Position"),
    ("תל אביב", "מפתח תוכנה"),
]


@pytest.mark.parametrize("location,title", KEEPERS)
def test_israeli_and_open_remote_still_kept(location, title):
    assert is_relevant_location(location, title) is True


def test_title_is_only_consulted_for_qualified_remote():
    """A physical Israeli location outranks anything the title says.

    This is the guard on the whole title-checking idea: an Israeli role that
    names a foreign market it serves must never be dropped for saying so."""
    assert is_relevant_location("Tel Aviv", "Sales Manager - United Kingdom") is True
    assert is_israel_location("Tel Aviv") is True
    # ...but with no Israeli location, the same title is decisive.
    assert is_relevant_location("Remote", "Sales Manager - United Kingdom") is False


def test_title_defaults_to_empty_so_old_callers_are_unaffected():
    assert is_relevant_location("Remote") is True
    assert is_relevant_location("Remote - US") is False
    assert is_qualified_remote("Remote") is True


def test_names_foreign_region_is_whole_word():
    # "us" inside "Jerusalem", "or" inside "Remote or Hybrid" - the reason the
    # marker list holds full names and not postal codes.
    assert names_foreign_region("Jerusalem") is False
    assert names_foreign_region("Remote or Hybrid") is False
    assert names_foreign_region("Remote - US") is True


def test_accent_folding_does_not_break_hebrew_or_apostrophes():
    # NFKD decomposes Hebrew points too; the keywords are unpointed, so the
    # stripping must leave the base letters intact.
    assert is_israel_location("תל אביב") is True
    assert is_israel_location("Be'er Sheva") is True
    assert is_israel_location("Ra'anana") is True
