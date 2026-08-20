#!/usr/bin/env python3
"""
resolve_workable_slugs.py - Workable's Israel feed -> importable rows.

    py -3 _onboarding/resolve_workable_slugs.py --out workable_sweep.csv

Two steps, and they are deliberately different jobs:

  1. DISCOVER, from Workable's own public cross-customer feed
     jobs.workable.com/api/v1/jobs?location=Israel - 20 per page, cursor
     paginated, ~8 requests for the whole Israeli slice. This is the only
     thing that feed is used for. It must NOT become the fetch path: it
     returns only postings whose location.countryName is Israel, so it would
     silently drop the qualified-remote roles relevance.py keeps, and a
     filtered feed is indistinguishable from a small board.

  2. RESOLVE each company to its account slug, then score its WHOLE board
     through the account endpoint the fetcher will really use.

*** The slug is read, not guessed ***

The feed identifies a company by a UUID and a display title, neither of which
is the account slug the API needs. Guessing the slug from the title is the
route that produced a third wrong companies in the 2026-08-18 sweep, so it is
not the primary route here.

Instead the company publishes it. jobs.workable.com/api/v1/jobs/{feed_job_id}
returns a `company.url` of the form
    https://jobs.workable.com/company/{token}/jobs-at-{slug}
and the tail after "jobs-at-" is the account slug. Verified 2026-08-19 on 8 of
8 companies tried, including the ones a title-slugifier would get wrong -
"REAL DEV INC" -> real-dev-inc, "D-ID" -> d-id.

Identity is then confirmed on top of that, because it is free: the account
endpoint publishes `name`, so the board says whose it is. A board naming a
different company than the feed did is REPORTED, never silently imported -
that is the one failure mode that would mean a wrong board.

*** The gates ***

Same ladder as gate_comeet_candidates.py, and the same honest limits:
Israel-relevance mirrors fetchers/api.fetch_workable exactly rather than
approximating it; contribution requires an on-family posting AND a physical
Israeli one, because a bare "Remote" at a foreign employer means remote within
that employer's country; stability (two probes ~24h apart) is not applied here
and is an operator action.
"""

import argparse
import csv
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from discover_ats import get_json, UA                                # noqa: E402
from relevance import (is_relevant_location, is_israel_country_code,  # noqa: E402
                       is_israel_location)
from roles import classify as classify_role                          # noqa: E402

FEED = "https://jobs.workable.com/api/v1/jobs?location=Israel"
ACCOUNT = "https://apply.workable.com/api/v1/widget/accounts/%s?details=true"
FEED_JOB = "https://jobs.workable.com/api/v1/jobs/%s"

WORKERS = 6
_SLUG_RE = re.compile(r"/jobs-at-([\w.\-]+)\s*$")


def feed_companies():
    """{company_title: {"id":..., "website":..., "shortcodes": [...]}} from the
    Israel feed. Cursor-paginated; the loop is bounded by totalSize, not by
    trusting nextPageToken to eventually stop."""
    out, token, seen = {}, None, 0
    while True:
        url = FEED + (("&pageToken=" + token) if token else "")
        data = get_json(url)
        if not data:
            break
        for j in data.get("jobs", []):
            company = j.get("company") or {}
            title = (company.get("title") or "").strip()
            if not title:
                continue
            entry = out.setdefault(title, {"id": company.get("id", ""),
                                           "website": company.get("website", ""),
                                           "job_ids": []})
            if j.get("id"):
                entry["job_ids"].append(str(j["id"]))
        seen += len(data.get("jobs", []))
        token = data.get("nextPageToken")
        if not token or seen >= (data.get("totalSize") or 0):
            break
        time.sleep(0.3)
    return out


def slug_from_feed_job(job_id):
    """The account slug the company itself publishes, or None.

    company.url is .../company/{token}/jobs-at-{slug}; the tail is the slug.
    Nothing is derived from the display title here - that is the guessing route
    this file exists to avoid."""
    data = get_json(FEED_JOB % job_id)
    if not data:
        return None
    url = ((data.get("company") or {}).get("url") or "").strip()
    m = _SLUG_RE.search(url)
    return m.group(1) if m else None


