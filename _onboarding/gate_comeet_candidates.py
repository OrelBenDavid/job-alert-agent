#!/usr/bin/env python3
"""
gate_comeet_candidates.py - turn Common Crawl Comeet tenants into importable rows.

    py -3 _onboarding/gate_comeet_candidates.py --out comeet_sweep.csv

Input is comeet_candidates.csv (slug/uid pairs harvested from the Common Crawl
URL index - see EXPANSION_STRATEGY.md section A1). Output is a row per surviving
candidate in the sweep-CSV shape import_companies.py already reads, so the
import path is the existing one and nothing new writes profiles.

*** Why the identity gate is free here, and is not a shortcut ***

The discovery sweep this replaces started from a company NAME and guessed a
slug, so it had to prove afterwards that the board it found belonged to the
company it was looking for - and a third of its raw hits were somebody else
(Align Technology -> "A-LIGN External").

This route runs the other way and the question does not arise. A Comeet board
page URL is /jobs/{slug}/{uid}; the pair identifies one board, the token only
resolves for the right pair, and the board's own API publishes its owner in
`company_name` ("Autobrains Technologies"). So the company is not matched, it
is READ - from the board itself. There is no name comparison anywhere in this
file, because there is nothing to compare.

That also means the name written into the profile is the board's own, not a
seed list's. Where the two would disagree, the board is right: it is what the
company calls itself on the page it publishes.

*** The gates, and which of them this file can actually apply ***

  1. Identity      - free, see above.
  2. Liveness      - the token resolves and the API returns a position array.
  3. Israel        - mirrors fetchers/api.fetch_comeet EXACTLY, reading
                     location.country, and label+city joined for the text test.
                     NOT an approximation of it: the 2026-08-18 sweep predicted
                     +606 on-family and delivered +391 because its probe was
                     looser than the fetcher, and that gap is the single
                     documented reason its forecasts were wrong.
  4. Contribution  - on_family > 0, the same rule import_companies.py applies
                     to sweep rows. A board with no Israel-relevant posting in
                     a target family buys a health-gate entry that never
                     produces an alert.
  5. Stability     - NOT applied here. It needs two probes ~24h apart, which is
                     an operator action, not something one run of a script can
                     do. Run this file twice on separate days and diff the two
                     CSVs before importing a batch you care about.
"""

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from discover_ats import resolve_comeet_token, get_json, COMEET_API  # noqa: E402
from relevance import (is_relevant_location, is_israel_country_code,  # noqa: E402
                       is_israel_location)
from roles import classify as classify_role                          # noqa: E402

CANDIDATES_CSV = os.path.join(HERE, "comeet_candidates.csv")

# Politeness. Resolving one token costs a ~750 KB board-page fetch, so this is
# bounded by courtesy to comeet.co rather than by local CPU. The 2026-08-18
# sweep drew exactly one HTTP 429 in 288,000 requests at a comparable width.
WORKERS = 6

# *** The company-level test, and why it is not a change to relevance.py ***
#
# `is_relevant_location` keeps a bare "Remote" on purpose: an Israeli company
# hiring remotely is exactly what this bot should deliver, and a posting is
# judged fail-open because losing a real job costs far more than showing a
# spare one. That rule is right and is not touched here.
#
# What it cannot do is judge a COMPANY. Measured over this batch: 9 candidates
# passed the on-family gate with a combined ~70 postings and not one role in
# Israel. All were US employers whose location fields say country=US and whose
# label or city happens to read "Remote" - CapsLock ("PA, US" / city "Remote"),
# MRIoA ("Remote - work from home (Pacific)" / Los Angeles), BDR Solutions
# ("Chula Vista, CA"), OuterBox, ROI Agency, Medvidi. Their "Remote" means
# remote within the United States.
#
# Note the shape of the leak: `location.country` is "US" on every one of them,
# and is_israel_country_code is ADDITIVE by design - a foreign code identifies
# nobody and rejects nobody. That is a deliberate, load-bearing choice in
# relevance.py and changing it is out of scope here.
#
# So the test is applied where it belongs: at admission. A company with no
# physical Israeli role in a target family is not an Israeli employer, and
# importing it buys eight fetches a day forever to deliver roles nobody here
# can take. This is Gate 4 - contribution - not a relevance change: a company
# WITH an Israeli presence still gets the fail-open per-posting rule in full,
# qualified remote included.
ALL_REMOTE_NO_ISRAEL = (
    "no PHYSICAL Israeli posting - all %d on-family roles are bare-remote at a "
    "foreign employer")


def score(positions):
    """(israel_jobs, on_family, on_family_physical), the way fetch_comeet decides.

    The three fields are read for what each is good for, per the platform
    profile: `country` is a picker value and decides Israel outright, while
    `name` (a free-text label) and `city` are JOINED for the text test - which
    is what lets the qualified-remote rule see "Remote" and "New York" on one
    posting and reject it.

    The third number is the one that does the real work at this scale - see
    ALL_REMOTE_NO_ISRAEL below.
    """
    israel = on_family = physical = 0
    for p in positions:
        loc = p.get("location") or {}
        label = (loc.get("name") or "").strip()
        city = (loc.get("city") or "").strip()
        country = loc.get("country") or ""
        title = (p.get("name") or "").strip()

        combined = ", ".join(part for part in (label, city) if part)
        if not (is_israel_country_code(country)
                or is_relevant_location(combined, title)):
            continue
        israel += 1
        if classify_role(title)[0] != "blocked":
            on_family += 1
            if is_israel_country_code(country) or is_israel_location(combined):
                physical += 1
    return israel, on_family, physical


