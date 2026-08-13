# -*- coding: utf-8 -*-
"""
v2 function templates - each matches a different profile type (Step 6 in
SKILL.md). Copy the matching template, fill in the placeholders (double
braces), and paste the result into the project's fetchers layer.

*** Material change from v1 ***
All templates return list[Job] - not list[str].
Reason: "new job" detection must run on a stable identifier (id/link), not
a display string. A "Title — Location" string changes with any cosmetic
change on the company's side (a space becoming a hyphen, "(Maternity
Leave)" appended to a title) - and then the same job gets re-sent as if
it were new. On top of that, without a link there's no way to send Orel a
message with a link to the job.

The "Title — Location" format still exists - but it's built in the
notification layer only, never here.
"""

# ---------------------------------------------------------------------------
# The job model - shared by all templates. In this project it lives in
# job_utils.py (or models.py, matching the project's actual layout).
# ---------------------------------------------------------------------------

JOB_MODEL = '''
from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    """A single job posting. frozen=True so it can go into a set and be
    compared across runs."""
    id: str          # Stable identifier: the ATS's own id, or the
                      # canonicalized URL as a fallback. This is what the
                      # diff runs on.
    title: str        # Job title as shown
    location: str      # Raw location string - kept as-is, for display and debugging
    url: str          # Direct link to the job page - sent in the Telegram alert
    company: str      # The company's slug, to know which profile a job came from

    def display(self) -> str:
        """The agreed display format. Built here, never stored, so the
        diff is never affected by it."""
        return f"{self.title} — {self.location}"
'''


# ---------------------------------------------------------------------------
# Link canonicalization - critical for deduplication
# ---------------------------------------------------------------------------

URL_HELPER = '''
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

# Params that change between runs and aren't part of the job's identity -
# must be stripped before a link becomes an id, otherwise every run would
# look like every job changed.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "gh_jid", "source", "src", "ref", "referrer", "lever-source",
    "fbclid", "gclid", "_ga", "session", "sessionid", "t", "ts",
}


def canonicalize_url(href: str, base: str = "") -> str:
    """Normalizes a job link so it can serve as a stable id between runs.

    Three things happen here, and each is a real bug if skipped:
    1) relative -> absolute link - otherwise the same job gets a different
       id if the site alternates between "/jobs/123" and
       "https://.../jobs/123" between loads
    2) stripping tracking params - some sites vary these on every load
    3) dropping the fragment and normalizing a trailing slash - meaningless
       differences
    """
    if not href:
        return ""
    absolute = urljoin(base, href.strip())        # (1) relative -> absolute
    parts = urlparse(absolute)
    kept = [(k, v) for k, v in parse_qsl(parts.query)
            if k.lower() not in _TRACKING_PARAMS]  # (2) strip tracking params
    path = parts.path.rstrip("/") or "/"           # (3) normalize trailing slash
    return urlunparse((parts.scheme, parts.netloc, path,
                       "", urlencode(kept), ""))   # fragment always dropped
'''


# ---------------------------------------------------------------------------
# The relevance filter - Israel + qualified remote (SKILL.md Step 5)
# Single source of truth: these lists are not duplicated into profiles.
# ---------------------------------------------------------------------------

