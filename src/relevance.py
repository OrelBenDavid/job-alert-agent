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
# The list was originally countries plus a handful of capitals, which left a
# whole shape of posting slipping through: a remote/hybrid role anchored to a
# foreign CITY rather than a country. "Remote - New York", "Hybrid - Boston",
# "Remote - Toronto" and "Remote - Zurich" were all being kept, because none
# of those cities was named and neither was a country. At three companies that
# is invisible; at a hundred it is a steady trickle of jobs nobody in Israel
# can take.
#
# The trade-off runs the opposite way from the experience filter's: there,
# ambiguity fails OPEN because a missed job is unrecoverable. Here, every
# entry can only ever REMOVE a posting, so the bar for adding one is that the
# place named genuinely rules Israel out. Deliberately still absent:
# "europe" (Israel is routinely inside a EMEA/Europe hiring region), "emea",
# "global", "worldwide" - and "ist", which is Israel Standard Time as often as
# India's.
FOREIGN_REGION_MARKERS = [
    # Countries and multi-country regions
    "us", "usa", "united states", "america", "americas", "north america",
    "south america", "canada", "latam", "brazil", "mexico", "argentina",
    "colombia", "chile", "peru", "uruguay", "costa rica", "panama",
    "uk", "united kingdom", "england", "scotland", "wales", "ireland",
    "germany", "france", "spain", "portugal", "netherlands", "belgium",
    "luxembourg", "poland", "romania", "ukraine", "serbia", "bulgaria",
    "czech", "czechia", "slovakia", "slovenia", "croatia", "hungary",
    "austria", "switzerland", "italy", "greece", "cyprus", "malta", "turkey",
    "sweden", "norway", "denmark", "finland", "iceland", "estonia", "latvia",
    "lithuania", "russia", "belarus", "kazakhstan",
    "india", "china", "japan", "korea", "taiwan", "hong kong", "singapore",
    "vietnam", "thailand", "malaysia", "indonesia", "philippines", "pakistan",
    "bangladesh", "australia", "new zealand",
    "uae", "saudi", "saudi arabia", "qatar", "kuwait", "bahrain", "oman",
    "egypt", "morocco", "tunisia", "jordan", "lebanon",
    "south africa", "nigeria", "kenya", "ghana",
    # Cities and metros - the gap this list used to have
    "new york", "nyc", "san francisco", "bay area", "silicon valley",
    "los angeles", "san diego", "san jose", "palo alto", "mountain view",
    "sunnyvale", "santa clara", "seattle", "boston", "cambridge ma", "austin",
    "dallas", "houston", "chicago", "denver", "atlanta", "miami", "phoenix",
    "portland", "minneapolis", "detroit", "philadelphia", "pittsburgh",
    "washington dc", "toronto", "vancouver", "montreal", "ottawa", "calgary",
    "london", "manchester", "edinburgh", "glasgow", "bristol", "dublin",
    "berlin", "munich", "hamburg", "frankfurt", "cologne", "stuttgart",
    "dusseldorf", "paris", "lyon", "madrid", "barcelona", "valencia",
    "lisbon", "porto", "amsterdam", "rotterdam", "brussels", "zurich",
    "geneva", "vienna", "milan", "rome", "athens", "istanbul", "warsaw",
    "krakow", "wroclaw", "gdansk", "prague", "brno", "budapest", "bucharest",
    "sofia", "belgrade", "zagreb", "kyiv", "kiev", "tallinn", "riga",
    "vilnius", "stockholm", "oslo", "copenhagen", "helsinki", "moscow",
    "bangalore", "bengaluru", "hyderabad", "pune", "chennai", "mumbai",
    "delhi", "new delhi", "gurgaon", "gurugram", "noida", "beijing",
    "shanghai", "shenzhen", "tokyo", "osaka", "seoul", "taipei", "manila",
    "jakarta", "kuala lumpur", "bangkok", "hanoi", "ho chi minh",
    "sydney", "melbourne", "brisbane", "perth", "auckland", "wellington",
    "dubai", "abu dhabi", "doha", "riyadh", "cairo", "nairobi", "lagos",
    "cape town", "johannesburg", "sao paulo", "rio de janeiro",
    "buenos aires", "mexico city", "guadalajara", "monterrey", "bogota",
    "santiago", "lima",
    # *** US states and Canadian provinces ***
    #
    # Added 2026-08-13, after the company count went from 3 to 142 exposed a
    # leak this list had all along. The countries and cities above are enough
    # when a location string names one - "Texas, USA, Remote" is rejected on
    # "usa", and "Remote - New York" on the city. But Greenhouse publishes a
    # separate `offices[]` array, each entry checked on its own, and its office
    # names are bare sub-national remote regions with NO country token:
    # "Remote - Colorado", "Remote - Texas", "Remote - British Columbia".
    #
    # Checked alone, those hit the remote keyword, match no marker, and are
    # therefore kept as qualified remote - the exact "Remote-US" case the
    # module docstring says is excluded. Measured across the 28 Greenhouse
    # companies: 31 distinct office strings leaking 151 job-office matches,
    # 68 jobs at Datadog alone, which would have been ~8% of every alert batch
    # made up of US roles nobody here can take.
    #
    # This was invisible at three companies: Lever and the Wix page never
    # produce a bare sub-national remote string, so nothing exercised it.
    #
    # Full names only, deliberately. Two-letter postal codes are NOT safe here
    # because matching is whole-word on a padded string: "or" (Oregon) would
    # match inside "Remote or Hybrid", and "in" / "me" / "ok" / "hi" / "de" /
    # "id" have the same problem. The two abbreviations below are kept because
    # neither is an English word and both genuinely rule Israel out.
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "north carolina", "north dakota", "ohio", "oklahoma",
    "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia",
    "wisconsin", "wyoming", "district of columbia", "dc", "ca",
    "ontario", "quebec", "british columbia", "alberta", "manitoba",
    "saskatchewan", "nova scotia", "new brunswick", "newfoundland",
    "prince edward island", "yukon", "nunavut", "northwest territories",
    # Regional shorthands and timezones
    "apac", "anz", "dach", "benelux", "nordics", "iberia",
    "est", "edt", "pst", "pdt", "cst", "cdt", "mst", "mdt", "bst", "cet",
    "cest", "aest", "jst", "brt", "gmt", "utc",
    "pacific time", "eastern time", "central time", "mountain time",
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
