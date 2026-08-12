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