RELEVANCE_HELPER = '''
import re
import unicodedata

# Locations in Israel - including variants actually observed across
# different ATS platforms and Hebrew-language names. The list is checked
# against a *normalized* string, so it's also written in normalized form
# here (lowercase, no hyphens/apostrophes - see _normalize below).
#
# Note: the Hebrew strings in this list and in REMOTE_KEYWORDS below are
# DATA (location names Israeli companies' ATS platforms actually return
# in Hebrew), not comments - they stay in Hebrew regardless of the
# project's "comments in English" convention.
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

# Words indicating a remote job
REMOTE_KEYWORDS = [
    "remote", "hybrid", "anywhere", "work from home", "wfh", "distributed",
    "מרחוק", "היברידי", "עבודה מהבית",
]

# *** The qualified-remote rule ***
# A remote job is kept only if it doesn't attach itself to an explicit
# foreign region.
# "Remote" / "Remote - EMEA" / "Remote - Global" -> kept.
# "Remote - US" / "Remote, EST hours" / "Remote (UK)" -> dropped.
# Matching is whole-word (with space padding) - otherwise "us" would match
# inside "Austin" and "est" inside "West".
# Countries alone left a whole shape of posting slipping through: a
# remote/hybrid role anchored to a foreign CITY rather than a country
# ("Remote - New York", "Hybrid - Boston", "Remote - Zurich"). Cities are
# therefore listed too. Every entry here can only ever REMOVE a posting, so
# the bar for adding one is that the place named genuinely rules Israel out.
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
    # Cities and metros
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
    # Regional shorthands and timezones
    "apac", "anz", "dach", "benelux", "nordics", "iberia",
    "est", "edt", "pst", "pdt", "cst", "cdt", "mst", "mdt", "bst", "cet",
    "cest", "aest", "jst", "brt", "gmt", "utc",
    "pacific time", "eastern time", "central time", "mountain time",
]

# EMEA includes Israel, so it's deliberately NOT in the foreign-region list.
# Same for "global"/"worldwide" and "europe" - all considered relevant. "ist"
# is out too: it is Israel Standard Time as often as India's.


def _normalize(text: str) -> str:
    """Normalizes a location string for comparison: lowercase, no
    apostrophes/punctuation, single spaces.

    The apostrophe is DELETED, not turned into a space, on purpose:
    "Be'er Sheva" -> "beer sheva", "Ra'anana" -> "raanana". Replacing it
    with a space would split into "be er"/"ra anana" and miss the match -
    this was caught by running the test cases, not by reading the code.
    """
    text = unicodedata.normalize("NFKC", text or "")   # unify character variants
    text = text.lower()
    text = re.sub(r"['\\u2019\\u05f3]", "", text)         # apostrophes - deleted
    text = re.sub(r"[\\-_/|,.()\\[\\]]", " ", text)       # rest of punctuation -> space
    text = re.sub(r"\\s+", " ", text).strip()            # collapse repeated spaces
    return f" {text} "                                  # padding, for whole-word search


def is_israel_location(location_text: str) -> bool:
    """Does the location string point to a physical place in Israel.
    Whole-word matching (with padding), so "lod" doesn't match inside
    "Cloudera"."""
    norm = _normalize(location_text)
    return any(f" {kw} " in norm for kw in ISRAEL_KEYWORDS)


def is_qualified_remote(location_text: str) -> bool:
    """Is this a remote job that can actually be staffed from Israel.

    Two conditions: (a) there's a remote signal, (b) there is *no* mention
    of an explicit foreign region. The second condition is the whole
    point - without it "Remote - US" would be kept.
    """
    norm = _normalize(location_text)
    if not any(f" {kw} " in norm for kw in REMOTE_KEYWORDS):
        return False                                    # (a) not remote at all
    if is_israel_location(location_text):
        return True                                     # "Remote (Israel)" - clearly yes
    return not any(f" {m} " in norm for m in FOREIGN_REGION_MARKERS)     # (b)


def is_relevant_location(location_text: str) -> bool:
    """The check every template calls: Israel, or qualified remote."""
    return is_israel_location(location_text) or is_qualified_remote(location_text)
'''


# ---------------------------------------------------------------------------
# TEMPLATE: static HTML (Step 2 succeeded - no browser needed)
# ---------------------------------------------------------------------------

TEMPLATE_HTML_STATIC = '''
def get_jobs_{{COMPANY_SLUG}}(url: str = "{{BASE_URL}}") -> list[Job]:
    """Fetches jobs from {{COMPANY_NAME}} via static HTML - verified that
    content exists without JS. If israel_filter.method == "url_param",
    the filter is already baked into this url; is_relevant_location below
    remains a safety net, not the only mechanism."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; job-alert-bot/1.0)"}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    jobs = []
    for card in soup.select("{{JOB_SELECTOR}}"):
        title_el = card.select_one("{{TITLE_SELECTOR}}")
        location_el = card.select_one("{{LOCATION_SELECTOR}}")
        link_el = card.select_one("{{LINK_SELECTOR}}")
        title = title_el.get_text(strip=True) if title_el else ""
        location = location_el.get_text(strip=True) if location_el else ""
        href = link_el.get("href", "") if link_el else ""
        if not title:
            continue
        if not is_relevant_location(location):
            continue
        canonical = canonicalize_url(href, "{{LINK_BASE}}")   # the id must be stable
        jobs.append(Job(
            id=canonical or f"{{COMPANY_SLUG}}|{title}|{location}",  # weak fallback, still better than nothing
            title=title, location=location,
            url=canonical, company="{{COMPANY_SLUG}}",
        ))
    return jobs
'''


