#!/usr/bin/env python3
"""
import_companies.py - turns the verified shortlist into thin company records.

    py -3 _onboarding/import_companies.py            # write profiles/companies/
    py -3 _onboarding/import_companies.py --dry-run  # report only, write nothing

One-off, but committed and re-runnable: it is the provenance of 140 generated
files, and a generated file whose generator is gone is a hand-written file that
nobody will admit to.

*** Idempotent, and specifically in the direction that preserves work ***

A company that already has a record is SKIPPED, never rewritten. That is what
makes a second run a no-op, and it is also what protects hand-tuning: mobileye
and wiz are both in the shortlist and both already carry health numbers chosen
by a human (mobileye's floor of 20; wiz's zero_is_plausible=true, which is the
difference between silence and a false maintenance alert every three hours).
Regenerating those from a live count would quietly undo both. To rebuild a
record from scratch, delete it and re-run.

Input is verify_raw.csv - Phase 1's verdict - not bot_shortlist.csv's
`api_endpoint` column, which is wrong for all 111 Comeet rows. Every row Phase 1
marked dead is skipped.

*** Second input format, added 2026-08-18 ***

`discover_ats.py discover` now produces a differently-shaped CSV, and it is the
source of every company added after the original 145. Pass it with --input; the
format is detected from the header, not from a flag, so the two cannot be
confused. The columns differ in three ways that matter:

  status      -> confidence. 'verified' means the board published a company name
                 that matches the seed name. 'unverifiable' means the platform
                 publishes no name at all, and is NOT imported here: a wrong
                 board is silent forever, so those rows want a human first.
  tier        -> size, which is only used in the generated note.
  host        -> absent. Lever's EU accounts surface as ats='lever_eu' instead,
                 which build_lever now reads.

One extra filter applies to sweep rows: on_family must be > 0. A board with no
Israel-relevant postings in a target job family is either structurally dead or
structurally off-target, and importing it buys a health-gate entry that never
produces an alert. Measured 2026-08-18: that rule drops 225 of 342 found boards.
"""

import argparse
import csv
import glob
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from discover_ats import (resolve_comeet_token,            # noqa: E402
                          resolve_workday_israel_facet)

PROFILES_DIR = os.path.join(HERE, "..", "profiles")
COMPANIES_DIR = os.path.join(PROFILES_DIR, "companies")
PLATFORMS_DIR = os.path.join(PROFILES_DIR, "_platforms")
VERIFY_CSV = os.path.join(HERE, "verify_raw.csv")

VERIFIED_ON = "2026-08-13"


# --------------------------------------------------------------- explicit drops
#
# Rows that are live but deliberately NOT imported. Keyed by (company, ats) so a
# decision applies to one board and not to a whole company.
#
# This table exists so that a dropped row is a recorded decision with a reason
# attached, rather than something a later reader has to reverse-engineer from an
# absence. Nothing is dropped silently.

