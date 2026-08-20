# -*- coding: utf-8 -*-
"""A foreign country code may reject - but only from a field declared to hold
one, and never over a positive Israeli signal.

*** The defect ***

`is_israel_country_code` is additive: "IL" means Israel, anything else means
nothing. That left a posting-level leak the 2026-08-19/20 expansion measured
on live boards. A US employer publishes `location.country == "US"` with a label
that reads "Remote"; the qualified-remote rule keeps it because nothing foreign
is NAMED in the text; the alert says a job Israel can take. Whole COMPANIES
qualified that way (CapsLock, MRIoA, BDR Solutions, OuterBox, ROI Agency,
Medvidi) and are now stopped by the admission gates in _onboarding/ - but those
gates cannot see inside a company that DOES have an Israeli presence, and
measured on 2026-08-20 that is where the whole remainder was: 10 postings at 4
Israeli companies, each of which keeps between 7 and 12 postings afterwards -
and at all four, every posting that survives names a physical Israeli place.

*** The three things that make it safe, all pinned below ***

1. It is opt-in per platform (`api.country_code_is_authoritative`), because a
   code is only trustworthy from a picker. Greenhouse's free-text location.name
   must be untouched.
2. A physical Israeli location, or an IL code, always wins.
3. Text naming a region that CONTAINS Israel wins too - a single-country picker
   and "EMEA" disagree, and a disagreement about location fails open.

Every location string below is a real one, with the company that published it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from relevance import (is_foreign_country_code, is_israel_country_code,
                       is_relevant_location, is_relevant_with_country_codes,
                       names_israel_inclusive_region)


# ---------------------------------------------------------------------------
# is_foreign_country_code is NOT the negation of is_israel_country_code
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ["US", "us", " gb ", "DE", "BR", "IN", "PL",
                                  "USA", 840])
def test_a_recognisable_non_israeli_code_is_foreign(code):
    assert is_foreign_country_code(code) is True


@pytest.mark.parametrize("code", ["IL", "il", "ISR", "376", 376])
def test_an_israeli_code_is_never_foreign(code):
    assert is_foreign_country_code(code) is False
    assert is_israel_country_code(code) is True


@pytest.mark.parametrize("code", ["", " ", None, 0, [], {}])
def test_an_absent_code_is_unknown_rather_than_foreign(code):
    """The single most important line in this file.

    143 of the ~3,200 live Comeet postings audited on 2026-08-20 carry no
    country at all (106 empty strings, 37 nulls) and they are ordinary Israeli
    roles. A plain `not is_israel_country_code(...)` would have dropped every
    one of them - the silent, unrecoverable loss this project exists to
    prevent, since state is written before filtering."""
    assert is_foreign_country_code(code) is False


@pytest.mark.parametrize("value", ["Remote", "Tel Aviv", "United States",
                                   "EMEA", "true", "N/A", "-"])
def test_a_value_that_is_not_a_country_code_cannot_reject(value):
    """The field is declared to hold a picker value. If a platform ever starts
    writing prose into it, this must lose the veto rather than gain a wildcard
    - so anything outside the ISO 3166-1 alpha-2/alpha-3/numeric shapes reads
    as unknown."""
    assert is_foreign_country_code(value) is False


# ---------------------------------------------------------------------------
# Constraint 3: without the flag, byte-identical behaviour
# ---------------------------------------------------------------------------

# (location, title, country code) - the cases where the flag changes something,
# plus a spread of ordinary ones.
SAMPLES = [
    ("Remote", "Business Development Representative", "US"),   # sentra
    ("Remote, Remote", "Channel Manager", "US"),               # atera_networks
    ("U.S. Remote", "Sales Engineer", "US"),                   # linx_security
    ("Remote - East Coast", "Solutions Engineer - East Coast", "US"),  # faye
    ("Remote, Europe", "Senior AI Data Scientist", "GB"),      # chaos_labs
    ("Europe (Remote)", "Director, Sales EMEA", "GB"),         # prisma
    ("Tel Aviv, Tel Aviv", "Senior DevOps", "IL"),
    ("careers", "Backend Engineer", "IL"),                      # imagen
    ("Herzeliya", "Algorithm Developer", ""),                   # bird, no code
    ("New York", "Project Manager", "US"),
    ("Remote - EMEA", "Data Scientist", None),
    ("Remote", "Technical Account Manager - UK", None),
]


@pytest.mark.parametrize("location,title,code", SAMPLES)
def test_without_the_flag_the_rule_is_exactly_what_it_always_was(location,
                                                                 title, code):
    """The expression the fetchers used before 2026-08-20, restated here so a
    future edit to is_relevant_with_country_codes has to keep matching it.

    Nothing may be subtracted on the un-flagged path: Greenhouse, Lever, Ashby,
    SmartRecruiters, HiBob and Workday all reach relevance through the same
    module and none of them has a picker to read."""
    old = (is_israel_country_code(code)
           or is_relevant_location(location, title))
    assert is_relevant_with_country_codes(location, title, [code],
                                          False) is old


def test_the_flag_defaults_to_off():
    """A caller that passes codes but never mentions the flag gets the old
    behaviour. That default is what makes this opt-in rather than opt-out."""
    assert is_relevant_with_country_codes("Remote", "", ["US"]) is True


def test_no_codes_at_all_is_the_plain_text_rule():
    for location, expected in [("Remote", True), ("Remote - US", False),
                               ("Tel Aviv", True), ("Berlin", False)]:
        assert is_relevant_with_country_codes(
            location, "", [], True) is expected, location


# ---------------------------------------------------------------------------
# Constraint 1: a physical Israeli location can never be overridden
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("location", [
    "Tel Aviv", "Herzliya", "Be'er Sheva", "תל אביב",
    "Ramat Gan Hybrid", "Jerusalem Office / Hybrid (In Israel)",
])
def test_an_israeli_location_survives_any_country_code(location):
    """A req attached to a foreign office while the role is open here is a real
    thing boards do. The text is the stronger signal and outranks the picker."""
    for code in ["US", "GB", "DE", "IN"]:
        assert is_relevant_with_country_codes(location, "", [code],
                                              True) is True, (location, code)


def test_an_israeli_code_in_a_multi_location_posting_still_wins():
    """Workable publishes one opening once per place. If ANY of them is Israel,
    the opening is relevant - the fail-open direction the collapse relies on."""
    assert is_relevant_with_country_codes("Remote", "", ["US", "PL", "IL"],
                                          True) is True


# ---------------------------------------------------------------------------
# Constraint 2: qualified remote Israel can genuinely reach must survive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("location", [
    "Remote", "Remote - EMEA", "Remote - Europe", "Remote - Global",
    "EMEA Remote", "Europe (Remote)", "Remote - Worldwide",
])
def test_open_remote_is_untouched_when_no_code_contradicts_it(location):
    """The property that matters most in this file. Losing a real job is
    unrecoverable; showing a spare one is not."""
    for codes in ([], [""], [None], ["IL"]):
        assert is_relevant_with_country_codes(location, "", codes,
                                              True) is True, (location, codes)


@pytest.mark.parametrize("location,code,company", [
    ("Remote, Europe", "GB", "chaos_labs"),
    ("Europe (Remote)", "GB", "prisma_photonics"),
    ("EMEA Remote, Stugart", "DE", "quantum_machines"),
    ("EMEA (Remote), Lecrín", "ES", "upwind_security"),
])
def test_an_israel_inclusive_region_outranks_a_single_country_picker(
        location, code, company):
    """All four live on 2026-08-20, all at companies with a real Israeli
    presence. A country code is one point; "Europe"/"EMEA" is a claim about a
    set that contains Israel, and a company that meant "United Kingdom" had no
    reason to type "Europe" beside it. When the two disagree, this project
    fails open.

    It is not free: quantum_machines' "Stugart" is a misspelt Stuttgart and
    upwind's "Lecrín" is a village in Granada, so two of these four are
    genuinely abroad and survive anyway. Both are delivered today, so keeping
    them is not a regression - it is the price of not guessing about the other
    two."""
    assert is_relevant_with_country_codes(location, "", [code], True) is True, (
        f"{company}: {location!r} / {code!r}")


@pytest.mark.parametrize("text,expected", [
    ("Remote - EMEA", True), ("EMEA Remote, Stugart", True),
    ("Europe (Remote)", True), ("Remote, Europe", True),
    ("Remote - Global", True), ("Remote - Worldwide", True),
    ("Remote", False), ("Remote - East Coast", False),
    ("U.S. Remote", False), ("Tel Aviv", False), ("", False),
])
def test_names_israel_inclusive_region_is_whole_word(text, expected):
    assert names_israel_inclusive_region(text) is expected


# ---------------------------------------------------------------------------
# What it actually removes - the ten live postings, verbatim
# ---------------------------------------------------------------------------

DROPPED = [
    # (location text as the fetcher joins it, title, code, company, why)
    ("Remote, Remote", "Channel Manager", "US", "atera_networks",
     "bare Remote on a US req, beside 12 physically Israeli ones"),
    ("Remote, Remote", "Enterprise Account Executive", "US", "atera_networks",
     "same"),
    ("Remote - East Coast, Remote- East Coast",
     "Account Development Manager - East Coast", "US", "faye",
     "the marker list has states and cities but no coasts"),
    ("West Coast - Remote, Remote",
     "Account Development Manager - West Coast", "US", "faye", "same"),
    ("West Coast - Remote, Remote",
     "Business Development Manager - West Coast", "US", "faye", "same"),
    ("Remote - East Coast, Remote- East Coast",
     "Solutions Engineer - East Coast", "US", "faye", "same"),
    ("U.S. Remote", "Customer Success Engineer", "US", "linx_security",
     "_normalize turns 'U.S.' into 'u s', so the `us` marker cannot match"),
    ("U.S. Remote", "Director Product Marketing", "US", "linx_security",
     "same"),
    ("U.S. Remote", "Sales Engineer", "US", "linx_security", "same"),
    ("Remote", "Business Development Representative", "US", "sentra",
     "bare Remote on a US req, beside 8 physically Israeli ones"),
]


@pytest.mark.parametrize("location,title,code,company,why", DROPPED)
def test_the_ten_postings_this_removes(location, title, code, company, why):
    """Each was delivered as an alert before 2026-08-20 and is a US role.

    They are kept as literals, with the company named, because the honest
    measurement of this change is a list of ten specific jobs - not a
    percentage. Note the first half of each assertion: the TEXT rule still
    keeps every one of them, which is the whole reason the picker had to be
    read."""
    assert is_relevant_location(location, title) is True, "text rule kept it"
    assert is_relevant_with_country_codes(location, title, [code],
                                          True) is False, f"{company}: {why}"


# ---------------------------------------------------------------------------
# The platform profiles that carry the flag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", ["comeet", "workable"])
def test_the_two_verified_platforms_declare_the_flag(platform):
    from profiles import load_platform
    api = load_platform(platform)["api"]
    assert api["country_code_is_authoritative"] is True
    # The flag says a live audit happened. Requiring the note means it cannot
    # be copied onto a third platform without one being written.
    assert "2026-08-20" in api["_country_code_note"]


@pytest.mark.parametrize("platform", ["greenhouse", "lever", "ashby", "hibob"])
def test_no_other_platform_claims_an_authoritative_code(platform):
    """Greenhouse's location.name is free text - a company writes "Remote -
    Colorado" into it by hand. None of these four publishes a picker, so none
    may carry the flag, and their fetchers do not read one at all."""
    from profiles import load_platform
    assert "country_code_is_authoritative" not in load_platform(platform)["api"]
