#!/usr/bin/env python3
"""
sweep_careers_fingerprint.py - ask each company where its board is, then score
it with the real fetcher.

    py -3 _onboarding/sweep_careers_fingerprint.py --out fingerprint_sweep.csv

*** Why this exists after the seed was declared exhausted ***

It was exhausted for the platforms that had FETCHERS on 2026-08-18. Three have
been added since - workday, smartrecruiters (platform profile), workable - and
the sweep's own deferred list named 11 Workday companies it had found and could
not import, "the biggest gap". Those companies never became a re-run, so they
are still outside the corpus along with everything else that resolved to a
platform this project could not read at the time.

Measured on a 200-company sample of the 1,127 unprofiled seed rows that carry a
careers URL: 15 fingerprint to a known ATS and **12 of those 15 are Workday**.
That is the backlog, and it costs no new code to collect.

*** Route 1 only, deliberately ***

discover_ats.discover tries the careers page first and then falls back to
guessing slugs across six platforms - roughly 40 requests per company, and the
route that put a third of its raw hits on the wrong company. This file runs
ONLY the careers-page fingerprint: one request, exact, and identity is free
because the company published the id itself on its own domain.

Companies that fingerprint to nothing are not guessed at. They are written to
the rejects file, which is the point of EXPANSION_STRATEGY.md 8.1 - the
negative result is the expensive half of what was learned, and persisting it is
what makes the next sweep incremental instead of a repeat.

*** Scoring goes through the real fetcher, not through a probe ***

The 2026-08-18 sweep predicted +606 on-family and delivered +391, because its
probe joined Greenhouse's location.name and offices[] into one string while the
fetcher treats offices[] as a fallback. Any probe that reimplements relevance
drifts from the thing it is predicting.

So a candidate here is turned into a real profile document, resolved against
its platform file, validated exactly as a committed profile would be, and
fetched with fetchers.fetch_jobs. The numbers in the output CSV are therefore
the numbers the bot will actually see - not an estimate of them.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from discover_ats import fingerprint_careers_page                    # noqa: E402
from import_companies import BUILDERS, slugify                       # noqa: E402
from profiles import load_profile, ProfileError                      # noqa: E402
from fetchers import fetch_jobs                                      # noqa: E402
from relevance import is_israel_location                             # noqa: E402
from roles import classify as classify_role                          # noqa: E402

SEED = os.path.join(HERE, "israeli_companies_seed.csv")
COMPANIES_DIR = os.path.join(HERE, "..", "profiles", "companies")

# A fingerprint to a platform with no builder is recorded as a DEFERRED
# candidate rather than dropped - that is how the Workday backlog became
# visible in the first place, and losing it again would repeat the mistake.
WORKERS = 8


def already_profiled():
    """Names and endpoints already in the corpus, both normalised.

    Endpoint matters as much as name: the same board reached under two company
    names is the gong.io defect, and it is invisible to a name check."""
    names, endpoints = set(), set()
    for path in Path(COMPANIES_DIR).glob("*.json"):
        d = json.loads(path.read_text(encoding="utf-8"))
        names.add(re.sub(r"[^a-z0-9]", "", (d.get("name") or "").lower()))
        ep = (d.get("api") or {}).get("endpoint")
        if ep:
            endpoints.add(ep.split("?")[0])
    return names, endpoints


def build_profile_document(company, platform, ident):
    """The record the importer would write, minus the health numbers we do not
    have yet. Returns (document, error)."""
    builder = BUILDERS.get(platform)
    if builder is None:
        return None, "no builder for %r" % platform
    try:
        built, err = builder({"id": ident, "company": company})
    except Exception as e:
        return None, "builder failed (%s)" % type(e).__name__
    if err:
        return None, err

    return {
        "slug": slugify(company),
        "name": company,
        "platform": "lever" if platform == "lever_eu" else platform,
        "careers_url": built["careers_url"],
        "api": {"endpoint": built["endpoint"]},
        # Neutral: this document is fetched once, never committed. The real
        # health numbers are set by the importer from the count found here.
        "health": {"expected_min_jobs": 0, "zero_is_plausible": True},
    }, None


def score_live(document):
    """(jobs, israel, on_family, physical) from the REAL fetcher, or an error.

    The document is written to a temp file and loaded through load_profile so
    it is resolved against its platform file and validated exactly as a
    committed profile is - a candidate that would not load is a candidate that
    must not be imported.

    The file is named {slug}.json inside a throwaway directory, because
    load_profile checks that the two agree - the same rule that stops a
    committed record being silently reachable under the wrong name."""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ("%s.json" % document["slug"])
            path.write_text(json.dumps(document, ensure_ascii=False),
                            encoding="utf-8")
            profile = load_profile(path)
            jobs = fetch_jobs(profile)
    except ProfileError as e:
        return None, "would not validate: %s" % str(e)[:120]
    except Exception as e:
        return None, "fetch failed (%s: %s)" % (type(e).__name__, str(e)[:80])

    israel = len(jobs)          # fetch_jobs already applied the Israel filter
    on_family = physical = 0
    for j in jobs:
        if classify_role(j.title or "")[0] != "blocked":
            on_family += 1
            if is_israel_location(j.location or ""):
                physical += 1
    return (len(jobs), israel, on_family, physical), None


def probe(row):
    name = row["name"]
    careers = (row.get("careers") or "").strip()
    out = {"company": name, "size": row.get("size", ""), "careers": careers,
           "platform": "", "ident": "", "jobs": 0, "israel_jobs": 0,
           "on_family": 0, "physical": 0, "reject": None}

    try:
        hits = fingerprint_careers_page(careers, name)
    except Exception as e:
        out["reject"] = "careers page unreachable (%s)" % type(e).__name__
        return out
    if not hits:
        out["reject"] = "careers page fingerprints to no known ATS"
        return out

    platform, ident = hits[0]
    out["platform"], out["ident"] = platform, ident
    if platform not in BUILDERS:
        out["reject"] = "no fetcher for platform %r - deferred" % platform
        return out

    document, err = build_profile_document(name, platform, ident)
    if err:
        out["reject"] = err
        return out

    scored, err = score_live(document)
    if err:
        out["reject"] = err
        return out

    out["jobs"], out["israel_jobs"], out["on_family"], out["physical"] = scored
    if out["on_family"] <= 0:
        out["reject"] = ("no Israel-relevant postings in a target job family "
                         "(%d Israel-relevant)" % out["israel_jobs"])
    elif out["physical"] <= 0:
        # Same company-level test as the Comeet and Workable imports: a bare
        # "Remote" at a foreign employer means remote within that country.
        out["reject"] = ("no PHYSICAL Israeli posting - all %d on-family roles "
                         "are bare-remote at a foreign employer" % out["on_family"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=SEED)
    ap.add_argument("--out", default=os.path.join(HERE, "fingerprint_sweep.csv"))
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    rows = list(csv.DictReader(io.open(args.input, encoding="utf-8")))
    names, endpoints = already_profiled()
    pool = [r for r in rows
            if (r.get("careers") or "").strip()
            and re.sub(r"[^a-z0-9]", "", r["name"].lower()) not in names]
    if args.limit:
        pool = pool[:args.limit]

    print("[sweep] %d unprofiled companies carry a careers URL" % len(pool),
          file=sys.stderr)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(probe, pool))

    kept = [r for r in results if not r["reject"]]

    # Endpoint collision against the EXISTING corpus and within this batch -
    # one board reached under two names is the gong.io defect, and a name check
    # cannot see it.
    seen_eps, deduped, collisions = set(endpoints), [], []
    for r in sorted(kept, key=lambda r: -r["on_family"]):
        doc, _ = build_profile_document(r["company"], r["platform"], r["ident"])
        ep = doc["api"]["endpoint"].split("?")[0] if doc else ""
        if ep and ep in seen_eps:
            collisions.append((r["company"], ep))
            continue
        seen_eps.add(ep)
        deduped.append(r)

    with io.open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "size", "route", "ats", "id", "confidence",
                    "jobs", "israel_jobs", "on_family"])
        for r in deduped:
            w.writerow([r["company"], r["size"] or "?", "careers_page",
                        r["platform"], r["ident"], "verified",
                        r["jobs"], r["israel_jobs"], r["on_family"]])

    rejects = os.path.splitext(args.out)[0] + "_rejected.csv"
    with io.open(rejects, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "size", "careers", "platform", "id",
                    "israel_jobs", "reason"])
        for r in results:
            if r["reject"]:
                w.writerow([r["company"], r["size"], r["careers"],
                            r["platform"], r["ident"], r["israel_jobs"],
                            r["reject"]])

    import collections
    reasons = collections.Counter(
        r["reject"].split("(")[0].strip() for r in results if r["reject"])
    plats = collections.Counter(r["platform"] for r in deduped)

    print("\n[sweep] %d of %d passed every gate" % (len(deduped), len(results)),
          file=sys.stderr)
    print("[sweep] on-family postings: %d (%d physically in Israel)"
          % (sum(r["on_family"] for r in deduped),
             sum(r["physical"] for r in deduped)), file=sys.stderr)
    print("[sweep] by platform: %s" % plats.most_common(), file=sys.stderr)
    for reason, n in reasons.most_common(12):
        print("        rejected %4d  %s" % (n, reason), file=sys.stderr)
    for company, ep in collisions:
        print("        collision %s -> board already monitored (%s)"
              % (company, ep), file=sys.stderr)
    print("[sweep] wrote %s and %s" % (args.out, rejects), file=sys.stderr)


if __name__ == "__main__":
    main()