def probe(row):
    """One candidate -> a result dict. Never raises: a candidate that cannot be
    reached is a reported reject, not a dead run."""
    slug, uid = row["slug"], row["uid"]
    out = {"slug": slug, "uid": uid, "company": "", "jobs": 0,
           "israel_jobs": 0, "on_family": 0, "physical": 0, "reject": None}

    try:
        token = resolve_comeet_token(slug, uid)
    except Exception as e:
        out["reject"] = "token lookup failed (%s)" % type(e).__name__
        return out
    if not token:
        # The board page did not yield a token. Per dead_rows_reresolution.md a
        # known-good uid with a WRONG slug returns the same shell page, so this
        # means "this pair does not resolve", never "this account is closed".
        out["reject"] = "no token for this slug/uid pair"
        return out

    try:
        positions = get_json(COMEET_API.format(uid=uid, token=token))
    except Exception as e:
        out["reject"] = "api call failed (%s)" % type(e).__name__
        return out
    if not isinstance(positions, list):
        out["reject"] = "api did not return a position array"
        return out
    if not positions:
        out["reject"] = "board is empty"
        return out

    out["company"] = (positions[0].get("company_name") or "").strip()
    if not out["company"]:
        # Nothing to name the profile after. Every board observed publishes
        # this, so treat its absence as a reason to look rather than to guess.
        out["reject"] = "board publishes no company_name"
        return out

    out["jobs"] = len(positions)
    out["israel_jobs"], out["on_family"], out["physical"] = score(positions)

    if out["on_family"] <= 0:
        out["reject"] = ("no Israel-relevant postings in a target job family "
                         "(%d on board, %d Israel-relevant)"
                         % (out["jobs"], out["israel_jobs"]))
    elif out["physical"] <= 0:
        out["reject"] = ALL_REMOTE_NO_ISRAEL % out["on_family"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=CANDIDATES_CSV)
    ap.add_argument("--out", default=os.path.join(HERE, "comeet_sweep.csv"))
    ap.add_argument("--include-profiled", action="store_true",
                    help="also probe pairs already in profiles/companies/ "
                         "(default: skip them - they are already monitored)")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not args.include_profiled:
        rows = [r for r in rows if r.get("already_profiled") != "yes"]
    if args.limit:
        rows = rows[:args.limit]

    print("[gate] probing %d candidates" % len(rows), file=sys.stderr)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(probe, rows))

    kept = [r for r in results if not r["reject"]]
    kept.sort(key=lambda r: -r["on_family"])

    # *** One board can be crawled under more than one slug. ***
    #
    # The uid is the board; the slug is only how a page linked to it. Measured
    # in this batch: 39.004 appears as both `mrioa` and
    # `medicalreviewinstituteofamerica`, and 35.000 as both `walmart` and
    # `aspectiva` (Walmart acquired Aspectiva, and the old slug still resolves).
    #
    # Both spellings produce the SAME api.endpoint, because the endpoint is
    # built from uid+token and the slug never appears in it. So a duplicate
    # here would be two profiles and two state files monitoring one board -
    # every posting diffed and alerted twice. That is the gong.io defect
    # exactly, and it is cheaper to prevent here than to detect downstream.
    #
    # The survivor is the first after the sort, which is deterministic.
    by_uid, aliases = {}, []
    for r in kept:
        uid = r["uid"]
        if uid in by_uid:
            aliases.append((r["slug"], uid, by_uid[uid]["slug"]))
            continue
        by_uid[uid] = r
    kept = list(by_uid.values())

    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "size", "route", "ats", "id", "confidence",
                    "jobs", "israel_jobs", "on_family"])
        for r in kept:
            w.writerow([r["company"], "?", "common_crawl", "comeet",
                        "%s/%s" % (r["slug"], r["uid"]), "verified",
                        r["jobs"], r["israel_jobs"], r["on_family"]])

    # The rejects are the expensive half of what was learned - see
    # EXPANSION_STRATEGY.md 8.1 on persisting negative results.
    rejects_path = os.path.splitext(args.out)[0] + "_rejected.csv"
    with open(rejects_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["slug", "uid", "company", "jobs", "israel_jobs", "reason"])
        for r in results:
            if r["reject"]:
                w.writerow([r["slug"], r["uid"], r["company"], r["jobs"],
                            r["israel_jobs"], r["reject"]])

    reasons = {}
    for r in results:
        if r["reject"]:
            key = r["reject"].split("(")[0].strip()
            reasons[key] = reasons.get(key, 0) + 1

    print("\n[gate] %d of %d candidates passed all applicable gates"
          % (len(kept), len(results)), file=sys.stderr)
    print("[gate] on-family postings across them: %d (%d physically in Israel)"
          % (sum(r["on_family"] for r in kept),
             sum(r["physical"] for r in kept)), file=sys.stderr)
    for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("       rejected %4d  %s" % (n, reason), file=sys.stderr)
    for dup_slug, uid, kept_slug in aliases:
        print("       alias    %s/%s -> same board as %s, dropped"
              % (dup_slug, uid, kept_slug), file=sys.stderr)
    print("[gate] wrote %s and %s" % (args.out, rejects_path), file=sys.stderr)


if __name__ == "__main__":
    main()
