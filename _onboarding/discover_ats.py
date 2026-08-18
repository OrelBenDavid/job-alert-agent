#!/usr/bin/env python3
"""
discover_ats.py - discovery and verification of Israeli companies hosted on a known ATS.

Two modes:
  verify   - takes bot_shortlist.csv (list of known ATS ids) and checks against the
             public API that the id is live, and how many Israel-relevant positions it
             currently has.
  discover - takes israeli_companies_seed.csv (company names), finds each company's
             board by reading its careers page or by guessing a slug, PROVES the board
             belongs to that company, and scores it. Each verified hit is a newly
             found company.

*** Status of this file, updated 2026-08-18 ***

`verify` was run against all 152 shortlist rows on 2026-08-13. `discover` had still
never been executed; it was run for the first time on 2026-08-18 over 451 unprofiled
companies, and what came back changed the design of this file more than the numbers
did. Roughly a third of its raw hits were THE WRONG COMPANY.

What changed as a result, all evidenced in the functions themselves:

  - The bare first-word slug candidate is gone. It produced six matches in 100
    companies and every one was someone else (Align Technology -> "A-LIGN External").
  - Every guessed hit now passes through `confidence_for`, which proves identity from
    the board's own published name, and reports 'unverifiable' rather than 'verified'
    where a platform publishes none. See `board_name`.
  - `looks_like_demo` rejects ATS vendor sample boards, which no name check can catch:
    Recruitee's `google` board really is named "Google".
  - `fingerprint_careers_page` reads the company's own careers page instead of
    guessing. It is exact, and it is the only route to comeet and workday.
  - `discover` stops at the first hit per company (the old inner `break` exited only
    the platform loop), and emits `confidence` and `on_family` so results can be
    ranked and reviewed instead of imported blind.

Adapter status: greenhouse / lever / ashby / comeet verified 2026-08-13; workable /
smartrecruiters / recruitee / workday verified live 2026-08-18. Two carry warnings
worth reading before trusting them - see `probe_recruitee` (100% false positives in
the first live run) and `probe_smartrecruiters` (answers 200 for any slug).

The two substantive corrections to date:
  1. **Comeet's `token` is not the company uid.** See `resolve_comeet_token`.
  2. **A live board is not proof of identity.** See the identity checks section.
"""

import csv
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# The relevance filter is imported from the project rather than re-implemented, so a
# count printed here means the same thing the bot will mean by it at runtime. The
# original file carried its own coarse ISRAEL_RE and warned that it was not the
# project's filter and must not be reused as one - importing the real one removes the
# discrepancy instead of restating it. It also matters for the numbers: the real filter
# keeps qualified-remote roles ("Remote - EMEA") that a location regex drops, and drops
# foreign-anchored remote ("Remote - US") that a naive "is it Israel" test never saw.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from relevance import is_relevant_location   # noqa: E402

# Some ATS endpoints reject requests with an empty or default User-Agent.
UA = {"User-Agent": "Mozilla/5.0 (job-alert-agent ATS discovery)"}

# These APIs are occasionally slow; the timeout is generous but bounded.
TIMEOUT = 20


def get_json(url):
    """GET a URL and return parsed JSON, or None on 404/error.

    Returning None rather than raising keeps a single bad company from aborting a
    whole scan.
    """
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, TimeoutError,
            OSError):
        return None


