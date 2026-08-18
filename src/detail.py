# -*- coding: utf-8 -*-
"""
The detail layer: getting a posting's own description text.

The listing endpoints this project fetches return id/title/location/url and
nothing else - required experience lives on the posting's own page, or in an
optional description field the listing can be asked for. This module is the
only place that gap is closed, and it is driven entirely by the profile's
`detail_fetch` block, so adding a company never means hand-writing "how to
get the description for company X" into the codebase.

*** The one rule that must not be broken here ***

A detail-layer failure is NEVER a company failure. A 404, a dead selector, a
timeout, a page that renders nothing - all of them resolve to
description=None, which reads upstream as "undetermined", which PASSES and
gets flagged. They must not mark the company unhealthy, must not block the
state write, and must not abort the run. The existing health /
expected_min_jobs machinery concerns the LISTING and is deliberately
untouched by any of this: a listing returning 0 jobs is a broken selector
worth waking someone up for, while a description that won't load just means
one job gets sent with a "couldn't tell" flag on it.
"""

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import dataclasses
import html as html_lib
import json
import re
import sys

import requests
from bs4 import BeautifulSoup

from models import Job

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-alert-bot/1.0)"}
_TIMEOUT = 15

# A hard ceiling on detail requests. On a normal run this is never reached -
# a 3-hourly cron sees a handful of new postings, which is the whole reason
# the cost stays near zero. It exists for the abnormal run: a company
# reworking its board can turn every id over at once, and 300 sequential page
# fetches would blow the workflow's 20-minute timeout and take the state
# commit down with it. Jobs past the cap keep description=None, so the
# overflow fails open like every other miss.
MAX_DETAIL_FETCHES_PER_RUN = 40


class DetailBudget:
    """The remaining detail-fetch allowance for a WHOLE run.

    The number above was always described as a per-run cap and was in fact
    enforced per company, because each call re-started its own count. At three
    companies the difference is invisible; at a hundred it is the difference
    between 40 requests and 4000, which at a 15s timeout apiece cannot fit in
    any workflow timeout - and a run killed by the timeout commits no state at
    all, so the whole run's work is lost and the next run repeats it.

    Constructed once in run.py and passed down. A caller that doesn't pass one
    gets a fresh budget, which is the old per-call behaviour and is what the
    unit tests want."""

    def __init__(self, remaining: int | None = None) -> None:
        # Read at construction, not bound as a default argument, so the
        # module constant stays patchable.
        self.remaining = int(MAX_DETAIL_FETCHES_PER_RUN
                             if remaining is None else remaining)

    def take(self) -> bool:
        """Claims one fetch. False means the run's allowance is spent."""
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def normalize_description(raw: str | None, content_is_html: bool) -> str | None:
    """Prepares a raw description field for the parser.

    The unescape step is not cosmetic. Greenhouse's `content` is both HTML
    AND HTML-escaped: it arrives as "&lt;p&gt;...". Parsing it without
    unescaping yields text literally full of "&lt;p&gt;"; unescaping without
    then parsing as HTML loses the bullet structure everything downstream
    depends on. Both steps, in this order."""
    if not raw or not raw.strip():
        return None
    if content_is_html:
        return html_lib.unescape(raw)
    return raw


# Lever's `lists` field names its heading "text" and its bullets "content".
# Overridable per profile so this stays a default, not a hard-coded platform
# special case.
DEFAULT_SECTION_HEADING_FIELD = "text"
DEFAULT_SECTION_CONTENT_FIELD = "content"


def render_sections(sections: list,
                    heading_field: str = DEFAULT_SECTION_HEADING_FIELD,
                    content_field: str = DEFAULT_SECTION_CONTENT_FIELD) -> str | None:
    """Renders a STRUCTURED description field into HTML the parser can read.

    This exists because the richest real-world inline source is not a string.
    Lever's listing returns the requirement bullets only in `lists`, as
    [{"text": "All you need is:", "content": "<li>...</li><li>...</li>"}] -
    verified live against Mobileye, where `description` and `descriptionBody`
    carry nothing but the intro paragraph and yield a number on 0 of 25
    postings, while `lists` yields one on 18 of 25.

    The heading is emitted as a real <h3> rather than being flattened away,
    because heading text is what promotes an unmarked bullet to "mandatory" -
    and these are exactly the recruiter-invented headings the design was
    built around ("All you need is:")."""
    parts: list[str] = []

    for section in sections:
        if not isinstance(section, dict):
            continue
        heading = str(section.get(heading_field) or "").strip()
        content = str(section.get(content_field) or "").strip()
        if not heading and not content:
            continue
        if heading:
            parts.append(f"<h3>{heading}</h3>")
        # Lever hands back bare <li> runs with no <ul> around them. Wrapping
        # is what makes them survive as separate blocks instead of collapsing
        # into one; anything that isn't a list is passed through untouched.
        parts.append(f"<ul>{content}</ul>" if "<li" in content else content)

    return "".join(parts) or None