# ---------------------------------------------------------------------------
# TEMPLATE: JSON-LD (Google Jobs markup) - cleaner than selectors when present
# ---------------------------------------------------------------------------

TEMPLATE_HTML_JSONLD = '''
def get_jobs_{{COMPANY_SLUG}}(url: str = "{{BASE_URL}}") -> list[Job]:
    """Fetches jobs from {{COMPANY_NAME}} from JSON-LD JobPosting blocks.
    Preferable to CSS selectors when both exist: the structure is
    documented, more resilient to redesigns, and carries a structured url
    and location instead of free-text DOM."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; job-alert-bot/1.0)"}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    jobs = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "{}")
        except json.JSONDecodeError:
            continue                     # malformed block - skip, don't fail the whole run
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if entry.get("@type") != "JobPosting":
                continue
            addr = ((entry.get("jobLocation") or {}).get("address") or {})
            location = ", ".join(x for x in [addr.get("addressLocality"),
                                             addr.get("addressCountry")] if x)
            if not is_relevant_location(location):
                continue
            canonical = canonicalize_url(entry.get("url", ""), "{{LINK_BASE}}")
            jobs.append(Job(
                id=str(entry.get("identifier") or canonical),
                title=(entry.get("title") or "").strip(),
                location=location, url=canonical, company="{{COMPANY_SLUG}}",
            ))
    return jobs
'''


# ---------------------------------------------------------------------------
# Shared helper for all Playwright templates: reading a single job card
# ---------------------------------------------------------------------------

PLAYWRIGHT_CARD_HELPER = '''
def _read_job_card(card, company: str, link_base: str,
                   title_sel: str, location_sel: str, link_sel: str,
                   fallback_location: str = "") -> Job | None:
    """Reads a single job card from the DOM and returns a Job, or None if
    there's no title. Centralized here instead of duplicated in every
    template, so a fix to the reading logic reaches all of them."""
    title_el = card.query_selector(title_sel)
    location_el = card.query_selector(location_sel)
    link_el = card.query_selector(link_sel)
    title = title_el.inner_text().strip() if title_el else ""
    if not title:
        return None                       # a card with no title isn't a job (separator/skeleton)
    location = location_el.inner_text().strip() if location_el else fallback_location
    href = link_el.get_attribute("href") if link_el else ""
    canonical = canonicalize_url(href, link_base)
    return Job(
        id=canonical or f"{company}|{title}|{location}",   # weak fallback if no link
        title=title, location=location, url=canonical, company=company,
    )
'''


# ---------------------------------------------------------------------------
# TEMPLATE: Playwright, single page, no pagination
# ---------------------------------------------------------------------------

TEMPLATE_PLAYWRIGHT_SINGLE_PAGE = '''
def get_jobs_{{COMPANY_SLUG}}(url: str = "{{BASE_URL}}") -> list[Job]:
    """Fetches jobs from {{COMPANY_NAME}} - single page, no pagination,
    requires JS."""
    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0")   # a real UA reduces blocks
        page.goto(url, timeout=30000)

        {{RELEVANCE_FILTER_ACTIONS}}
        # method == "url_param"  -> stays empty, the filter is already in the url
        # method == "ui_interaction" -> the click sequence from Step 3a goes here,
        #   *before* reading any jobs
        # method == "post_fetch" -> stays empty, the check in the loop below does the work

        page.wait_for_selector("{{JOB_SELECTOR}}", timeout=15000)

        for card in page.query_selector_all("{{JOB_SELECTOR}}"):
            job = _read_job_card(card, "{{COMPANY_SLUG}}", "{{LINK_BASE}}",
                                 "{{TITLE_SELECTOR}}", "{{LOCATION_SELECTOR}}",
                                 "{{LINK_SELECTOR}}")
            if job and is_relevant_location(job.location):
                jobs.append(job)

        browser.close()
    return jobs
'''


# ---------------------------------------------------------------------------
# TEMPLATE: Playwright, URL-parameter pagination
# Fixed in v2: (1) the filter is re-applied after every navigation,
#              (2) a page cap, (3) detecting a page that repeats itself
#                  (sites that clamp an out-of-range page)
# ---------------------------------------------------------------------------

