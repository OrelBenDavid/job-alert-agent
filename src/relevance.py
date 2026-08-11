# -*- coding: utf-8 -*-
"""
The relevance filter: does a job posting belong to "hiring from Israel" -
a physical location in Israel, or remote work that can genuinely be done
from here (not Remote-US / Remote-UK etc).

Single source of truth shared with the career-site-profiler skill
(RELEVANCE_HELPER) - any change to the lists or the logic is made here and
in the skill together, deliberately, never in just one of them.
Fully covered by tests/test_relevance.py.

Note: the ISRAEL_KEYWORDS and REMOTE_KEYWORDS lists below contain Hebrew
strings by design - these are DATA (location names actually returned by
Israeli companies' ATS platforms in Hebrew), not comments, and must stay
in Hebrew regardless of the project's "comments in English" convention.
"""

import re
import unicodedata

# Locations in Israel, in normalized form (see _normalize) - including
# variants actually observed across different ATS platforms (Lever returns
# "Tel-Aviv" with a hyphen) and Hebrew-language names.
ISRAEL_KEYWORDS = [
    "israel", "isr",
    "tel aviv", "telaviv", "tlv", "ramat gan", "givatayim", "holon", "bat yam",
    "herzliya", "hertzliya", "raanana", "kfar saba", "hod hasharon", "netanya",
    "petah tikva", "petach tikva", "rosh haayin", "or yehuda", "airport city",
    "jerusalem", "beit shemesh", "modiin",
    "haifa", "yokneam", "nesher", "tirat carmel", "caesarea", "hadera",
    "beer sheva", "beersheba", "beersheva", "sderot", "omer",
    "rehovot", "ness ziona", "nes ziona", "rishon lezion", "lod", "ramla",
    "yavne", "ashdod", "ashkelon", "tefen", "migdal haemek",
    # Hebrew - Israeli sites frequently return the location in Hebrew only
    "ישראל", "תל אביב", "תלאביב", "רמת גן", "הרצליה", "רעננה", "כפר סבא",
    "הוד השרון", "נתניה", "פתח תקווה", "פתח תקוה", "ראש העין", "אור יהודה",
    "ירושלים", "מודיעין", "חיפה", "יקנעם", "קיסריה", "באר שבע", "רחובות",
    "נס ציונה", "ראשון לציון", "יבנה", "אשדוד", "אשקלון", "מגדל העמק",
]

# Words that indicate a remote job
REMOTE_KEYWORDS = ["remote", "hybrid", "anywhere", "work from home", "wfh",
                   "distributed", "מרחוק", "היברידי", "עבודה מהבית"]

# *** The qualified-remote rule ***
# A remote job is kept only if it doesn't attach an explicit foreign
# region/country/timezone to itself.
# "Remote" / "Remote - EMEA" / "Remote - Global" -> kept (EMEA includes Israel).
# "Remote - US" / "Remote, EST hours" / "Remote (UK)" -> dropped.
# Matching is whole-word (with space padding) - otherwise "us" would match
# inside "Austin" and "est" inside "West".
FOREIGN_REGION_MARKERS = [
    "us", "usa", "united states", "america", "americas", "canada",
    "latam", "brazil", "mexico", "argentina",
    "uk", "united kingdom", "england", "london", "ireland", "dublin",
    "germany", "berlin", "munich", "france", "paris", "spain", "madrid",
    "portugal", "lisbon", "netherlands", "amsterdam", "poland", "warsaw",
    "romania", "bucharest", "ukraine", "serbia", "bulgaria", "czech", "prague",
    "india", "bangalore", "china", "beijing", "shanghai", "japan", "tokyo",
    "singapore", "australia", "sydney", "new zealand", "korea", "taiwan",
    "apac", "anz", "dach", "benelux", "nordics",
    "est", "pst", "cst", "mst", "gmt", "utc", "pacific time", "eastern time",
]


def _normalize(text: str) -> str:
    """Normalizes a location string for comparison: lowercase, no
    apostrophes/punctuation, single spaces.

    The apostrophe is DELETED, not turned into a space, on purpose:
    "Be'er Sheva" -> "beer sheva", "Ra'anana" -> "raanana". Replacing it
    with a space would split into "be er"/"ra anana" and miss the match -
    this was caught by a real test failure, not by inspection.
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = text.lower()
    text = re.sub(r"['\u2019\u05f3]", "", text)          # apostrophes - deleted
    text = re.sub(r"[\-_/|,.()\[\]]", " ", text)          # rest of punctuation -> space
    text = re.sub(r"\s+", " ", text).strip()
    return f" {text} "                                    # padding for whole-word matching


def is_israel_location(location_text: str) -> bool:
    """Does the location string point to a physical place in Israel."""
    norm = _normalize(location_text)
    return any(f" {kw} " in norm for kw in ISRAEL_KEYWORDS)


def is_qualified_remote(location_text: str) -> bool:
    """Is this a remote job that can actually be staffed from Israel."""
    norm = _normalize(location_text)
    if not any(f" {kw} " in norm for kw in REMOTE_KEYWORDS):
        return False
    if is_israel_location(location_text):
        return True
    return not any(f" {m} " in norm for m in FOREIGN_REGION_MARKERS)


def is_relevant_location(location_text: str) -> bool:
    """The check every fetcher calls: Israel, or qualified remote."""
    return is_israel_location(location_text) or is_qualified_remote(location_text)