def normalize_inline_value(raw, content_is_html: bool,
                           heading_field: str = DEFAULT_SECTION_HEADING_FIELD,
                           content_field: str = DEFAULT_SECTION_CONTENT_FIELD
                           ) -> str | None:
    """An inline field's raw value -> description text, whatever shape it
    arrived in. Strings pass straight through; a list of sections is rendered
    first. Anything else reads as no description, which fails open."""
    if isinstance(raw, str):
        return normalize_description(raw, content_is_html)
    if isinstance(raw, list):
        return normalize_description(
            render_sections(raw, heading_field, content_field), content_is_html)
    return None


def _content_is_html(cfg: dict) -> bool:
    """Defaults to True: every ATS description field observed so far is HTML,
    and the fallback path in the parser handles the plain-text case anyway."""
    return bool(cfg.get("content_is_html", True))


def wants_detail_fetch(profile) -> bool:
    """Whether this company needs per-posting REQUESTS (as opposed to inline
    data, which the listing already carried, or nothing at all). Used to keep
    a disabled filter from triggering any work."""
    cfg = profile.detail_fetch
    return bool(cfg) and cfg.get("method") in ("html", "playwright",
                                               "embedded_json", "json")


def _detail_url(job: Job, cfg: dict) -> str:
    """The URL to fetch this posting's description from.

    url_template uses plain placeholder replacement rather than str.format -
    real careers URLs contain braces often enough (tracking params, encoded
    payloads) that format() would raise KeyError on a perfectly good
    template."""
    if cfg.get("url_source", "job_url") == "job_url":
        # url_rewrite is a [from, to] pair applied to the posting's own URL,
        # for the case where the description lives at a URL that is a simple
        # transform of the public one but is NOT expressible as a template -
        # because the varying part is the path, which no placeholder carries.
        #
        # Added 2026-08-19 for Workday, where the public page
        #   https://{host}/en-US/{site}/job/...
        # and the JSON the description lives in
        #   https://{host}/wday/cxs/{tenant}/{site}/job/...
        # differ only in that one segment. Verified live against CrowdStrike.
        rewrite = cfg.get("url_rewrite")
        if rewrite and len(rewrite) == 2 and rewrite[0] in job.url:
            return job.url.replace(rewrite[0], rewrite[1], 1)
        return job.url
    template = cfg.get("url_template") or ""
    return template.replace("{id}", job.id).replace("{url}", job.url)


def _extract_from_json(markup: str, cfg: dict) -> str | None:
    """Pulls the description out of a per-posting JSON API response.

    *** Why this is separate from embedded_json (added 2026-08-18) ***

    They sound like the same thing and are not. `embedded_json` digs a JSON
    blob out of an HTML page by finding a `VAR = {...}` assignment inside a
    <script>; this method is for an endpoint that returns JSON and nothing
    else, where there is no assignment to anchor on and json.loads is the
    whole parse.

    Written for Workday, whose listing carries no description at all - the
    posting fields are title/externalPath/locationsText/postedOn/bulletFields
    and that is everything. The description lives at
    `wday/cxs/{tenant}/{site}{externalPath}` under jobPostingInfo.jobDescription,
    verified live 2026-08-18 against CrowdStrike (8,934 characters of HTML).

    Costs one GET per posting, the same as html and embedded_json, and draws
    on the same run-wide budget - which is why the cheaper rungs were checked
    first and genuinely ruled out rather than skipped.
    """
    try:
        obj = json.loads(markup)
    except ValueError:
        return None

    current = obj
    for part in (cfg.get("json_path") or "").split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)

    heading = cfg.get("inline_section_heading", DEFAULT_SECTION_HEADING_FIELD)
    content = cfg.get("inline_section_content", DEFAULT_SECTION_CONTENT_FIELD)

    # A list of {name, value} sections takes the same path Lever's `lists` and
    # Comeet's embedded blob take, so section headings still become real <h3>s
    # and heading promotion in experience.classify_block keeps working.
    if isinstance(current, list):
        return render_sections(current, heading, content)

    # A MAPPING of sections is the same data keyed instead of ordered -
    # SmartRecruiters returns {"qualifications": {"title": ..., "text": ...},
    # ...}. Verified live 2026-08-19 against fairtility. Its values are taken
    # in insertion order, which is the order the sections were authored in, so
    # "Qualifications" still lands under its own heading and the bullets
    # beneath it are still promoted to mandatory.
    if isinstance(current, dict) and all(
            isinstance(v, dict) for v in current.values()):
        return render_sections(list(current.values()), heading, content)

    return current if isinstance(current, str) else None