TEMPLATE_PLAYWRIGHT_URL_PAGES = '''
def get_jobs_{{COMPANY_SLUG}}(base_url: str = "{{BASE_URL}}") -> list[Job]:
    """Fetches jobs from {{COMPANY_NAME}} - URL-based pagination
    (?{{PAGE_PARAM}}=N), requires JS.
    Observed stop condition: {{STOP_CONDITION}}.

    Two bugs from v1, fixed here:
    1) a ui_interaction filter was wiped by every page.goto - it's now
       re-applied after every navigation.
    2) a site that clamped ?page=999 back to page 1 caused an infinite
       loop up to the Actions timeout. There's now both a hard page cap
       and a page-content fingerprint comparison."""

    def apply_relevance_filter(page) -> None:
        """Applies the location filter. Called after *every* navigation -
        see the docstring above. If israel_filter.method == "url_param",
        the body stays a no-op - nothing to do."""
        {{RELEVANCE_FILTER_ACTIONS}}

    jobs, seen_ids = [], set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0")

        previous_fingerprint = None
        for page_num in range({{START_VALUE}}, {{START_VALUE}} + {{MAX_PAGES}}):
            sep = "&" if "?" in base_url else "?"
            page.goto(f"{base_url}{sep}{{PAGE_PARAM}}={page_num}", timeout=30000)
            apply_relevance_filter(page)          # (1) the filter must be re-applied after navigation
            try:
                page.wait_for_selector("{{JOB_SELECTOR}}", timeout=10000)
            except Exception:
                break                             # empty / unloaded page - end of the road

            cards = page.query_selector_all("{{JOB_SELECTOR}}")
            if not cards:
                break

            page_jobs = [_read_job_card(c, "{{COMPANY_SLUG}}", "{{LINK_BASE}}",
                                        "{{TITLE_SELECTOR}}", "{{LOCATION_SELECTOR}}",
                                        "{{LINK_SELECTOR}}") for c in cards]
            page_jobs = [j for j in page_jobs if j]

            # (2) if this page is identical to the last one, the site is
            # clamping rather than ending. Stop instead of looping forever.
            fingerprint = tuple(j.id for j in page_jobs)
            if fingerprint == previous_fingerprint:
                break
            previous_fingerprint = fingerprint

            for job in page_jobs:
                if job.id in seen_ids:
                    continue                      # dedupe by id, not by title
                if not is_relevant_location(job.location):
                    continue
                seen_ids.add(job.id)
                jobs.append(job)

        browser.close()
    return jobs
'''


# ---------------------------------------------------------------------------
# TEMPLATE: Playwright, click-through pagination (JS state, no URL change)
# Fixed in v2: is_disabled() doesn't catch aria-disabled / class="disabled"
# ---------------------------------------------------------------------------

