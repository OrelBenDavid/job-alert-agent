#!/usr/bin/env python3
"""Measure: how many unprofiled Israeli companies expose a machine-readable
board WITHOUT being on a known ATS.

Answers three questions per company, in order of how cheap the answer is:
  1. Does its careers page fingerprint to a known ATS? (the existing route
     already covers it - not a new company)
  2. Does anything on that page carry schema.org/JobPosting JSON-LD?
  3. If not, does a job DETAIL page linked from it? Google requires the markup
     on the detail page, so a listing-only check under-reports badly.
"""
import csv, io, json, os, re, sys, random, collections
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(REPO, "_onboarding"))
sys.path.insert(0, os.path.join(REPO, "src"))

from discover_ats import get_text, fingerprint_careers_page  # noqa: E402

JOBPOSTING = re.compile(r'"@type"\s*:\s*"?JobPosting', re.I)
LDJSON = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I)
JOBLINK = re.compile(
    r'href=["\']([^"\']*(?:job|career|position|vacanc|opening)[^"\']*)["\']', re.I)


def has_jobposting(html):
    if not html:
        return False
    if JOBPOSTING.search(html):
        return True
    for blob in LDJSON.findall(html):
        if JOBPOSTING.search(blob):
            return True
    return False


def absolutize(base, href):
    if href.startswith("http"):
        return href
    m = re.match(r"(https?://[^/]+)", base or "")
    if not m:
        return None
    if href.startswith("/"):
        return m.group(1) + href
    return base.rstrip("/") + "/" + href


def probe(row):
    name = row["name"]
    careers = (row.get("careers") or "").strip()
    out = {"name": name, "size": row.get("size", ""), "careers": careers,
           "ats": "", "jsonld": False, "where": "", "err": ""}
    if not careers:
        out["err"] = "no careers url"
        return out

    try:
        hits = fingerprint_careers_page(careers, name)
    except Exception:
        hits = []
    if hits:
        out["ats"] = hits[0][0]
        return out                       # already covered by the ATS route

    try:
        html = get_text(careers)
    except Exception as e:
        out["err"] = type(e).__name__
        return out
    if not html:
        out["err"] = "careers page unreachable"
        return out

    if has_jobposting(html):
        out["jsonld"] = True
        out["where"] = "listing"
        return out

    # Try up to 3 plausible job detail pages linked from the listing.
    seen, tried = set(), 0
    for href in JOBLINK.findall(html):
        url = absolutize(careers, href)
        if not url or url in seen or url.rstrip("/") == careers.rstrip("/"):
            continue
        seen.add(url)
        tried += 1
        if tried > 3:
            break
        try:
            sub = get_text(url)
        except Exception:
            continue
        if has_jobposting(sub):
            out["jsonld"] = True
            out["where"] = "detail"
            return out
    return out


def main():
    path = os.path.join(REPO, "_onboarding", "israeli_companies_seed.csv")
    rows = list(csv.DictReader(io.open(path, encoding="utf-8")))

    have = set()
    import glob
    for f in glob.glob(os.path.join(REPO, "profiles", "companies", "*.json")):
        d = json.load(io.open(f, encoding="utf-8"))
        have.add(re.sub(r"[^a-z0-9]", "", (d.get("name") or "").lower()))

    pool = [r for r in rows
            if (r.get("careers") or "").strip()
            and re.sub(r"[^a-z0-9]", "", r["name"].lower()) not in have]

    # Weighted to where yield lives, per the measured size curve.
    random.seed(11)
    by_size = collections.defaultdict(list)
    for r in pool:
        by_size[r.get("size", "")].append(r)
    sample = []
    for size, n in (("xl", 40), ("l", 60), ("m", 50), ("s", 30), ("xs", 20)):
        bucket = by_size.get(size, [])
        sample += random.sample(bucket, min(n, len(bucket)))
    print("pool: %d | sample: %d" % (len(pool), len(sample)), file=sys.stderr)

    with ThreadPoolExecutor(max_workers=10) as ex:
        res = list(ex.map(probe, sample))

    ats = [r for r in res if r["ats"]]
    err = [r for r in res if r["err"]]
    ld = [r for r in res if r["jsonld"]]
    nothing = [r for r in res if not r["ats"] and not r["err"] and not r["jsonld"]]

    print("\n=== RESULT over %d sampled companies ===" % len(res))
    print("  already on a known ATS (existing route)  : %3d" % len(ats))
    print("  careers page unreachable / no answer     : %3d" % len(err))
    print("  *** self-hosted WITH JobPosting JSON-LD  : %3d ***" % len(ld))
    print("      of which found on the listing page   : %3d"
          % sum(1 for r in ld if r["where"] == "listing"))
    print("      of which found on a detail page      : %3d"
          % sum(1 for r in ld if r["where"] == "detail"))
    print("  self-hosted, no structured data found    : %3d" % len(nothing))

    reachable = len(res) - len(err)
    if reachable:
        print("\n  JSON-LD rate among REACHABLE non-ATS companies: %.0f%%"
              % (100.0 * len(ld) / max(1, len(ld) + len(nothing))))
    print("\n  ATS breakdown:", collections.Counter(r["ats"] for r in ats).most_common())
    print("\n  sample of JSON-LD hits:")
    for r in ld[:15]:
        print("     %-34s %-4s %-7s %s" % (r["name"][:34], r["size"], r["where"], r["careers"][:60]))

    with io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "jsonld_measure.csv"), "w",
                 encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "size", "careers", "ats", "jsonld", "where", "err"])
        for r in res:
            w.writerow([r["name"], r["size"], r["careers"], r["ats"],
                        r["jsonld"], r["where"], r["err"]])


if __name__ == "__main__":
    main()