_EMBEDDED_JSON_VAR_DEFAULT = "POSITION_DATA"


def _extract_from_embedded_json(markup: str, cfg: dict) -> str | None:
    """Pulls the description out of a JSON blob embedded in a page's script.

    *** Why this method exists at all ***

    It was written for Comeet, which is 105 of this project's 145 companies -
    so without it the experience filter is off for 72% of the corpus, which is
    what it silently was until 2026-08-13.

    Comeet cannot be reached any other way, and each cheaper rung was ruled out
    against live responses rather than assumed:
      - `inline` is impossible: NEITHER the positions listing NOR the
        per-position API endpoint returns any description field. (The Comeet
        platform profile previously claimed the listing carried a `details`
        array. It does not - that claim was written without being verified.)
      - `html` is impossible: the hosted posting page is an Angular app, so the
        description is not in the server-rendered DOM and no content_selector
        can reach it. The text exists only inside a `POSITION_DATA = {...}`
        assignment in a <script>.

    The blob is located by its ASSIGNMENT, not by the first mention of the
    variable name - the name appears several times before it, and anchoring on
    the name alone parses the wrong object. That failure was observed and is
    the nasty kind: it silently yielded the COMPANY object, whose custom_fields
    is empty, so every posting came back with zero sections and looked exactly
    like a company that writes no requirements.

    `json.JSONDecoder().raw_decode` is what makes this safe: it consumes
    exactly one JSON value from the given offset and stops, so no brace
    counting is needed to find where the object ends.

    The extracted value is a list of {name, value} sections, fed straight into
    render_sections - the same path Lever's `lists` takes. That reuse is the
    point: the section NAME ("Requirements") becomes a real <h3>, and heading
    promotion in experience.classify_block is what makes the unmarked bullets
    underneath it count as mandatory.
    """
    variable = cfg.get("embedded_json_var", _EMBEDDED_JSON_VAR_DEFAULT)
    match = re.search(re.escape(variable) + r"\s*=\s*\{", markup)
    if not match:
        return None

    try:
        # end() - 1 puts the offset on the opening brace itself.
        obj, _ = json.JSONDecoder().raw_decode(markup, match.end() - 1)
    except ValueError:
        return None

    current = obj
    for part in (cfg.get("embedded_json_path") or "").split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)

    if isinstance(current, list):
        return render_sections(
            current,
            cfg.get("inline_section_heading", DEFAULT_SECTION_HEADING_FIELD),
            cfg.get("inline_section_content", DEFAULT_SECTION_CONTENT_FIELD))
    if isinstance(current, str):
        return current
    return None


def _extract_from_html(markup: str, cfg: dict) -> str | None:
    """Pulls the description subtree out of a fetched page.

    requirements_section_selector is tried first but is only ever a hint: it
    is why the code falls back to content_selector instead of failing when it
    misses. Real postings title their requirements block whatever the
    recruiter felt like ("All you need is:"), so no selector can be relied on
    to find it - the parser does the real work per-bullet instead."""
    soup = BeautifulSoup(markup, "html.parser")

    section_selector = cfg.get("requirements_section_selector")
    if section_selector:
        section = soup.select_one(section_selector)
        if section:
            return str(section)

    element = soup.select_one(cfg["content_selector"])
    return str(element) if element else None