TEMPLATE_PLAYWRIGHT_CLICK_NEXT = '''
def _next_is_unavailable(btn) -> bool:
    """Whether the "Next" button is actually disabled.

    Playwright's is_disabled() checks only the disabled attribute of form
    elements - it returns False for <a class="disabled"> and for
    aria-disabled="true", which are the common forms on careers sites.
    Relying on it alone means an infinite click loop on the last page."""
    if btn is None:
        return True
    if btn.get_attribute("aria-disabled") == "true":
        return True
    if "disabled" in (btn.get_attribute("class") or "").lower():
        return True
    try:
        if btn.is_disabled() or not btn.is_visible():
            return True
    except Exception:
        return True                    # element vanished from the DOM between checks
    return False


def get_jobs_{{COMPANY_SLUG}}(url: str = "{{BASE_URL}}") -> list[Job]:
    """Fetches jobs from {{COMPANY_NAME}} - click-through pagination (the
    URL doesn't change). The filter is applied exactly once, before the
    loop - correct here, since there are no navigations that reset it
    (unlike the URL-pages template).
    Observed disabled marker for this site: {{DISABLED_MARKER}}"""
    jobs, seen_ids = [], set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0")
        page.goto(url, timeout=30000)

        {{RELEVANCE_FILTER_ACTIONS}}
        # example (ui_interaction, flat filter):
        # page.click("{{LOCATION_FILTER_SELECTOR}}")
        # page.click("{{ISRAEL_OPTION_SELECTOR}}")
        # page.wait_for_timeout(1000)  # let the list refresh before continuing
        #
        # example (ui_interaction, hierarchical filter - like Mobileye: a "+"
        # next to Israel expands a city list rather than filtering directly -
        # every city that appears must be selected):
        # page.click("{{LOCATION_FILTER_SELECTOR}}")
        # page.click("{{ISRAEL_EXPAND_SELECTOR}}")           # click the "+" next to Israel
        # for city_checkbox in page.query_selector_all("{{ISRAEL_CITY_CHECKBOXES_SELECTOR}}"):
        #     city_checkbox.click()                            # check every city that appears - not a fixed list
        # page.click("{{APPLY_BUTTON_SELECTOR}}")             # if there's an "Apply" button
        # page.wait_for_timeout(1000)

        page.wait_for_selector("{{JOB_SELECTOR}}", timeout=15000)

        for _ in range({{MAX_PAGES}}):        # hard cap instead of while True
            new_on_this_page = 0
            for card in page.query_selector_all("{{JOB_SELECTOR}}"):
                job = _read_job_card(card, "{{COMPANY_SLUG}}", "{{LINK_BASE}}",
                                     "{{TITLE_SELECTOR}}", "{{LOCATION_SELECTOR}}",
                                     "{{LINK_SELECTOR}}")
                if not job or job.id in seen_ids:
                    continue
                new_on_this_page += 1
                seen_ids.add(job.id)
                if is_relevant_location(job.location):
                    jobs.append(job)

            next_btn = page.query_selector("{{NEXT_BUTTON_SELECTOR}}")
            if _next_is_unavailable(next_btn):
                break
            if new_on_this_page == 0:
                break     # clicked "Next" but nothing new appeared - the site is stuck, stop

            next_btn.click()
            page.wait_for_timeout(1200)   # let the new content load

        browser.close()
    return jobs
'''


# ---------------------------------------------------------------------------
# TEMPLATE: Playwright, infinite scroll
# Fixed in v2: (1) requires N consecutive stable checks, not one,
#              (2) supports an inner scroll container
# ---------------------------------------------------------------------------

TEMPLATE_PLAYWRIGHT_INFINITE_SCROLL = '''
def get_jobs_{{COMPANY_SLUG}}(url: str = "{{BASE_URL}}") -> list[Job]:
    """Fetches jobs from {{COMPANY_NAME}} - infinite scroll, stops once the
    job count stops growing.

    Two fixes vs v1:
    1) stops only after three consecutive checks with no growth. A single
       check after an 800ms wait stopped too early on a slow network (or a
       busy runner).
    2) if the list scrolls inside an inner element rather than the window,
       page.mouse.wheel does nothing. In that case
       scroll_container_selector in the profile is set, and it's scrolled
       via JS instead."""
    STABLE_ROUNDS_REQUIRED = 3
    MAX_SCROLLS = {{MAX_SCROLLS}}
    container = "{{SCROLL_CONTAINER_SELECTOR}}"   # empty string = scroll the window

    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0")
        page.goto(url, timeout=30000)

        {{RELEVANCE_FILTER_ACTIONS}}

        page.wait_for_selector("{{JOB_SELECTOR}}", timeout=15000)

        previous_count, stable_rounds = 0, 0
        for _ in range(MAX_SCROLLS):
            if container:
                page.eval_on_selector(                    # scroll an inner container
                    container, "el => el.scrollTop = el.scrollHeight")
            else:
                page.mouse.wheel(0, 4000)                 # scroll the window
            page.wait_for_timeout(1000)

            current_count = len(page.query_selector_all("{{JOB_SELECTOR}}"))
            if current_count == previous_count:
                stable_rounds += 1
                if stable_rounds >= STABLE_ROUNDS_REQUIRED:
                    break                                 # really done, not just slow
            else:
                stable_rounds = 0                         # grew - reset the counter
            previous_count = current_count

        for card in page.query_selector_all("{{JOB_SELECTOR}}"):
            job = _read_job_card(card, "{{COMPANY_SLUG}}", "{{LINK_BASE}}",
                                 "{{TITLE_SELECTOR}}", "{{LOCATION_SELECTOR}}",
                                 "{{LINK_SELECTOR}}")
            if job and is_relevant_location(job.location):
                jobs.append(job)

        browser.close()
    # dedupe by id (scrolling sometimes duplicates cards on re-render)
    return list({j.id: j for j in jobs}.values())
'''