def score(jobs):
    """(israel, on_family, physical), exactly as fetch_workable decides."""
    israel = on_family = physical = 0
    for j in jobs:
        title = (j.get("title") or "").strip()
        city = (j.get("city") or "").strip()
        state = (j.get("state") or "").strip()
        country = (j.get("country") or "").strip()
        codes = [e.get("countryCode") for e in (j.get("locations") or [])
                 if isinstance(e, dict)]

        combined = ", ".join(p for p in (city, state, country) if p)
        israeli_code = any(is_israel_country_code(c) for c in codes)
        if not (israeli_code or is_relevant_location(combined, title)):
            continue
        israel += 1
        if classify_role(title)[0] != "blocked":
            on_family += 1
            if israeli_code or is_israel_location(combined):
                physical += 1
    return israel, on_family, physical


def resolve(item):
    title, meta = item
    out = {"company": title, "slug": "", "board_name": "", "jobs": 0,
           "israel_jobs": 0, "on_family": 0, "physical": 0, "reject": None}

    slug = None
    for job_id in meta["job_ids"][:3]:            # a few tries, then give up
        slug = slug_from_feed_job(job_id)
        if slug:
            break
    if not slug:
        out["reject"] = "company published no account slug"
        return out
    out["slug"] = slug

    data = get_json(ACCOUNT % slug)
    if not data:
        out["reject"] = "account endpoint did not answer"
        return out

    out["board_name"] = (data.get("name") or "").strip()
    jobs = data.get("jobs") or []
    if not jobs:
        out["reject"] = "board is empty"
        return out

    out["jobs"] = len(jobs)
    out["israel_jobs"], out["on_family"], out["physical"] = score(jobs)

    if out["on_family"] <= 0:
        out["reject"] = ("no Israel-relevant postings in a target job family "
                         "(%d on board, %d Israel-relevant)"
                         % (out["jobs"], out["israel_jobs"]))
    elif out["physical"] <= 0:
        out["reject"] = ("no PHYSICAL Israeli posting - all %d on-family roles "
                         "are bare-remote at a foreign employer"
                         % out["on_family"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "workable_sweep.csv"))
    args = ap.parse_args()

    companies = feed_companies()
    print("[workable] %d companies in the Israel feed" % len(companies),
          file=sys.stderr)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(resolve, sorted(companies.items())))

    kept = [r for r in results if not r["reject"]]

    # One board, two feed entries, is possible the same way it was on Comeet.
    by_slug, aliases = {}, []
    for r in sorted(kept, key=lambda r: -r["on_family"]):
        if r["slug"] in by_slug:
            aliases.append((r["company"], by_slug[r["slug"]]["company"]))
            continue
        by_slug[r["slug"]] = r
    kept = list(by_slug.values())

    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "size", "route", "ats", "id", "confidence",
                    "jobs", "israel_jobs", "on_family"])
        for r in kept:
            # The board's own name wins over the feed's display title: it is
            # what the company calls itself on the page it publishes.
            w.writerow([r["board_name"] or r["company"], "?",
                        "workable_israel_feed", "workable", r["slug"],
                        "verified", r["jobs"], r["israel_jobs"], r["on_family"]])

    rejects_path = os.path.splitext(args.out)[0] + "_rejected.csv"
    with open(rejects_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "slug", "jobs", "israel_jobs", "reason"])
        for r in results:
            if r["reject"]:
                w.writerow([r["company"], r["slug"], r["jobs"],
                            r["israel_jobs"], r["reject"]])

    print("\n[workable] %d of %d companies passed all applicable gates"
          % (len(kept), len(results)), file=sys.stderr)
    print("[workable] on-family postings: %d (%d physically in Israel)"
          % (sum(r["on_family"] for r in kept),
             sum(r["physical"] for r in kept)), file=sys.stderr)

    reasons = {}
    for r in results:
        if r["reject"]:
            key = r["reject"].split("(")[0].strip()
            reasons[key] = reasons.get(key, 0) + 1
    for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("           rejected %3d  %s" % (n, reason), file=sys.stderr)

    # Reported, never silently accepted: the board naming a different company
    # than the feed did is the one thing that would mean a wrong board.
    for r in kept:
        if r["board_name"] and r["company"].lower() not in r["board_name"].lower() \
                and r["board_name"].lower() not in r["company"].lower():
            print("           NAME DIFFERS  feed=%r board=%r slug=%s"
                  % (r["company"], r["board_name"], r["slug"]), file=sys.stderr)
    for dup, kept_name in aliases:
        print("           alias %r -> same board as %r, dropped"
              % (dup, kept_name), file=sys.stderr)


if __name__ == "__main__":
    main()
