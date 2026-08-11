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
    # "us" must not match inside "Austin" - but "usa" still matches when spelled out
    assert is_qualified_remote("Remote - Austin, USA") is False
    # by contrast: "Austin" alone, with no country mentioned, isn't in the
    # foreign-city list - this is a known limitation (the list is
    # countries/regions, not every city on earth), not a whole-word-
    # matching bug. There's no way to know "Austin" means Texas without
    # more context.
    assert is_qualified_remote("Remote - Austin") is True