# ---------------------------------------------------------------------------
# TEMPLATE: Playwright, flat "City, Country" single-select filter
# (structure = flat_multi_location) - only when there's no API alternative!
# Fixed in v2: (1) URL-encoding, (2) pagination within each city,
#              (3) dedupe by id
# ---------------------------------------------------------------------------

TEMPLATE_PLAYWRIGHT_MULTI_LOCATION = '''
def get_jobs_{{COMPANY_SLUG}}(base_url: str = "{{BASE_URL}}") -> list[Job]:
    """Fetches jobs from {{COMPANY_NAME}} - the location picker is a flat
    list of "City, Country" options, single-select, with no aggregate
    "Israel" option. So: one separate run per Israeli city found in the
    picker, then merged.

    Before using this template, double-check there's no known ATS
    (SKILL Step 1a/1b). If there's an API, it returns every job's
    location in one call and this loop is unnecessary.

    Fixes vs v1:
    1) the city name goes through quote() - "Tel-Aviv, Israel" contains a
       space and a comma that can't go raw into a URL (the valid value is
       ?location=Tel-Aviv%2C%20Israel).
    2) pagination *within* each city. v1 fetched only the first page per
       city and silently dropped the rest, despite the skill claiming
       this template handled internal pagination.
    3) merging by id (canonicalized link), not by title - the same title
       in two different cities is usually two different jobs."""
    jobs_by_id = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0")
        page.goto(base_url, timeout=30000)

        # Step 1: read the menu's actual options. Not a hardcoded list in
        # code - the cities that appear depend on currently open roles
        # and change over time
        page.click("{{LOCATION_FILTER_SELECTOR}}")
        option_elements = page.query_selector_all("{{LOCATION_OPTION_SELECTOR}}")
        israel_option_texts = [
            el.inner_text().strip()
            for el in option_elements
            if "israel" in el.inner_text().strip().lower()
        ]
        page.keyboard.press("Escape")  # close the menu without selecting yet

        jobs_by_id = {}   # key = canonical job link (for dedup), value = Job

        for location_text in israel_option_texts:
            sep = "&" if "?" in base_url else "?"
            filtered_url = f"{base_url}{sep}{{LOCATION_PARAM_NAME}}={quote(location_text)}"  # (1) quote()
            page.goto(filtered_url, timeout=30000)
            try:
                page.wait_for_selector("{{JOB_SELECTOR}}", timeout=10000)
            except Exception:
                continue   # no jobs in this city right now - move to the next

            for card in page.query_selector_all("{{JOB_SELECTOR}}"):
                job = _read_job_card(card, "{{COMPANY_SLUG}}", "{{LINK_BASE}}",
                                     "{{TITLE_SELECTOR}}", "{{LOCATION_SELECTOR}}",
                                     "{{LINK_SELECTOR}}", fallback_location=location_text)
                if job:
                    jobs_by_id[job.id] = job   # automatic dedup by key

        browser.close()
    return list(jobs_by_id.values())
'''


# ---------------------------------------------------------------------------
# TEMPLATE: SmartRecruiters API (the only one of the four requiring pagination)
# ---------------------------------------------------------------------------

TEMPLATE_SMARTRECRUITERS_API = '''
def get_jobs_smartrecruiters(company_slug: str) -> list[Job]:
    """Fetches jobs from a company using SmartRecruiters, including
    offset/limit pagination."""
    jobs = []
    offset = 0
    limit = 100
    while True:
        url = f"https://api.smartrecruiters.com/v1/companies/{company_slug}/postings"
        r = requests.get(url, params={"offset": offset, "limit": limit}, timeout=15)
        r.raise_for_status()
        data = r.json()
        postings = data.get("content", [])
        if not postings:
            break
        for p in postings:
            title = p.get("name", "")
            loc = p.get("location", {})
            location = f"{loc.get('city', '')}, {loc.get('country', '')}".strip(", ")
            if not is_relevant_location(location):
                continue
            job_id = str(p.get("id") or p.get("ref", ""))
            jobs.append(Job(
                id=job_id, title=title, location=location,
                url=f"https://jobs.smartrecruiters.com/{company_slug}/{job_id}",
                company=company_slug,
            ))
        offset += limit
        if offset >= data.get("totalFound", 0):
            break
    return jobs
'''