DELIBERATE_DROPS = {
    ("BioCatch", "lever"): (
        "BioCatch runs two live boards; only the Comeet one is imported. The "
        "Lever board is abandoned, not a second division: its newest posting "
        "was created 2026-02-23 while the Comeet board was updated the day of "
        "this import, and only 3 titles overlap. Importing both would merge ~3 "
        "long-dead postings into the company permanently - nothing ever "
        "retires them from state, so they would sit in /jobs forever. See "
        "verify_report.md."),

    ("Paradox", "workday"): (
        "The careers-page fingerprint resolved Paradox to Workday tenant "
        "'workday/wd5/Workday' - which is WORKDAY INC'S OWN careers site, 341 "
        "postings of Workday's own hiring, not Paradox's. Paradox's page links "
        "to its ATS vendor rather than embedding its own board, and the "
        "fingerprint route cannot tell the two apart: it trusts an id because "
        "the company published it, and here the company published someone "
        "else's. Checked live 2026-08-19 - the top postings are 'Regional Sales "
        "Director, Large Enterprise' in USA/IL/Chicago, where IL is Illinois. "
        "This is the fingerprint route's version of the slug-collision problem "
        "and the reason a vendor-named tenant deserves a second look."),

    ("Gong.io", "greenhouse"): (
        "*** This one shipped as a live defect and is the reason the "
        "endpoint-collision check below exists. *** The seed list carries the "
        "same company under two names - 'Gong' in the original 2026-08-13 "
        "shortlist and 'Gong.io' in israeli_companies_seed.csv - and both "
        "resolve to Greenhouse board 'gongio'. The 2026-08-18 import had no "
        "collision check, so it wrote gong_io.json alongside the existing "
        "gong.json and both went live: one board, two profiles, two state "
        "files, every Israeli Gong posting diffed and alerted TWICE. Nothing "
        "caught it, because both companies look perfectly healthy - which is "
        "exactly the silent-duplication failure the check now prevents. "
        "gong.json is kept as the original; gong_io.json and its state file "
        "were removed on 2026-08-19."),

    ("Flow Security", "workday"): (
        "Resolves to 'crowdstrike/wd5/crowdstrikecareers', the SAME board as "
        "Crowdstrike Israel R D, because CrowdStrike acquired Flow Security and "
        "its careers page now points at the acquirer's board. Importing both "
        "would monitor one board under two names: every Israeli CrowdStrike "
        "posting would be diffed, stored and alerted twice, and no health check "
        "would notice because both companies look perfectly healthy. "
        "Crowdstrike Israel R D is kept - it is the board's actual owner and "
        "the two carry identical counts. Detected by the endpoint-collision "
        "check below, not by hand."),
}


# --------------------------------------------------------------- slugs

