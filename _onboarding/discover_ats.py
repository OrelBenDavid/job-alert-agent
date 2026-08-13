#!/usr/bin/env python3
"""
discover_ats.py - discovery and verification of Israeli companies hosted on a known ATS.

Two modes:
  verify   - takes bot_shortlist.csv (list of known ATS ids) and checks against the
             public API that the id is live, and how many Israel-relevant positions it
             currently has.
  discover - takes israeli_companies_seed.csv (company names), generates candidate
             slugs from each name, and probes them against every platform's API.
             Each hit is a newly found company.

*** Status of this file, updated 2026-08-13 ***

The version handed over with the shortlist had never been executed against a live
endpoint, and said so. It has now been run against all 152 rows. Every adapter below
that this project actually uses (greenhouse / lever / ashby / comeet) has been
corrected where reality differed, and each carries a note saying what was verified and
when. The adapters for platforms NOT in the shortlist (workable / smartrecruiters /
recruitee) are still unverified and are marked as such - they are reachable only from
`discover` mode, which nothing has run yet.

The one substantive correction: **Comeet's `token` is not the company uid.** See
`resolve_comeet_token`.
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
    """UNVERIFIED here - no shortlist row uses SmartRecruiters. (The bot's own
    fetcher for this platform is a separate implementation and predates this file.)"""
    d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    if not d or "content" not in d:
        return None
    return [
        (j.get("name", ""),
         (j.get("location") or {}).get("city", "") + " " + (j.get("location") or {}).get("country", ""))
        for j in d["content"]
    ]


def probe_recruitee(slug):
    """UNVERIFIED - no shortlist row uses Recruitee."""
    d = get_json(f"https://{slug}.recruitee.com/api/offers/")
    if not d or "offers" not in d:
        return None
    return [(j.get("title", ""), j.get("location", "")) for j in d["offers"]]


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
}


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


# --------------------------------------------------------------- discover mode


def slugs_for(name):
    """Generate candidate slugs from a company name, covering the common conventions."""
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    if not base:
        return []
    parts = base.split()
    cands = {"".join(parts), "-".join(parts), parts[0]}
    cands |= {"".join(parts) + s for s in ("inc", "ltd", "io", "ai", "hq")}
    return [c for c in cands if 2 <= len(c) <= 40]


def discover(path="israeli_companies_seed.csv", platforms=("greenhouse", "lever", "ashby"), limit=None):
    """NOT RUN. Comeet is deliberately absent from the default platform list: a Comeet
    probe costs a ~750 KB board-page fetch per candidate slug (see resolve_comeet_token),
    and brute-forcing 7,940 names x several slugs against that is a different order of
    load from the JSON-only platforms. It also needs a throttle and a human review pass
    on the hits before anything is imported."""
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    if limit:
        rows = rows[: int(limit)]
    w = csv.writer(sys.stdout)
    w.writerow(["company", "ats", "id", "jobs", "israel_jobs"])

    def one(r):
        found = []
        for slug in slugs_for(r["name"]):
            for p in platforms:
                jobs = PROBES[p](slug)
                # Only a board with jobs counts as a hit - an empty board is
                # indistinguishable from a wrong slug.
                if jobs:
                    found.append((r["name"], p, slug, len(jobs), count_israel(jobs)))
                    break
            time.sleep(0.2)  # gentle throttle; these APIs are free and shared
        return found

    with ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(one, rows):
            for line in res:
                w.writerow(line)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if mode == "verify":
        verify(*sys.argv[2:])
    else:
        discover(*sys.argv[2:])
