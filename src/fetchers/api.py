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

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import requests

from models import Job
from relevance import is_relevant_location
from detail import (DEFAULT_SECTION_CONTENT_FIELD, DEFAULT_SECTION_HEADING_FIELD,
                    normalize_inline_value)


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


def _inline_description(profile, item: dict) -> str | None:
    """The cheapest rung of the cheap-to-expensive ladder for descriptions:
    if the listing response already contains the description text, take it
    here and no per-posting request is ever built.

    Both live-verified platforms turn out to reach their description this
    way, for different reasons:
      - Greenhouse returns the full description under `content` when the
        endpoint carries ?content=true, while its posting PAGE serves only
        the application form - so inline is the ONLY path there.
      - Lever returns it under `lists`, a structured array rather than a
        string. Its posting page does serve the description, but fetching it
        would cost one request per posting for data the listing already sent
        - which the project's cheap-to-expensive rule rules out.

    Returns None for any profile that isn't declared inline - including
    every v2 profile, which have no detail_fetch at all."""
    cfg = profile.detail_fetch
    if not cfg or cfg.get("method") != "inline":
        return None
    raw = _get_by_path(item, cfg["inline_field"])
    return normalize_inline_value(
        raw,
        bool(cfg.get("content_is_html", True)),
        cfg.get("inline_section_heading", DEFAULT_SECTION_HEADING_FIELD),
        cfg.get("inline_section_content", DEFAULT_SECTION_CONTENT_FIELD))


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
            description=_inline_description(profile, p),
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
            description=_inline_description(profile, j),
        ))
    return jobs


def _secondary_locations(job: dict, field: str | None) -> list[str]:
    """Ashby's extra locations for one posting, as plain strings.

    *** Read the caveat before changing this ***

    Both Ashby boards in the shortlist returned `secondaryLocations: []` on
    every posting on 2026-08-13, so the element shape could NOT be verified
    against a real response - only the field's existence and its emptiness
    were. Per the project convention, that is written down rather than
    asserted: the documented shape is `[{"location": "..."}]`, and this
    accepts both that and a bare string, because guessing one and getting it
    wrong costs a silently dropped job.

    Fails safe in the direction that matters. A posting is only ever ADDED by
    this, never removed, so a wrong guess cannot corrupt results - the worst
    case is a posting whose ONLY Israeli location is a secondary one going
    unseen, which is the same outcome as not reading the field at all."""
    if not field:
        return []
    out = []
    for entry in _get_by_path(job, field) or []:
        if isinstance(entry, dict):
            value = entry.get("location") or entry.get("name") or ""
        else:
            value = entry
        if isinstance(value, str) and value:
            out.append(value)
    return out


def fetch_ashby(profile) -> list[Job]:
    """Ashby: one call, everything in `jobs`, no pagination.

    VERIFIED live on 2026-08-13 against zafran-security (26 postings) and
    tavily (18). Shape: {"jobs": [...], "apiVersion": 1} - no cursor, no
    total, no page parameter. The board is small on both, so "does not
    paginate" is confirmed only at that size (see _onboarding/verify_report.md).

    `secondary_location_field` in the profile is optional and defaults to
    nothing being read - see _secondary_locations for why it is handled
    defensively rather than confidently.

    `isListed` is deliberately NOT filtered on: it was true for all 44
    postings across both boards, so a filter on it has never been exercised
    against a false value, and dropping postings on an untested predicate is
    exactly the silent loss this project avoids."""
    api = profile.raw["api"]
    r = requests.get(api["endpoint"], timeout=20)
    r.raise_for_status()
    data = r.json()

    fields = api["fields"]
    jobs = []
    for j in data.get("jobs", []):
        location = _get_by_path(j, fields["location"]) or ""
        secondary = _secondary_locations(j, fields.get("secondary_location"))
        if not (is_relevant_location(location)
                or any(is_relevant_location(s) for s in secondary)):
            continue
        # Same rule as Greenhouse's offices fallback: show something real when
        # the primary field is empty but a secondary one carried the match.
        display_location = location or ", ".join(secondary)
        jobs.append(Job(
            id=str(_get_by_path(j, fields["id"]) or ""),
            title=(_get_by_path(j, fields["title"]) or "").strip(),
            location=display_location.strip(),
            url=_get_by_path(j, fields["url"]) or "",
            company=profile.slug,
            description=_inline_description(profile, j),
        ))
    return jobs


def fetch_comeet(profile) -> list[Job]:
    """Comeet: one call, a flat array of positions, no pagination.

    VERIFIED live on 2026-08-13 (this docstring previously recorded the token
    as never verified against a live company). Two things were established
    then and both matter here:

      - The API token is NOT the company uid. It is a separate per-company
        public value, resolved once at import time and baked into the profile's
        endpoint - never re-resolved at runtime, because resolving it costs a
        ~750 KB board-page fetch against a few KB for this call.
      - Every pagination parameter tried (page/limit/offset/skip) is IGNORED:
        the largest board in the set returned all 238 postings, with 238
        unique uids, no matter what was appended.

    The stable id is `uid` and the per-posting link is
    `url_comeet_hosted_page`; both come off the profile's field mapping.
    location_query_param in the profile, if present, is passed as a param;
    otherwise {}."""
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
            description=_inline_description(profile, p),
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
                description=_inline_description(profile, p),
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
    "ashby": fetch_ashby,   # added 2026-08-13, after a live verification
                             # against both boards in the shortlist
    # workable/recruitee/workday: planned but not implemented - per
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
            "If this is Workable/Recruitee/Workday - it needs a live "
            "verification against api_platforms.md first, then a handler.")
    return handler(profile)
