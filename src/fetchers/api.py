# -*- coding: utf-8 -*-
"""
Fetching jobs from known ATS platforms (Lever/Greenhouse/Comeet/
SmartRecruiters). This is the generic, profile-driven expression of the
same logic the career-site-profiler skill generates per-company in Step 6
- one function per platform, reading fields (endpoint, field mapping)
from the profile, instead of a separate function per company.

Adding a new company on an existing platform = a new
profiles/<slug>.json, zero code changes here. New code is only needed
when a new platform shows up.
"""

import requests

from models import Job
from relevance import is_relevant_location


def _get_by_path(obj: dict, dotted_path: str):
    """Reads a nested field by a dotted string like "categories.location"
    or "location.name". This is what lets a profile declare a field
    mapping without writing platform-specific code."""
    cur = obj
    for part in dotted_path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def fetch_lever(profile) -> list[Job]:
    """Lever: one call, a direct array, no pagination. api_platforms.md -
    verified."""
    api = profile.raw["api"]
    r = requests.get(api["endpoint"], timeout=20)
    r.raise_for_status()
    postings = r.json()

    fields = api["fields"]
    jobs = []
    for p in postings:
        location = _get_by_path(p, fields["location"]) or ""
        if not is_relevant_location(location):
            continue   # post_fetch - the correct approach for an api profile (SKILL Step 5)
        jobs.append(Job(
            id=str(_get_by_path(p, fields["id"]) or p.get("hostedUrl", "")),
            title=(_get_by_path(p, fields["title"]) or "").strip(),
            location=location.strip(),
            url=_get_by_path(p, fields["url"]) or "",
            company=profile.slug,
        ))
    return jobs


def fetch_greenhouse(profile) -> list[Job]:
    """Greenhouse: ?content=true is mandatory - without it "Multiple
    Locations" can't be resolved, which requires checking offices as well
    as location.name."""
    api = profile.raw["api"]
    r = requests.get(api["endpoint"], timeout=20)
    r.raise_for_status()
    data = r.json()

    fields = api["fields"]
    jobs = []
    for j in data.get("jobs", []):
        location = _get_by_path(j, fields["location"]) or ""
        offices = [o.get("name", "") for o in (j.get("offices") or [])]
        if not (is_relevant_location(location)
                or any(is_relevant_location(o) for o in offices)):
            continue
        display_location = location or ", ".join(offices)
        jobs.append(Job(
            id=str(_get_by_path(j, fields["id"])),
            title=(_get_by_path(j, fields["title"]) or "").strip(),
            location=display_location.strip(),
            url=_get_by_path(j, fields["url"]) or "",
            company=profile.slug,
        ))
    return jobs


def fetch_comeet(profile) -> list[Job]:
    """Comeet: the token was never verified against a live company - see
    api_platforms.md. location_query_param in the profile, if present, is
    passed as a param; otherwise {}."""
    api = profile.raw["api"]
    params = api.get("extra_params", {})
    r = requests.get(api["endpoint"], params=params, timeout=20)
    r.raise_for_status()
    positions = r.json()

    fields = api["fields"]
    jobs = []
    for p in positions:
        location = _get_by_path(p, fields["location"]) or ""
        if not is_relevant_location(location):
            continue
        jobs.append(Job(
            id=str(_get_by_path(p, fields["id"]) or p.get("id", "")),
            title=(_get_by_path(p, fields["title"]) or "").strip(),
            location=location.strip(),
            url=_get_by_path(p, fields["url"]) or "",
            company=profile.slug,
        ))
    return jobs


def fetch_smartrecruiters(profile) -> list[Job]:
    """SmartRecruiters: the only one of the four that requires pagination
    (offset/limit)."""
    api = profile.raw["api"]
    jobs, offset, limit = [], 0, 100
    max_pages = 50   # hard cap - protects against an inconsistent totalFound
    for _ in range(max_pages):
        r = requests.get(api["endpoint"], params={"offset": offset, "limit": limit},
                         timeout=20)
        r.raise_for_status()
        data = r.json()
        postings = data.get("content", [])
        if not postings:
            break
        for p in postings:
            loc = p.get("location") or {}
            location = ", ".join(x for x in [loc.get("city"), loc.get("country")] if x)
            if not is_relevant_location(location):
                continue
            job_id = str(p.get("id") or p.get("ref", ""))
            jobs.append(Job(
                id=job_id, title=p.get("name", "").strip(), location=location,
                url=_build_sr_url(api, job_id),
                company=profile.slug,
            ))
        offset += limit
        if offset >= data.get("totalFound", 0):
            break
    return jobs


def _build_sr_url(api: dict, job_id: str) -> str:
    """Builds a SmartRecruiters job link from the company slug embedded in
    the endpoint."""
    # example endpoint: https://api.smartrecruiters.com/v1/companies/<co>/postings
    parts = api["endpoint"].rstrip("/").split("/")
    company = parts[parts.index("companies") + 1] if "companies" in parts else ""
    return f"https://jobs.smartrecruiters.com/{company}/{job_id}"


_PLATFORM_DISPATCH = {
    "lever": fetch_lever,
    "greenhouse": fetch_greenhouse,
    "comeet": fetch_comeet,
    "smartrecruiters": fetch_smartrecruiters,
    # ashby/workable/recruitee/workday: planned but not implemented - per
    # api_platforms.md they're UNVERIFIED. Only added after a live
    # verification, never before.
}


def fetch(profile) -> list[Job]:
    """The entry point the main dispatcher calls for fetch_type == "api"."""
    platform = profile.raw["api"]["platform"]
    handler = _PLATFORM_DISPATCH.get(platform)
    if handler is None:
        raise NotImplementedError(
            f"{profile.slug}: api platform not implemented: {platform!r}. "
            "If this is Ashby/Workable/Recruitee/Workday - it needs a live "
            "verification against api_platforms.md first, then a handler.")
    return handler(profile)
