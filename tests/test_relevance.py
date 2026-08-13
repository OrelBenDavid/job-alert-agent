import pytest

from relevance import is_relevant_location, is_israel_location, is_qualified_remote

# (input text, expected relevant?) - real-world cases seen across various
# ATS platforms, including three that failed in the first pass of writing
# this helper (Be'er Sheva, Ra'anana, Remote - US) and therefore stay here
# permanently as a regression safety net.
CASES = [
    ("Tel-Aviv, Israel", True), ("Tel Aviv", True), ("תל אביב", True),
    ("ישראל", True), ("Jerusalem, Israel", True), ("Beer Sheva", True),
    ("Be'er Sheva", True), ("Beersheba", True), ("Haifa, Israel", True),
    ("Ra'anana", True), ("Petah Tikva", True), ("Yokneam Illit", True),
    ("Remote", True), ("Remote - EMEA", True), ("Remote - Global", True),
    ("Remote (Israel)", True), ("Hybrid - Tel Aviv", True), ("Remote/Hybrid", True),
    ("Remote - US", False), ("Remote, EST hours", False),
    ("Remote - Americas", False), ("Remote - UK", False),
    ("London, United Kingdom", False), ("New York, USA", False),
    ("Munich, Germany", False), ("Bangalore, India", False),
    ("Multiple Locations", False), ("Austin, Texas", False),
    ("West Palm Beach", False), ("Cloudera HQ", False), ("", False),
    ("San Francisco, CA", False), ("Remote - Poland", False),
]


def test_relevance_matrix():
    failures = [(t, e, is_relevant_location(t)) for t, e in CASES
               if is_relevant_location(t) != e]
    assert not failures, f"failures: {failures}"


def test_israel_does_not_need_remote_keyword():
    assert is_israel_location("Tel Aviv") is True
    assert is_qualified_remote("Tel Aviv") is False   # no remote keyword - that's expected


def test_whole_word_matching_not_substring():
    # "lod" must not match inside "Cloudera" - a real whole-word test
    assert is_israel_location("Cloudera HQ, remote") is False
    # and the same rule the other way: "us" sits inside "Jerusalem", which
    # must not therefore read as a foreign region
    assert is_qualified_remote("Remote - Jerusalem") is True
    assert is_qualified_remote("Remote - Austin, USA") is False


def test_a_remote_job_anchored_to_a_foreign_city_is_dropped():
    """The gap that used to be recorded here as a known limitation.

    The marker list was countries plus a few capitals, so a remote role
    naming only a city ("Remote - Austin", "Hybrid - Boston") was kept - a
    job nobody in Israel can take. Invisible at three companies, a steady
    trickle at a hundred."""
    for location in ["Remote - Austin", "Hybrid - Boston", "Remote - New York",
                     "Remote (Bay Area)", "Remote - Toronto",
                     "Hybrid - Zurich", "Remote - Bangalore"]:
        assert is_relevant_location(location) is False, location


def test_regions_that_include_israel_are_still_kept():
    """The other direction, and the reason the list stops where it does:
    every entry can only ever remove a posting, so anything Israel plausibly
    sits inside must stay out of it."""
    for location in ["Remote - EMEA", "Remote - Global", "Remote - Worldwide",
                     "Remote - Europe", "Remote", "Remote (Israel)"]:
        assert is_relevant_location(location) is True, location


# ---------------------------------------------------------------------------
# Sub-national remote regions
#
# Regression cover for a leak that survived until the company count went from
# 3 to 142. Greenhouse publishes a separate `offices[]` array and the fetcher
# checks each entry ON ITS OWN, so an office named "Remote - Colorado" was
# tested with no country token anywhere in the string: it hit the remote
# keyword, matched no marker, and was kept as qualified remote - the exact
# "Remote-US" case this module's docstring says is excluded.
#
# Measured before the fix: 31 distinct office strings leaking 151 job-office
# matches across the 28 Greenhouse companies, 68 jobs at Datadog alone.
# Invisible at three companies because neither Lever nor the Wix page ever
# produces a bare sub-national remote string.
# ---------------------------------------------------------------------------

US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
    "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "West Virginia", "Wisconsin", "Wyoming",
]


@pytest.mark.parametrize("state", US_STATES)
def test_remote_in_a_us_state_is_not_relevant(state):
    """All fifty, parametrised on purpose: the first version of the fix left
    Texas out, and a spot-check of five would not have caught it."""
    assert not is_relevant_location(f"Remote - {state}")


@pytest.mark.parametrize("province", [
    "Ontario", "Quebec", "British Columbia", "Alberta", "Manitoba",
    "Saskatchewan", "Nova Scotia", "New Brunswick",
])
def test_remote_in_a_canadian_province_is_not_relevant(province):
    assert not is_relevant_location(f"Remote - {province}")


@pytest.mark.parametrize("office", [
    "Remote - DC", "Remote (CA)", "Remote - District of Columbia",
])
def test_the_observed_abbreviated_office_strings_are_not_relevant(office):
    """Only abbreviations that are not English words were added. See the
    comment in relevance.py on why "or"/"in"/"me" are deliberately absent."""
    assert not is_relevant_location(office)


@pytest.mark.parametrize("text", [
    "Remote or Hybrid",
    "Tel Aviv or Herzliya",
    "Remote - Israel, in office 2 days",
])
def test_english_words_that_double_as_state_codes_do_not_reject_a_job(text):
    """The reason full state names were added and two-letter postal codes
    were not: "or" (Oregon), "in" (Indiana), "me" (Maine) are ordinary words,
    and matching is whole-word on a padded string. Adding them would silently
    reject real Israeli postings - the unrecoverable direction."""
    assert is_relevant_location(text)


def test_a_genuinely_unqualified_remote_role_is_still_kept():
    """The fix must not have turned the qualified-remote rule into a blanket
    rejection of remote work - EMEA and Global both include Israel."""
    for text in ("Remote", "Remote - EMEA", "Remote - Global", "Hybrid"):
        assert is_relevant_location(text), text