def get_text(url):
    """GET a URL and return the body as text, or None on error."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status != 200:
                return None
            return r.read().decode("utf-8", "replace")
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, TimeoutError,
            OSError):
        return None


# --------------------------------------------------------------- platform adapters
# Each adapter returns a list of (title, location) tuples - enough to confirm the
# board exists and to count Israeli roles. This is an existence/count probe only;
# the bot's actual job fetching lives in the profile-driven fetchers, not here.


def probe_greenhouse(slug):
    """VERIFIED live 2026-08-13 against `forter` (40 jobs) and `wizprivate`.

    `?content=true` is added deliberately, and it is not cosmetic for a *count*:
    Greenhouse routinely reports `location.name` as "Multiple Locations", and the only
    way to tell whether one of those locations is Israel is the `offices` array. A probe
    reading location.name alone under-reports Israel jobs on exactly the boards most
    likely to have them. The bot's own fetcher already checks both (fetchers/api.py).

    Response shape confirmed: {"jobs": [...], "meta": {"total": N}} where len(jobs)
    == meta.total, i.e. the endpoint returns the whole board in one call.
    """
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not d or "jobs" not in d:
        return None
    out = []
    for j in d["jobs"]:
        location = (j.get("location") or {}).get("name", "") or ""
        offices = ", ".join(o.get("name", "") for o in (j.get("offices") or []))
        # Both fields joined into the location string the counter sees, mirroring the
        # fetcher's "location OR offices" rule.
        out.append((j.get("title", ""), f"{location}, {offices}"))
    return out


def probe_lever(slug, eu=False):
    """VERIFIED live 2026-08-13 against `mobileye` on the EU host (137 postings).

    EU-hosted Lever accounts live on a different API host, and there is no way to know
    which from the id alone - so `verify` tries the US host and falls back to EU. A flat
    JSON array, no pagination, the whole board in one call.
    """
    host = "api.eu.lever.co" if eu else "api.lever.co"
    d = get_json(f"https://{host}/v0/postings/{slug}?mode=json")
    if not isinstance(d, list):
        return None
    return [(j.get("text", ""), (j.get("categories") or {}).get("location", "")) for j in d]


def probe_ashby(slug):
    """VERIFIED live 2026-08-13 against `zafran-security` (26 jobs).

    Shape: {"jobs": [...], "apiVersion": ...}. Location is a plain string on `location`,
    with extra entries in `secondaryLocations` - both are read, because an Israeli role
    listed under a secondary location would otherwise be invisible.
    """
    d = get_json("https://api.ashbyhq.com/posting-api/job-board/" + slug)
    if not d or "jobs" not in d:
        return None
    out = []
    for j in d["jobs"]:
        secondary = ", ".join(
            (s.get("location") or "") if isinstance(s, dict) else str(s)
            for s in (j.get("secondaryLocations") or []))
        out.append((j.get("title", ""), f"{j.get('location', '')}, {secondary}"))
    return out


def probe_workable(slug):
    """UNVERIFIED - no shortlist row uses Workable, so this has never been called
    against a live board. Reachable only from `discover` mode."""
    d = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    if not d or "jobs" not in d:
        return None
    return [(j.get("title", ""), f"{j.get('city','')} {j.get('country','')}") for j in d["jobs"]]


def probe_smartrecruiters(slug):
    """VERIFIED live 2026-08-18 against `lonza` (6 postings), with one trap.

    *** This endpoint cannot distinguish a wrong slug from an empty board. ***
    It answers HTTP 200 with {"content": []} for ANY slug, including pure
    nonsense - checked against 'thiscompanydoesnotexistatall9x'. It also
    publishes no company name, so board_name() cannot rescue it.

    The consequence is handled in discover(): an empty board is only reported
    when the account name can be verified, which SmartRecruiters never can. So
    only a NON-EMPTY SmartRecruiters board is ever treated as a hit here.

    (The bot's own fetcher for this platform is a separate implementation and
    predates this file.)
    """
    d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    if not d or "content" not in d:
        return None
    return [
        (j.get("name", ""),
         (j.get("location") or {}).get("city", "") + " " + (j.get("location") or {}).get("country", ""))
        for j in d["content"]
    ]


def probe_recruitee(slug):
    """VERIFIED live 2026-08-18, and it needs a warning.

    The endpoint shape is right, but this adapter is the least trustworthy one
    here for two measured reasons:

      1. It MISSES real customers. Recruitee accounts on a custom careers domain
         do not answer on {slug}.recruitee.com - checked against several known
         Recruitee users, all None.
      2. It HITS vendor demo boards. `google`, `meta` and `samsung` all answer,
         each with a single posting called "Senior Marketer (Sample)".

    In the live xl run, 100% of this adapter's hits were false positives. It is
    kept because `company_name` in the response feeds board_name(), and because
    looks_like_demo() now removes the demo boards - but a Recruitee hit deserves
    a human glance more than any other platform's.
    """
    d = get_json(f"https://{slug}.recruitee.com/api/offers/")
    if not d or "offers" not in d:
        return None
    return [(j.get("title", ""), j.get("location", "")) for j in d["offers"]]


# --------------------------------------------------------------- Workday

# Workday's public job search is a POST, not a GET, so it needs its own helper.
WORKDAY_API = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"

# The Israel country facet id, per tenant. It is a Workday-internal GUID, so it
# differs per customer and has to be resolved once - exactly like the Comeet
# token, and for the same reason it is baked into the profile at import time
# rather than re-resolved every run.
_workday_facets = {}
_workday_lock = threading.Lock()


def post_json(url, body):
    """POST a JSON body and return parsed JSON, or None on any failure."""
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={**UA, "Content-Type": "application/json",
                     "Accept": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, TimeoutError,
            OSError):
        return None


def resolve_workday_israel_facet(tenant, wd, site):
    """Find the tenant's `locationCountry` facet id for Israel, or None.

    VERIFIED live 2026-08-18 against crowdstrike/wd5/crowdstrikecareers: the
    unfiltered board is 449 postings, and applying this facet returns 13, all
    of them genuinely in Israel ("Israel - Tel Aviv", "Israel - Remote").

    *** Why this matters more here than on any other platform ***

    Workday paginates at 20 per request, so reading a 449-posting board costs 23
    requests per company per RUN. With the facet it is one request returning 13.
    Without it Workday would be too expensive to include at all. This is the
    SKILL's "filter before fetching" rule paying for itself.
    """
    key = (tenant, wd, site)
    with _workday_lock:
        if key in _workday_facets:
            return _workday_facets[key]

    facet = None
    d = post_json(WORKDAY_API.format(tenant=tenant, wd=wd, site=site),
                  {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
    for group in (d or {}).get("facets", []):
        if group.get("facetParameter") != "locationMainGroup":
            continue
        for sub in group.get("values") or []:
            if sub.get("facetParameter") != "locationCountry":
                continue
            for value in sub.get("values") or []:
                if (value.get("descriptor") or "").strip().lower() == "israel":
                    facet = value.get("id")

    with _workday_lock:
        _workday_facets[key] = facet
    return facet


def probe_workday(ident):
    """Probe Workday. The id arrives as 'tenant/wdN/site' - all three are needed.

    VERIFIED live 2026-08-18 against crowdstrike/wd5/crowdstrikecareers (449
    postings, 13 in Israel) and verily/wd1/Verily_Careers (71 postings).

    Two shape notes that matter for the eventual fetcher:
      - `locationsText` is "3 Locations" on multi-site postings, exactly the
        Greenhouse "Multiple Locations" problem. Applying the country facet
        avoids it entirely, because every returned posting is then Israeli.
      - `postedOn` is a RELATIVE string ("Posted Yesterday", "Posted 12 Days
        Ago"), not a timestamp - see posted_days_ago.

    Unlike every other adapter here this cannot be reached by guessing a slug
    from a company name: the tenant/site pair is not derivable from the name.
    It is discovered from the company's own careers page - see
    fingerprint_careers_page.
    """
    parts = ident.split("/")
    if len(parts) != 3:
        return None
    tenant, wd, site = parts
    url = WORKDAY_API.format(tenant=tenant, wd=wd, site=site)
    facet = resolve_workday_israel_facet(tenant, wd, site)
    applied = {"locationCountry": [facet]} if facet else {}

    out, offset = [], 0
    while True:
        d = post_json(url, {"appliedFacets": applied, "limit": 20,
                            "offset": offset, "searchText": ""})
        if not d or "jobPostings" not in d:
            return out or None
        for j in d["jobPostings"]:
            out.append((j.get("title", ""), j.get("locationsText", "")))
        offset += 20
        # Without the facet a large board would page forever; 5 pages is enough
        # to size a candidate, and a real profile will always carry the facet.
        if offset >= min(int(d.get("total") or 0), 100 if facet else 100):
            break
    return out


# --------------------------------------------------------------- Comeet

COMEET_API = "https://www.comeet.co/careers-api/2.0/company/{uid}/positions?token={token}"
COMEET_BOARD = "https://www.comeet.co/jobs/{slug}/{uid}"

# Comeet tokens are long uppercase hex-ish strings. Deliberately loose: the candidate is
# only ever *proposed* by this pattern and then confirmed by a real API call below, so a
# false positive costs one request and a false negative costs the company.
_COMEET_TOKEN_RE = re.compile(r'"token"\s*:\s*"([0-9A-Za-z]{12,64})"')

# Resolved tokens are cached for the process. Both modes hit the same company more than
# once (verify does not, but a retry or a re-run within one process would), and the board
# page is ~750 KB - by far the most expensive request in this file.
_comeet_tokens = {}
_comeet_lock = threading.Lock()


def resolve_comeet_token(slug, uid):
    """Find the API token for one Comeet company, or None.

    *** This is the correction that mattered most in the whole file ***

    The handed-over version built the endpoint as `...?token=<uid>` and flagged that
    assumption as the least certain thing in the file. It is wrong, and it fails closed
    rather than loudly: every Comeet row returned HTTP 400
    {"message": "Account uid or token are not valid"}, which the old `get_json` turned
    into None, which `verify` reported as `dead_or_blocked`. All 111 Comeet companies -
    73% of the shortlist - would have been written off as dead.

    The uid and the token are two different values. Comeet's own error messages
    distinguish them ("invalid company id" vs "Account uid or token are not valid"),
    which is what showed the uid was in fact correct and only the token was not.

    The token is public - it ships in the JSON blob embedded in the company's
    Comeet-hosted board page, next to its `company_uid`:

        "company_uid": "71.00A", "token": "17A5E876...", "slug": "SolarEdge"

    It is per-company, not shared: SolarEdge's token against VAST Data's uid returns
    400. So resolving it is one extra page fetch per company, once.

    Every candidate the regex proposes is CONFIRMED with a real API call before being
    returned, so a token from this function is verified by construction rather than
    pattern-matched and hoped for.
    """
    key = (slug, uid)
    with _comeet_lock:
        if key in _comeet_tokens:
            return _comeet_tokens[key]

    token = None
    html = get_text(COMEET_BOARD.format(slug=urllib.parse.quote(slug), uid=uid))
    if html:
        # Prefer a token that sits next to THIS company's uid - a board page can embed
        # more than one token-shaped string, and picking the wrong one silently profiles
        # the wrong company.
        near = re.search(
            r'"company_uid"\s*:\s*"' + re.escape(uid) + r'"\s*,\s*"token"\s*:\s*"([0-9A-Za-z]{12,64})"',
            html)
        candidates = ([near.group(1)] if near else []) + _COMEET_TOKEN_RE.findall(html)
        seen = set()
        for cand in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            if get_json(COMEET_API.format(uid=uid, token=cand)) is not None:
                token = cand
                break

    with _comeet_lock:
        _comeet_tokens[key] = token
    return token


def probe_comeet(pair):
    """Probe Comeet. The id arrives as 'slug/uid'; both parts are needed.

    VERIFIED live 2026-08-13 against SolarEdge (71.00A, 110 positions) and the rest of
    the shortlist. The token is resolved per company - see resolve_comeet_token.

    Shape: a flat JSON array of positions, the whole board in one call, no pagination.
    Location is a nested object (`location.name`); the per-posting link is
    `url_comeet_hosted_page`.
    """
    if "/" not in pair:
        return None
    slug, uid = pair.split("/", 1)
    token = resolve_comeet_token(slug, uid)
    if token is None:
        return None
    d = get_json(COMEET_API.format(uid=uid, token=token))
    if not isinstance(d, list):
        return None
    return [(j.get("name", ""), (j.get("location") or {}).get("name", "")) for j in d]


# Ordered most-common first so discovery spends fewer requests on rare platforms.
PROBES = {
    "greenhouse": probe_greenhouse,
    "lever": probe_lever,
    "lever_eu": lambda s: probe_lever(s, eu=True),
    "ashby": probe_ashby,
    "workable": probe_workable,
    "smartrecruiters": probe_smartrecruiters,
    "recruitee": probe_recruitee,
    "comeet": probe_comeet,
    "workday": probe_workday,
}

# Platforms a slug can meaningfully be GUESSED for. Three are excluded on
# purpose, for three different reasons - all of them measured:
#
#   comeet   - a probe costs a ~750 KB board page per candidate slug.
#   workday  - a tenant/site pair is not derivable from a company name at all.
#   workable - *** IT RATE-LIMITS, AND HARD. *** Measured 2026-08-18: a sweep of
#              100 companies issued 3,281 requests and drew 425 HTTP 429s, and
#              an isolated six-request probe immediately afterwards returned 429
#              for every single one while greenhouse, lever, ashby, recruitee and
#              smartrecruiters all answered a clean 404. apply.workable.com was
#              the sole source. It recovers after a pause, so this is throttling
#              rather than a ban, but brute-forcing ~7 slugs per company across
#              7,800 companies is exactly the traffic shape that triggers it.
#
#              This also invalidates an earlier reading in this project: Workable
#              appeared to have near-zero Israeli market share because it
#              returned no hits across 451 probed companies. That measurement was
#              taken WHILE the sweep was throttling it, and get_json turns a 429
#              into None - which is indistinguishable from "no such board". Its
#              real share is unmeasured, not zero.
#
# All three remain in PROBES and are reached through fingerprint_careers_page,
# which asks the company for its own id and costs one request instead of dozens.
GUESSABLE = ("greenhouse", "lever", "lever_eu", "ashby",
             "smartrecruiters", "recruitee")


def count_israel(jobs):
    """Count how many returned roles are relevant, using the PROJECT's filter."""
    return sum(1 for _, loc in jobs if is_relevant_location(loc or ""))


# --------------------------------------------------------------- verify mode


def verify(path="bot_shortlist.csv", out_path=None):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))

    def one(r):
        ats, ident = r["ats"], r["id"]
        fn = PROBES.get(ats)
        if fn is None:
            # No adapter for this platform - report rather than silently pass.
            return {**r, "status": "no_probe", "jobs": "", "israel_jobs": "", "host": ""}
        host = ats
        if ats == "lever":
            jobs = probe_lever(ident)
            if jobs is None:
                # A US-host miss may just mean the account is EU-hosted. Which host
                # answered is recorded, because the profile needs it.
                jobs = probe_lever(ident, eu=True)
                host = "lever_eu" if jobs is not None else "lever"
        else:
            jobs = fn(ident)
        if jobs is None:
            return {**r, "status": "dead_or_blocked", "jobs": "", "israel_jobs": "",
                    "host": ""}
        return {**r, "status": "ok", "jobs": len(jobs),
                "israel_jobs": count_israel(jobs), "host": host}

    # 8 workers is fast enough without tripping rate limits on the smaller APIs.
    with ThreadPoolExecutor(max_workers=8) as ex:
        out = list(ex.map(one, rows))

    stream = open(out_path, "w", encoding="utf-8", newline="") if out_path else sys.stdout
    try:
        w = csv.DictWriter(stream, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    finally:
        if out_path:
            stream.close()
    return out


# --------------------------------------------------------------- identity checks
#
# *** Why this section exists (added 2026-08-18) ***
#
# Discovery guesses a slug and asks a board "do you exist". A board that answers
# yes is NOT evidence that it belongs to the company whose name produced the
# guess, and the live xl run proved it: roughly a third of the hits were other
# companies. Nothing downstream can catch that - the profile loads, the fetch
# returns 200, the health gate sees a healthy count - so it has to be caught here.
#
# Two independent checks, because they catch different things:
#   verify_identity  - the board's own name does not match the company's
#   looks_like_demo  - the board is a vendor sample, not a real employer

# Words that may legitimately differ between a company's name and its board's
# name. "Tenable" vs "Tenable, Inc." is the same company; "Align" vs "Align
# External" is not, which is why this list is short and closed rather than a
# general stopword list.
_CORP_SUFFIXES = {
    "inc", "inc.", "llc", "ltd", "ltd.", "limited", "corp", "corporation",
    "co", "company", "group", "holdings", "plc", "gmbh", "ag", "sa", "nv",
    "bv", "israel", "il", "global", "international", "technologies",
    "technology", "tech", "labs", "systems", "solutions", "software",
    "the", "careers", "jobs",
}


def _norm_name(s):
    """Lowercase, strip punctuation, drop corporate suffix words."""
    tokens = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()
    kept = [t for t in tokens if t not in _CORP_SUFFIXES]
    return kept or tokens


def names_match(seed_name, board_name):
    """Is `board_name` plausibly the same company as `seed_name`?

    Deliberately strict in one specific direction: a prefix match is accepted
    ONLY when the leftover is a corporate suffix. That is the rule that keeps
    "Tenable" == "Tenable, Inc." while rejecting AT&T ("att") == Attio
    ("attio"), where the leftover "io" is part of another company's name.
    """
    a, b = _norm_name(seed_name), _norm_name(board_name)
    if not a or not b:
        return False
    ja, jb = "".join(a), "".join(b)
    if ja == jb:
        return True
    # One name fully contains the other AND every extra token is a suffix word.
    sa, sb = set(a), set(b)
    if sa <= sb or sb <= sa:
        return True
    # Concatenated forms: accept only if the remainder is empty (handled above)
    # or the shorter is a prefix of the longer and the two differ by a suffix
    # token that _norm_name did not already strip.
    return False


def board_name(platform, slug):
    """The board's own company name, or None if the platform does not publish one.

    VERIFIED live 2026-08-18:
      greenhouse - /v1/boards/{slug} returns {"name": ...}. `align` -> "A-LIGN
                   External", `tenableinc` -> "Tenable, Inc.". This is the check
                   that would have caught the Align Technology collision.
      workable   - the widget endpoint returns {"name": "Hotjar", "jobs": []}.
                   Note this ALSO means a live-but-empty Workable board is
                   distinguishable from a wrong slug, which discover() used to
                   treat as the same thing.
      recruitee  - offers carry `company_name`.

      lever      - the JSON API publishes no name, but the hosted board PAGE's
                   <title> is exactly the company name: `palantir` -> "Palantir
                   Technologies", `lendbuzz` -> "Lendbuzz", `houzz` -> "Houzz".
      ashby      - same idea, formatted "{Company} Jobs": `attio` -> "Attio
                   Jobs". This is what rejects the AT&T -> `attio` collision,
                   which the JSON API alone could not. NOT always populated -
                   `lemonade` returns a bare "Jobs" - so it still falls through
                   to None sometimes, which is honest rather than guessed.

      smartrecruiters - CORRECTED 2026-08-19. This previously returned None,
                   on the basis that no company-name ENDPOINT exists. That is
                   true and it was the wrong place to look: every posting
                   carries a `company` object, so the name is in the listing
                   response that was already being fetched. Verified against all
                   12 SmartRecruiters candidates from the sweep - `fairtility`
                   -> "Fairtility", `nexarinc` -> "Nexar Inc", `slauthio` ->
                   "Slauth.io", `ta9` -> "TA9". All 12 had been sitting in the
                   `unverifiable` bucket for want of this one field.

    A None here means "unverifiable", NOT "verified" - see confidence_for,
    which then falls back to the board's own posting text.

    The two page fetches are only ever reached for a slug that already returned
    postings, so they cost one extra request per candidate hit, not per probe.
    """
    if platform == "greenhouse":
        d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        return (d or {}).get("name")
    if platform == "workable":
        d = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
        return (d or {}).get("name")
    if platform == "recruitee":
        d = get_json(f"https://{slug}.recruitee.com/api/offers/")
        offers = (d or {}).get("offers") or []
        return offers[0].get("company_name") if offers else None
    if platform == "smartrecruiters":
        d = get_json("https://api.smartrecruiters.com/v1/companies/"
                     f"{slug}/postings?limit=1")
        content = (d or {}).get("content") or []
        return ((content[0].get("company") or {}).get("name") or "").strip() or None
    if platform in ("lever", "lever_eu"):
        return _page_title(f"https://jobs.lever.co/{slug}")
    if platform == "ashby":
        title = _page_title(f"https://jobs.ashbyhq.com/{urllib.parse.quote(slug)}")
        if not title:
            return None
        # \s* not \s+ : an un-branded Ashby board's title is the bare word
        # "Jobs" with nothing before it (measured on `lemonade`). Requiring
        # leading whitespace left "Jobs" standing, which then matched no
        # company and REJECTED a real one - Lemonade carries 24 on-family
        # Israeli roles, so that false negative was expensive.
        name = re.sub(r"\s*\bJobs\b\s*$", "", title).strip()
        # A bare "Jobs" carries no identity - report it as unknown, not as a
        # company called "Jobs" that would then fail to match anything.
        return name or None
    return None


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def _page_title(url):
    """The <title> of a page, whitespace-collapsed, or None."""
    html = get_text(url)
    if not html:
        return None
    m = _TITLE_RE.search(html)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


# Vendor sample boards answer every identity check perfectly - Recruitee's
# `google` board really is named "Google" - so name matching alone does not
# catch them. What gives them away is the content: one posting, titled as a
# sample. Measured 2026-08-18: recruitee `google`, `meta` and `samsung` each
# returned exactly one posting, "Senior Marketer (Sample)".
_DEMO_TITLE_RE = re.compile(r"\b(sample|example|demo|test job|dummy)\b", re.I)


def looks_like_demo(jobs):
    """True if this board looks like an ATS vendor's demo account."""
    if not jobs or len(jobs) > 2:
        return False
    return all(_DEMO_TITLE_RE.search(title or "") for title, _ in jobs)


def company_named_in_postings(seed_name, platform, slug, threshold=0.5):
    """Do the board's own job descriptions name this company?

    The fallback for a platform that publishes no company name anywhere. It is
    evidence of the same kind - the company's own words on its own board - and
    in practice it is stronger than a title, because a description that says
    "every role at Lemonade" cannot plausibly belong to anyone else.

    VERIFIED live 2026-08-19 on the two Ashby candidates the name check could
    not resolve: `lemonade` names Lemonade in 34 of 34 postings, `simply` names
    Simply in 4 of 5. An un-branded Ashby board reports its title as the bare
    word "Jobs", which is why those two had no name to check against.

    Deliberately limited to platforms whose LISTING already carries the
    description, so this costs one request and never a per-posting walk.
    Requires a majority of postings to mention the name: one mention could be a
    passing reference to a partner or a customer, most of them cannot.
    """
    tokens = _norm_name(seed_name)
    if not tokens:
        return False
    # The distinctive token is the longest one - "Play" is common, "Perfect"
    # less so; matching on the whole normalised string would miss "Nexar Inc"
    # written as plain "Nexar" in prose.
    needle = max(tokens, key=len)
    if len(needle) < 4:            # too short to be distinctive in prose
        return False
    pattern = re.compile(r"\b" + re.escape(needle) + r"\b", re.I)

    if platform == "ashby":
        d = get_json("https://api.ashbyhq.com/posting-api/job-board/" + slug)
        texts = [(j.get("descriptionPlain") or "") for j in (d or {}).get("jobs", [])]
    else:
        return False               # only verified for Ashby; do not guess

    if not texts:
        return False
    hits = sum(1 for t in texts if pattern.search(t))
    return hits >= max(1, int(len(texts) * threshold))


def confidence_for(seed_name, platform, slug, jobs):
    """One of 'verified' / 'unverifiable' / 'rejected'.

    'unverifiable' is a real and common answer - Lever and Ashby publish no
    company name - and it is deliberately NOT the same as 'verified'. The
    import step should treat it as needing a human glance, which is cheap at a
    few hundred candidates and is the difference between a curated list and a
    list that quietly contains other people's companies.
    """
    if looks_like_demo(jobs):
        return "rejected"
    name = board_name(platform, slug)
    if name is not None:
        return "verified" if names_match(seed_name, name) else "rejected"
    # No published name. Before giving up, ask the board's own text - see
    # company_named_in_postings. This is what recovers an un-branded Ashby
    # board, whose title is the bare word "Jobs".
    if company_named_in_postings(seed_name, platform, slug):
        return "verified"
    return "unverifiable"


# --------------------------------------------------------------- careers-page route
#
# *** Why this exists, and why it outranks slug guessing (added 2026-08-18) ***
#
# Slug guessing asks every board "is there a company called X here". Reading the
# company's own careers page asks the company "which board do you use", and it
# answers with the exact id. There is nothing to collide with, so this route
# needs no identity check at all.
#
# It is also the ONLY practical route to the two platforms that matter most:
#   comeet  - 72% of this project's existing corpus, and the single most common
#             ATS in a 600-page sweep of Israeli careers pages (18 of 68 hits).
#             discover() cannot brute-force it: resolving a Comeet token costs a
#             ~750 KB board-page fetch per candidate slug.
#   workday - the second most common (14 of 68), and its tenant/site pair simply
#             is not derivable from a company name.
#
# Measured over 600 real careers URLs from the seed list on 2026-08-18:
#   457/600 (76%) reachable; of those, 68 (15%) embed a known ATS.
# The rest are static HTML needing per-company selectors (61%) or JavaScript
# shells needing a browser (24%). schema.org JSON-LD JobPosting appeared on
# 4 of 457 pages (0.9%) - it is not a viable generic route in this market.

_FINGERPRINTS = [
    # Comeet publishes slug AND uid in the board URL; both are needed, and the
    # token is then resolved by the existing resolve_comeet_token.
    ("comeet", re.compile(r"comeet\.co/jobs/([A-Za-z0-9_.\-]+)/([0-9A-Z.]+)")),
    ("comeet", re.compile(r"careers-api/2\.0/company/([0-9A-Z.]+)")),
    ("workday", re.compile(
        r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)")),
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([\w-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.\-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([\w-]+)")),
    ("smartrecruiters", re.compile(r"careers\.smartrecruiters\.com/([\w-]+)")),
    ("recruitee", re.compile(r"([\w-]+)\.recruitee\.com")),
]


def comeet_ident_from_uid(company_name, uid):
    """Turn a bare Comeet uid into the 'slug/uid' probe_comeet needs, or None.

    Some careers pages embed only the API URL (".../company/{uid}/positions"),
    with the board slug nowhere in the raw HTML - measured on proteanTecs, whose
    uid D5.00E appears alone. The slug is still needed, because the TOKEN lives
    on the board page and the board page URL contains the slug.

    So the slug is guessed from the company name and confirmed the same way
    resolve_comeet_token confirms a token: by a real API call. Verified live
    2026-08-18 - proteanTecs resolved on the first candidate and returned 14
    postings. This is safe to guess where a platform slug is not, because the
    uid is the authoritative half of the pair: a wrong slug with a right uid
    yields no token, and a right slug with a wrong uid is not reachable here at
    all. The company's identity is pinned by the uid it published.
    """
    for slug in slugs_for(company_name):
        if resolve_comeet_token(slug, uid):
            return f"{slug}/{uid}"
    return None


def fingerprint_careers_page(careers_url, company_name=""):
    """Read one careers page and return [(platform, identifier), ...] it embeds.

    The identifier is in each platform's own `verify`/probe format, so a result
    here can be handed straight to PROBES.

    A page can legitimately fingerprint as nothing: 61% of reachable Israeli
    careers pages in the 2026-08-18 sweep embedded no known ATS at all. Cato
    Networks is a worked example - it is a real Greenhouse customer, but its
    careers page loads the board through JavaScript, so nothing is visible in
    the raw HTML and only slug guessing finds it. The two routes are
    complementary, not redundant.
    """
    if not careers_url:
        return []
    url = careers_url if careers_url.startswith("http") else "https://" + careers_url
    html = get_text(url)
    if not html:
        return []

    found, seen = [], set()
    for platform, rx in _FINGERPRINTS:
        for m in rx.finditer(html):
            groups = [g for g in m.groups() if g]
            if platform == "comeet":
                # Two patterns: (slug, uid) or a bare (uid,). The bare form is
                # common enough to be worth recovering rather than dropping.
                if len(groups) >= 2:
                    ident = f"{groups[0]}/{groups[1]}"
                else:
                    ident = comeet_ident_from_uid(company_name, groups[0])
                    if not ident:
                        continue
            elif platform == "workday":
                if len(groups) < 3:
                    continue
                ident = "/".join(groups[:3])
            else:
                ident = groups[0]
            key = (platform, ident)
            if key not in seen:
                seen.add(key)
                found.append(key)
    return found


# --------------------------------------------------------------- scoring
#
# A company's value to this bot is NOT its board size. Measured live across all
# 145 profiled companies on 2026-08-18: 1,319 Israel-relevant postings, of which
# 856 are in the user's four target families and 461 survive the seniority title
# check. Ranking on raw counts would put a 250-posting sales board above a
# 20-posting backend one.
#
# The primary key is the ON-FAMILY count rather than the fully-filtered one, and
# that choice was measured too. Of the 41 profiled companies currently producing
# zero deliverable postings, only 21 are structurally wrong (14 have no Israeli
# roles at all, 7 have no on-family roles); the other 20 - Cloudinary, Datadog,
# Chargeflow among them - have Israeli on-family roles and simply nothing junior
# open today. Ranking on the fully-filtered count would discard those 20.

try:
    from roles import classify as _classify_role
except ImportError:                      # roles.py is newer than this file
    _classify_role = None


def count_on_family(jobs):
    """Israel-relevant roles that are not in a blocked job family.

    Falls back to the Israel count if roles.py is unavailable, which keeps this
    script runnable against an older checkout rather than failing at import.
    """
    if _classify_role is None:
        return count_israel(jobs)
    n = 0
    for title, loc in jobs:
        if is_relevant_location(loc or "", title or "") and \
                _classify_role(title or "")[0] != "blocked":
            n += 1
    return n


# --------------------------------------------------------------- discover mode


def slugs_for(name):
    """Generate candidate slugs from a company name, covering the common conventions.

    *** The bare first word was REMOVED on 2026-08-18, with evidence. ***

    This used to include `parts[0]` - the company's first word - for every name.
    Run live over the 100 unprofiled `xl` companies, that one candidate produced
    six matches and every one of them was a DIFFERENT COMPANY:

        Align Technology       -> greenhouse `align`       = "A-LIGN External"
        Allied Universal ...   -> greenhouse `allied`
        Cornerstone OnDemand   -> greenhouse `cornerstone`
        Change Healthcare      -> ashby `change`
        Novo Nordisk           -> ashby `novo`
        Samsung Semiconductors -> recruitee `samsung`

    That failure mode is the worst one this project has, because it is silent:
    the board is live, the fetch succeeds, the health gate is satisfied, and the
    bot monitors the wrong company forever. A first word is simply not evidence
    of identity - "Change", "Novo" and "Allied" are other people's whole names.

    It is still generated for a SINGLE-word company name, where `parts[0]` is
    the entire name rather than a fragment of it.

    The +inc/+ltd/+io/+ai/+hq suffixes are kept: they earned two genuine hits in
    the same run (`tenableinc` = Tenable, `innodatainc` = Innodata) against one
    false positive (AT&T -> `attio`, which is the company Attio), and unlike the
    first word they are now caught by verify_identity below.
    """
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    if not base:
        return []
    parts = base.split()
    cands = {"".join(parts), "-".join(parts)}
    if len(parts) == 1:
        cands.add(parts[0])
    cands |= {"".join(parts) + s for s in ("inc", "ltd", "io", "ai", "hq")}
    return [c for c in cands if 2 <= len(c) <= 40]


def discover(path="israeli_companies_seed.csv", platforms=None, limit=None,
             workers=8):
    """Find each company's board, and score it.

    *** Rewritten 2026-08-18, after the first run this file ever had. ***

    Two routes, tried in this order, because they are not equally trustworthy:

      1. The company's own careers page (fingerprint_careers_page). Exact, needs
         no identity check, and the only way to reach Comeet and Workday. Costs
         one GET, and only ~16% of seed rows carry a careers URL.
      2. Slug guessing across GUESSABLE platforms, every hit then passed through
         confidence_for. In the live xl run this route's raw hits were ~1/3 other
         companies; that is what the confidence column exists to surface.

    Three fixes to the previous version, all of which changed results:
      - The inner `break` exited only the platform loop, so a company kept
        probing further slug candidates after it had already been found, and
        could emit several rows for one company. It now stops at the first hit.
      - An empty board was treated as a miss. For Workable that is wrong: it
        returns the account name alongside an empty job list, so a live account
        with nothing open is distinguishable from a bad slug. Empty boards are
        now reported with jobs=0 rather than dropped.
      - Output carries `confidence` and `on_family`, so the result can be ranked
        and reviewed instead of just imported.

    Output is CSV on stdout, ordered as found; sort by on_family to rank.
    """
    platforms = tuple(platforms) if platforms else GUESSABLE
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    if limit:
        rows = rows[: int(limit)]

    w = csv.writer(sys.stdout)
    w.writerow(["company", "size", "route", "ats", "id", "confidence",
                "jobs", "israel_jobs", "on_family"])

    def one(r):
        name = r["name"]

        # --- route 1: the company's own careers page
        for platform, ident in fingerprint_careers_page(r.get("careers", ""), name):
            probe = PROBES.get(platform)
            if probe is None:
                continue
            jobs = probe(ident)
            if jobs is None:
                continue
            # No identity check: the company published this id itself.
            return [(name, r.get("size", ""), "careers_page", platform, ident,
                     "verified", len(jobs), count_israel(jobs),
                     count_on_family(jobs))]

        # --- route 2: guess a slug, then prove it belongs to this company
        for slug in slugs_for(name):
            for platform in platforms:
                jobs = PROBES[platform](slug)
                if jobs is None:
                    continue
                confidence = confidence_for(name, platform, slug, jobs)
                if confidence == "rejected":
                    continue
                # *** An empty board only counts where the account can be PROVEN
                # to exist. *** Reporting empty boards at all exists for
                # Workable, which returns the account name beside an empty job
                # list. SmartRecruiters looks identical and is not: measured
                # 2026-08-18, /v1/companies/{anything}/postings returns HTTP 200
                # with {"content": []} for slugs that are pure nonsense, and it
                # publishes no company name - so every company in a 50-row
                # sample "matched" SmartRecruiters with 0 jobs. Requiring a
                # verified name for the empty case keeps Workable's real signal
                # and drops that noise.
                if not jobs and confidence != "verified":
                    continue
                return [(name, r.get("size", ""), "slug_guess", platform, slug,
                         confidence, len(jobs), count_israel(jobs),
                         count_on_family(jobs))]
            time.sleep(0.2)   # gentle throttle; these APIs are free and shared
        return []

    with ThreadPoolExecutor(max_workers=int(workers)) as ex:
        for res in ex.map(one, rows):
            for line in res:
                w.writerow(line)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if mode == "verify":
        verify(*sys.argv[2:])
    else:
        discover(*sys.argv[2:])
