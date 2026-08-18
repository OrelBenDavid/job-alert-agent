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


# The strings a board uses to say "this posting's location field carries no
# place". Only when one of these is the location does a secondary field
# (Greenhouse offices[], Ashby secondaryLocations[]) get to decide relevance.
_UNRESOLVED_LOCATIONS = {"", "multiple locations", "multiple location",
                         "various", "various locations", "multiple"}


def _is_unresolved_location(location: str) -> bool:
    return (location or "").strip().lower() in _UNRESOLVED_LOCATIONS


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

    # *** inline_prefix ***
    #
    # Some platforms expose the requirements as their OWN field, already
    # separated from the marketing copy - which sounds ideal and parses badly.
    # experience.classify_block decides what a bullet means from the heading
    # above it, and a field that is nothing but "<ul><li>…" has no heading at
    # all, so its bullets are never classified as requirements.
    #
    # Measured on HiBob, 17 Israel-relevant postings: the bare `requirements`
    # field yielded a years-of-experience number on 2, and the identical
    # content behind a synthetic "<h3>Requirements</h3>" yielded one on all 17.
    # The text was always there; only the signal that it was requirements text
    # was missing.
    #
    # This is a per-profile string rather than something inferred, because
    # prepending a requirements heading to a field that is NOT requirements
    # would do the opposite of help - it would promote nice-to-haves into
    # hard requirements and start suppressing jobs the user should see.
    prefix = cfg.get("inline_prefix") or ""
    if prefix and isinstance(raw, str):
        raw = prefix + raw

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
        title = (_get_by_path(p, fields["title"]) or "").strip()
        if not is_relevant_location(location, title):
            continue   # post_fetch - the correct approach for an api profile (SKILL Step 5)
        jobs.append(Job(
            id=str(_get_by_path(p, fields["id"]) or p.get("hostedUrl", "")),
            title=title,
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
        title = (_get_by_path(j, fields["title"]) or "").strip()
        offices = [o.get("name", "") for o in (j.get("offices") or [])]

        # *** offices[] is a FALLBACK, not an OR - fixed 2026-08-18 ***
        #
        # This used to keep a posting when location.name OR any office was
        # Israeli, which reads reasonably and is wrong. Greenhouse's offices[]
        # is not "where this job is"; on large boards it is the hiring
        # region's whole office list, attached to every posting in it.
        # Datadog publishes ['Bordeaux', 'Lisbon', 'Lyon', 'Madrid',
        # 'Montpellier', 'Nantes', 'Paris', 'Remote - France',
        # 'Sophia Antipolis', 'Tel Aviv'] on roles whose location.name is
        # plainly "Paris, France" - so the Tel Aviv entry kept 13 French and
        # Spanish jobs, and 21 across the whole corpus.
        #
        # The platform profile always said what this field is for: resolving
        # the literal "Multiple Locations" string, which is the one case where
        # location.name carries no place at all. So it is consulted only then.
        if _is_unresolved_location(location):
            relevant = any(is_relevant_location(o, title) for o in offices)
        else:
            relevant = is_relevant_location(location, title)
        if not relevant:
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
        title = (_get_by_path(j, fields["title"]) or "").strip()
        secondary = _secondary_locations(j, fields.get("secondary_location"))
        # Deliberately still an OR, unlike Greenhouse's offices[] above.
        # secondaryLocations is documented as further locations the posting is
        # genuinely open in, not a regional office list - so a posting whose
        # only Israeli location is a secondary one must survive. See
        # _secondary_locations for why this stays the fail-open direction.
        if not (is_relevant_location(location, title)
                or any(is_relevant_location(s, title) for s in secondary)):
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


def fetch_hibob(profile) -> list[Job]:
    """HiBob's own careers product, on the company's own subdomain.

    VERIFIED live on 2026-08-13 against hibob-fa0ad69d0cb34a.careers.hibob.com
    (62 postings, 17 in Israel). Added for a single company, which is unusual -
    the justification is that HiBob dogfoods its own hiring product, so there
    is no third-party ATS to point at, and 17 Israeli roles is more than most
    companies in this project's corpus contribute.

    *** The Referer header is required, not decoration ***

    The endpoint returns HTTP 401 to a plain request and 200 with the same URL
    once Referer (and a browser-ish User-Agent) are set. That is a soft
    anti-scraping check rather than authentication - no cookie or token is
    involved - but it means this handler cannot share the bare requests.get
    the other platforms use. It is also the most likely thing to break here: if
    the check is ever tightened, this returns 401, which surfaces as an
    ordinary fetch error and reaches a maintenance alert after two consecutive
    runs. That is the loud failure, which is the acceptable one.

    Response shape: {"filterGroups": {...}, "jobAdDetails": [...]}. Location
    lives in `country` as a full name ("Israel"), NOT in `site`, which carries
    a two-letter code ("IL") that the relevance filter does not and should not
    match - "il" is far too short to be a safe keyword.
    """
    api = profile.raw["api"]
    endpoint = api["endpoint"]
    # Derived from the endpoint rather than stored separately: the two must
    # agree, and a profile that could set them independently would eventually
    # set them inconsistently.
    origin = "/".join(endpoint.split("/")[:3])
    r = requests.get(endpoint, timeout=20, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
        "Accept": "application/json",
        "Referer": origin + "/",
    })
    r.raise_for_status()
    data = r.json()

    fields = api["fields"]
    jobs = []
    for j in data.get("jobAdDetails", []):
        location = _get_by_path(j, fields["location"]) or ""
        title = (_get_by_path(j, fields["title"]) or "").strip()
        if not is_relevant_location(location, title):
            continue
        job_id = str(_get_by_path(j, fields["id"]) or "")
        jobs.append(Job(
            id=job_id,
            title=title,
            location=location.strip(),
            # The listing carries no per-posting link, only an id - so the URL
            # is built from the template in the profile. url is mandatory on
            # Job, and a blank one would render as an unlinked bullet forever.
            url=api["job_url_template"].replace("{id}", job_id),
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
        title = (_get_by_path(p, fields["title"]) or "").strip()
        if not is_relevant_location(location, title):
            continue
        jobs.append(Job(
            id=str(_get_by_path(p, fields["id"]) or p.get("id", "")),
            title=title,
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
            if not is_relevant_location(location, p.get("name", "").strip()):
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
    "hibob": fetch_hibob,   # added 2026-08-13; single-company, see the
                             # handler for why that was judged worth it
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