def slugify(name):
    """Company name -> profile slug.

    Lowercase [a-z0-9_] per references/profile_schema.md - note UNDERSCORES,
    not hyphens, which is why this isn't the usual web slugify. 'D-Fend
    Solutions' -> 'd_fend_solutions'.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return re.sub(r"_+", "_", slug)


# --------------------------------------------------------------- health

# Below this many Israel-relevant postings, no absolute floor is set at all.
#
# On a small board the floor buys nothing and costs false alarms: a company with
# 5 postings dropping to 1 is ordinary churn, while dropping to 0 is already
# caught by the total-zero check, which needs no floor. state.py's relative
# collapse gate also declines to act below a baseline of 10, for the same
# reason - so anything under that is left to the zero check alone, deliberately.
FLOOR_MIN_COUNT = 12

# The floor as a fraction of what was actually observed live. Deliberately far
# below the observed count: this half of the health gate is for SLOW decay over
# months, while sudden breakage is caught by the relative gate, which needs no
# number. Calibrated against the hand-chosen precedent - mobileye sits at 20
# against ~120 observed, about 0.17.
FLOOR_FRACTION = 0.25


def health_for(israel_jobs):
    """The health block for a company, from its live verified count.

    The zero case is the one that matters. 14 live companies currently have no
    Israel-relevant postings at all - a real and legitimate state, and the same
    call already made by hand for wiz. Importing them with the platform default
    (zero_is_plausible=false) would make every one of them fire a maintenance
    alert on its very first run, for a board that is working perfectly.
    """
    if israel_jobs == 0:
        return {
            "expected_min_jobs": 0,
            "zero_is_plausible": True,
            "_note": (
                "Live-verified on %s: the board is reachable and non-empty, but "
                "it has NO Israel-relevant postings right now. That is a real "
                "state, not a fault, so zero_is_plausible is true - otherwise "
                "this company would fire a maintenance alert on every run. "
                "Revisit if it grows an Israeli presence: at that point "
                "zero_is_plausible should go back to false so a genuine "
                "breakage is caught." % VERIFIED_ON),
        }

    floor = int(israel_jobs * FLOOR_FRACTION) if israel_jobs >= FLOOR_MIN_COUNT else 0
    return {
        "expected_min_jobs": floor,
        "zero_is_plausible": False,
        "_note": (
            "Live-verified on %s: %d Israel-relevant postings. "
            "expected_min_jobs=%d is a deliberately conservative floor (about "
            "%.0f%% of what was observed), because this half of the health gate "
            "exists for slow decay over months - sudden breakage is caught by "
            "the relative-collapse check in state.py, which needs no number "
            "here. %s"
            % (VERIFIED_ON, israel_jobs, floor, FLOOR_FRACTION * 100,
               "No absolute floor is set: below %d postings a floor causes more "
               "false alarms than it catches, and a drop to zero is already "
               "covered by the total-zero check." % FLOOR_MIN_COUNT
               if floor == 0 else
               "zero_is_plausible=false, so a drop to zero is treated as a "
               "fault rather than silently accepted.")),
    }


# --------------------------------------------------------------- per-platform

def build_comeet(row):
    """Resolve the per-company token and build the endpoint.

    The token is baked in HERE, once, on purpose: resolving it costs a ~750 KB
    board-page fetch against a few KB for the API call itself, so doing it at
    runtime would add ~78 MB to every scheduled run for a value that
    effectively never changes. If a token is ever rotated the fetch returns
    HTTP 400, which surfaces as an ordinary fetch failure and reaches a
    maintenance alert after two consecutive runs - a loud failure, not a silent
    zero.
    """
    slug, uid = row["id"].split("/", 1)
    token = resolve_comeet_token(slug, uid)
    if token is None:
        return None, "comeet token could not be resolved"
    return {
        "careers_url": "https://www.comeet.co/jobs/%s/%s" % (slug, uid),
        "endpoint": ("https://www.comeet.co/careers-api/2.0/company/%s/"
                     "positions?token=%s" % (uid, token)),
        "resolved_from": (
            "bot_shortlist.csv id '%s' (Comeet slug/uid). The API token is NOT "
            "the uid - it was resolved from the company's Comeet-hosted board "
            "page and confirmed with a live API call at import time."
            % row["id"]),
    }, None


def build_greenhouse(row):
    token = row["id"]
    return {
        "careers_url": "https://job-boards.greenhouse.io/%s" % token,
        "endpoint": ("https://boards-api.greenhouse.io/v1/boards/%s/jobs"
                     "?content=true" % token),
        "resolved_from": (
            "bot_shortlist.csv id '%s' (Greenhouse board token), confirmed live "
            "at import time. ?content=true is required - without it "
            "'Multiple Locations' postings cannot be resolved to Israel."
            % token),
    }, None


def build_lever(row):
    """The API host is part of the endpoint because nothing in the slug reveals
    it. Phase 1 established which host answers, per company, and recorded it in
    verify_raw.csv's `host` column; sweep rows carry the same fact as
    ats='lever_eu' instead, because discover probes the two hosts as two
    separate platforms."""
    slug = row["id"]
    eu = row.get("host") == "lever_eu" or row.get("ats") == "lever_eu"
    api_host = "api.eu.lever.co" if eu else "api.lever.co"
    site_host = "jobs.eu.lever.co" if eu else "jobs.lever.co"
    return {
        "careers_url": "https://%s/%s" % (site_host, slug),
        "endpoint": "https://%s/v0/postings/%s?mode=json" % (api_host, slug),
        "resolved_from": (
            "bot_shortlist.csv id '%s' (Lever slug). The %s host was determined "
            "live at import time by calling the US host and falling back to EU "
            "on a 404 - nothing in the slug reveals which one an account is on."
            % (slug, "EU" if eu else "US")),
    }, None


def build_smartrecruiters(row):
    """The detail URL carries the company slug, so url_template is per company.

    `{id}` is substituted by detail._detail_url with the posting id, which the
    listing publishes directly.
    """
    slug = row["id"]
    return {
        "careers_url": "https://careers.smartrecruiters.com/%s" % slug,
        "endpoint": ("https://api.smartrecruiters.com/v1/companies/%s/postings"
                     % slug),
        "url_template": ("https://api.smartrecruiters.com/v1/companies/%s"
                         "/postings/{id}" % slug),
        "resolved_from": (
            "SmartRecruiters company slug '%s', confirmed live at import time. "
            "Identity was proven from the board's own `company.name` field on "
            "its postings - this endpoint answers 200 with an empty list for "
            "any slug, so a reachable board is NOT on its own evidence that "
            "the company exists." % slug),
    }, None


def build_workday(row):
    """Resolve the tenant's Israel facet id and build the POST endpoint.

    The id arrives as 'tenant/wdN/site' - all three are needed, and none of
    them is derivable from a company name, which is why Workday is found only
    by fingerprinting a careers page.

    The facet is baked in HERE for the same reason the Comeet token is: it is a
    Workday-internal GUID that never changes, and resolving it per run would
    cost an extra request per company per run for a constant. Unlike the Comeet
    token, a missing facet is not fatal - the fetcher falls back to reading the
    board unfiltered and deciding relevance post-fetch - so a failure to
    resolve is recorded in the note rather than dropping the company.
    """
    parts = row["id"].split("/")
    if len(parts) != 3:
        return None, "workday id must be 'tenant/wdN/site', got %r" % row["id"]
    tenant, wd, site = parts
    host = "https://%s.%s.myworkdayjobs.com" % (tenant, wd)
    facet = resolve_workday_israel_facet(tenant, wd, site)
    return {
        "careers_url": "%s/en-US/%s" % (host, site),
        "endpoint": "%s/wday/cxs/%s/%s/jobs" % (host, tenant, site),
        "israel_facet": facet,
        # The description lives at the CXS path, which differs from the public
        # posting URL by exactly one segment - see detail._detail_url.
        "url_rewrite": ["/en-US/", "/wday/cxs/%s/" % tenant],
        "resolved_from": (
            "Found by fingerprinting the company's own careers page during the "
            "2026-08-18 sweep: '%s' (Workday tenant/host/site). The Israel "
            "locationCountry facet %s at import time."
            % (row["id"],
               "resolved to %s and was confirmed by a live filtered call" % facet
               if facet else
               "could NOT be resolved - the board will be read unfiltered and "
               "filtered post-fetch, which costs more requests but is correct")),
    }, None


def build_ashby(row):
    slug = row["id"]
    return {
        "careers_url": "https://jobs.ashbyhq.com/%s" % slug,
        "endpoint": "https://api.ashbyhq.com/posting-api/job-board/%s" % slug,
        "resolved_from": (
            "bot_shortlist.csv id '%s' (Ashby job-board slug), confirmed live "
            "at import time." % slug),
    }, None


BUILDERS = {
    "comeet": build_comeet,
    "greenhouse": build_greenhouse,
    "lever": build_lever,
    "lever_eu": build_lever,
    "ashby": build_ashby,
    "workday": build_workday,
    "smartrecruiters": build_smartrecruiters,
}


# --------------------------------------------------------------- input formats
#
# Two producers write rows here, and they are told apart by their header rather
# than by a flag, so the wrong one cannot be passed by mistake.
#
#   verify_raw.csv                - Phase 1, columns include status/tier/host
#   discover_ats.py discover      - the sweep, columns include confidence/size
#
# normalize_row() converts the second into the first's shape, so build_record
# and everything below it stay unaware there are two.

SWEEP_COLUMNS = {"confidence", "on_family", "route"}


def is_sweep_format(fieldnames):
    return SWEEP_COLUMNS.issubset(set(fieldnames or ()))


def normalize_row(row, sweep):
    """Give a row the shape build_record expects, and decide if it is importable.

    Returns (row, reason_to_skip). A reason is a string, not an exception: a row
    that is deliberately not imported should be reported in the summary, the way
    dead and dropped rows already are.
    """
    if not sweep:
        return row, (None if row.get("status") == "ok" else "not ok in phase 1")

    row = dict(row)
    row["tier"] = row.get("size", "?")
    row["status"] = "ok"

    if row.get("confidence") != "verified":
        # The board answered, but nothing proved it belongs to this company.
        # Importing on that basis is the one mistake nothing downstream catches.
        return row, "confidence=%s - needs a human check" % row.get("confidence")
    if int(row.get("on_family") or 0) <= 0:
        return row, "no Israel-relevant postings in a target job family"
    return row, None


# --------------------------------------------------------------- the import

def build_record(row):
    """One company record, or (None, reason)."""
    builder = BUILDERS.get(row["ats"])
    if builder is None:
        return None, "no builder for platform %r" % row["ats"]
    built, error = builder(row)
    if error:
        return None, error

    slug = slugify(row["company"])

    # `platform` names a FILE under profiles/_platforms/, so it must be the
    # platform's real name. discover probes Lever's two API hosts as two
    # platforms ('lever' and 'lever_eu'), but there is only lever.json - the
    # EU host is already carried by the endpoint build_lever produced above.
    platform = "lever" if row["ats"] == "lever_eu" else row["ats"]

    if row.get("route"):        # sweep row
        provenance = (
            "Generated by _onboarding/import_companies.py from the 2026-08-18 "
            "discovery sweep (size bucket %s; found by %s; identity %s). "
            % (row["tier"], row["route"], row["confidence"]))
    else:
        provenance = (
            "Generated by _onboarding/import_companies.py from the Phase 1 "
            "verification (tier %s in bot_shortlist.csv). " % row["tier"])

    record = {
        "slug": slug,
        "name": row["company"],
        "platform": platform,
        "careers_url": built["careers_url"],
        "resolved_from": built["resolved_from"],
        "api": {"endpoint": built["endpoint"]},
        "health": health_for(int(row["israel_jobs"] or 0)),
        # Per-company overrides a builder may add on top of the platform file.
        # Kept out of the literal above so the common shape stays readable.
        "verified_on": VERIFIED_ON,
        "notes": (
            provenance +
            "Everything not stated here - fetch_type, the field map, "
            "pagination, the Israel filter and detail_fetch - is inherited from "
            "profiles/_platforms/%s.json. Live at import: %s postings on the "
            "board, %s Israel-relevant%s."
            % (platform, row["jobs"], row["israel_jobs"],
               ", %s of them in a target job family" % row["on_family"]
               if row.get("on_family") else "")),
    }

    if built.get("israel_facet") is not None:
        record["api"]["israel_facet"] = built["israel_facet"]
    if built.get("url_rewrite"):
        record["detail_fetch"] = {"url_rewrite": built["url_rewrite"]}
    if built.get("url_template"):
        record.setdefault("detail_fetch", {})["url_template"] = built["url_template"]

    return record, None


def main():
    global VERIFIED_ON
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen; write nothing")
    parser.add_argument("--input", default=VERIFY_CSV,
                        help="verify_raw.csv (default) or a discover sweep CSV; "
                             "the format is detected from the header")
    parser.add_argument("--verified-on", default=None,
                        help="date recorded in each record's verified_on and "
                             "health note; defaults to %s" % VERIFIED_ON)
    args = parser.parse_args()
    if args.verified_on:
        VERIFIED_ON = args.verified_on

    reader = csv.DictReader(open(args.input, encoding="utf-8"))
    sweep = is_sweep_format(reader.fieldnames)
    rows = list(reader)
    print("input: %s (%s format)"
          % (os.path.basename(args.input), "sweep" if sweep else "phase 1"))

    live, dead, dropped = [], [], []
    for row in rows:
        row, skip = normalize_row(row, sweep)
        if skip:
            dead.append((row, skip))
        elif (row["company"], row["ats"]) in DELIBERATE_DROPS:
            dropped.append(row)
        else:
            live.append(row)

    # Comeet tokens are one ~750 KB page fetch each, so they are resolved
    # concurrently. Everything else here is local.
    comeet = [r for r in live if r["ats"] == "comeet"]
    if comeet:
        print("resolving %d comeet tokens..." % len(comeet), file=sys.stderr)
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda r: resolve_comeet_token(*r["id"].split("/", 1)),
                        comeet))

    records, failures = [], []
    for row in live:
        record, error = build_record(row)
        if error:
            failures.append((row["company"], error))
        else:
            records.append(record)

    # *** Collision handling ***
    #
    # Two different companies whose names slugify to the same string would
    # otherwise overwrite each other's file AND share one state/seen/<slug>.json,
    # silently losing alerts for both. Nothing is written if this fires - a
    # partial import with one company missing is harder to notice than none.
    by_slug = {}
    for record in records:
        by_slug.setdefault(record["slug"], []).append(record)
    collisions = {s: rs for s, rs in by_slug.items() if len(rs) > 1}

    # *** Endpoint collisions - two companies, one board ***
    #
    # Different from a slug collision, and not caught by it: the names and slugs
    # differ, so every file is written and every profile loads, but two records
    # point at the SAME board. Both would then diff, store and alert the same
    # postings, and both would look healthy while doing it.
    #
    # This is not hypothetical. The 2026-08-18 sweep produced exactly one:
    # CrowdStrike acquired Flow Security, so Flow Security's careers page now
    # fingerprints to CrowdStrike's Workday board. Acquisitions are the general
    # case and there will be more of them, so this is a check rather than a
    # one-off drop - the drop above records the decision, this catches the next.
    #
    # It also covers collisions against companies ALREADY imported, which a
    # within-batch check alone would miss.
    endpoints = {}
    for path in (glob.glob(os.path.join(COMPANIES_DIR, "*.json"))
                 if os.path.isdir(COMPANIES_DIR) else []):
        try:
            doc = json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        endpoint = (doc.get("api") or {}).get("endpoint")
        if endpoint:
            endpoints.setdefault(endpoint, set()).add(doc.get("name", path))
    for record in records:
        endpoints.setdefault(record["api"]["endpoint"], set()).add(record["name"])
    # Keyed by NAME, not by occurrence: re-running the import re-derives records
    # for companies already on disk, and a company matching itself is not a
    # collision. Only two DIFFERENT names on one board are.
    endpoint_collisions = {e: sorted(n) for e, n in endpoints.items()
                           if len(n) > 1}

    existing = set()
    if os.path.isdir(COMPANIES_DIR):
        existing = {f[:-5] for f in os.listdir(COMPANIES_DIR)
                    if f.endswith(".json")}
    standalone = {f[:-5] for f in os.listdir(PROFILES_DIR)
                  if f.endswith(".json")}

    to_write = [r for r in records
                if r["slug"] not in existing and r["slug"] not in standalone]
    skipped = [r for r in records if r["slug"] in existing]
    shadowed = [r for r in records if r["slug"] in standalone]

    print()
    print("rows in input               %d" % len(rows))
    print("  skipped                   %d" % len(dead))
    by_reason = {}
    for _row, reason in dead:
        by_reason[reason] = by_reason.get(reason, 0) + 1
    for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print("      %-56s %d" % (reason, n))
    print("  deliberately dropped      %d" % len(dropped))
    for row in dropped:
        print("      %s (%s)" % (row["company"], row["ats"]))
    print("  live, importable          %d" % len(live))
    print("    already present, kept   %d   %s"
          % (len(skipped), sorted(r["slug"] for r in skipped)))
    if shadowed:
        print("    shadowed by standalone  %d   %s"
              % (len(shadowed), sorted(r["slug"] for r in shadowed)))
    print("    to write                %d" % len(to_write))
    if failures:
        print("  FAILED to build           %d" % len(failures))
        for company, error in failures:
            print("      %s: %s" % (company, error))

    if collisions:
        print()
        print("SLUG COLLISIONS - nothing written:")
        for slug, rs in sorted(collisions.items()):
            print("  %s <- %s" % (slug, [r["name"] for r in rs]))
        return 1

    if endpoint_collisions:
        print()
        print("ENDPOINT COLLISIONS - two companies share one board, nothing "
              "written. Resolve by adding the loser to DELIBERATE_DROPS:")
        for endpoint, names in sorted(endpoint_collisions.items()):
            print("  %s\n      <- %s" % (endpoint, names))
        return 1

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    os.makedirs(COMPANIES_DIR, exist_ok=True)
    for record in to_write:
        path = os.path.join(COMPANIES_DIR, record["slug"] + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
            f.write("\n")
    print("\nwrote %d records to profiles/companies/" % len(to_write))
    return 0


if __name__ == "__main__":
    sys.exit(main())