def _fetch_html_descriptions(jobs: list[Job], cfg: dict, slug: str,
                             budget: DetailBudget, extract=None) -> list[Job]:
    """One plain GET per posting. Each failure is isolated to its own job.

    `extract` is the page-to-text step, defaulting to the CSS-selector one.
    It is a parameter so the embedded-JSON method reuses this loop rather
    than copying it - the budget accounting, the connection reuse and the
    per-job fail-open handler are the parts that must not diverge between
    two methods that differ only in how they read one fetched page."""
    extract = extract or _extract_from_html
    enriched: list[Job] = []
    session = requests.Session()      # reuse the connection across postings
    session.headers.update(_HEADERS)

    for index, job in enumerate(jobs):
        if not budget.take():
            print(f"[detail cap] {slug}: the run's detail-fetch budget is "
                  f"spent; the remaining {len(jobs) - index} jobs stay "
                  "undetermined (they are still sent, flagged)",
                  file=sys.stderr)
            enriched.extend(jobs[index:])
            break

        try:
            response = session.get(_detail_url(job, cfg), timeout=_TIMEOUT)
            response.raise_for_status()
            raw = extract(response.text, cfg)
            enriched.append(dataclasses.replace(
                job, description=normalize_description(raw, _content_is_html(cfg))))
        except Exception as e:
            # Swallowed on purpose, and only here: this is the fail-open
            # boundary. Logged so a systematically dead selector is still
            # visible in the run log rather than being invisible.
            print(f"[detail miss] {slug}/{job.id}: {e}", file=sys.stderr)
            enriched.append(job)

    return enriched


def _fetch_playwright_descriptions(jobs: list[Job], cfg: dict, slug: str,
                                   budget: DetailBudget) -> list[Job]:
    """Same as the html path, but for postings that only render under JS.

    The browser is launched ONCE for the whole company, not per posting -
    a chromium launch costs seconds, and this is already the most expensive
    path in the project."""
    from playwright.sync_api import sync_playwright   # imported lazily - most
                                                      # companies never need it

    enriched: list[Job] = []
    wait_for = cfg.get("wait_for")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=_HEADERS["User-Agent"])
            try:
                for index, job in enumerate(jobs):
                    if not budget.take():
                        print(f"[detail cap] {slug}: the run's detail-fetch "
                              f"budget is spent; the remaining "
                              f"{len(jobs) - index} jobs stay undetermined "
                              "(still sent, flagged)", file=sys.stderr)
                        enriched.extend(jobs[index:])
                        break
                    try:
                        page.goto(_detail_url(job, cfg), timeout=30000)
                        if wait_for:
                            page.wait_for_selector(wait_for, timeout=10000)
                        raw = _extract_from_html(page.content(), cfg)
                        enriched.append(dataclasses.replace(
                            job, description=normalize_description(
                                raw, _content_is_html(cfg))))
                    except Exception as e:
                        print(f"[detail miss] {slug}/{job.id}: {e}", file=sys.stderr)
                        enriched.append(job)
            finally:
                browser.close()
    except Exception as e:
        # The browser itself failed to start. Still not a company failure -
        # every job simply stays undetermined.
        print(f"[detail miss] {slug}: browser unavailable: {e}", file=sys.stderr)
        return jobs

    return enriched


def enrich(jobs: list[Job], profile, budget: DetailBudget | None = None) -> list[Job]:
    """jobs -> the same jobs, with `description` filled in where possible.

    Returns a list of the same length and order in every case, so a caller
    can never lose a job to the detail layer.

    `budget` is the whole run's remaining request allowance, shared across
    companies. Omitting it gives this call its own fresh budget."""
    if not jobs:
        return jobs

    cfg = profile.detail_fetch
    if not cfg:
        return jobs          # v2 profile, or method "none" - all undetermined

    budget = budget if budget is not None else DetailBudget()

    method = cfg.get("method")
    if method == "inline":
        # Nothing to do: the listing response already carried the description
        # and the fetcher populated it. Zero extra requests, which is why
        # inline is checked for before any per-job request is ever built - and
        # why it does not touch the budget at all.
        return jobs
    if method == "html":
        return _fetch_html_descriptions(jobs, cfg, profile.slug, budget)
    if method == "embedded_json":
        # Same one-GET-per-posting cost as html; only the extraction of
        # text from the fetched page differs.
        return _fetch_html_descriptions(jobs, cfg, profile.slug, budget,
                                        _extract_from_embedded_json)
    if method == "json":
        # Same one-GET-per-posting cost as html; the fetched body is JSON
        # rather than markup, so only the extraction step differs.
        return _fetch_html_descriptions(jobs, cfg, profile.slug, budget,
                                        _extract_from_json)
    if method == "playwright":
        return _fetch_playwright_descriptions(jobs, cfg, profile.slug, budget)

    return jobs
