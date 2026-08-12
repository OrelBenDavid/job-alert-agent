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
import sys

import requests
from bs4 import BeautifulSoup

from models import Job

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-alert-bot/1.0)"}
_TIMEOUT = 15

# A hard ceiling on per-run detail requests. On a normal run this is never
# reached - a 3-hourly cron sees a handful of new postings, which is the
# whole reason the cost stays near zero. It exists for the abnormal run: a
# company reworking its board can turn every id over at once, and 300
# sequential page fetches would blow the workflow's 20-minute timeout and
# take the state commit down with it. Jobs past the cap keep
# description=None, so the overflow fails open like every other miss.
MAX_DETAIL_FETCHES_PER_RUN = 40


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
    return bool(cfg) and cfg.get("method") in ("html", "playwright")


def _detail_url(job: Job, cfg: dict) -> str:
    """The URL to fetch this posting's description from.

    url_template uses plain placeholder replacement rather than str.format -
    real careers URLs contain braces often enough (tracking params, encoded
    payloads) that format() would raise KeyError on a perfectly good
    template."""
    if cfg.get("url_source", "job_url") == "job_url":
        return job.url
    template = cfg.get("url_template") or ""
    return template.replace("{id}", job.id).replace("{url}", job.url)


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


def _fetch_html_descriptions(jobs: list[Job], cfg: dict, slug: str) -> list[Job]:
    """One plain GET per posting. Each failure is isolated to its own job."""
    enriched: list[Job] = []
    session = requests.Session()      # reuse the connection across postings
    session.headers.update(_HEADERS)

    for index, job in enumerate(jobs):
        if index >= MAX_DETAIL_FETCHES_PER_RUN:
            print(f"[detail cap] {slug}: stopping after "
                  f"{MAX_DETAIL_FETCHES_PER_RUN} detail fetches; the "
                  f"remaining {len(jobs) - index} jobs stay undetermined "
                  "(they are still sent, flagged)", file=sys.stderr)
            enriched.extend(jobs[index:])
            break

        try:
            response = session.get(_detail_url(job, cfg), timeout=_TIMEOUT)
            response.raise_for_status()
            raw = _extract_from_html(response.text, cfg)
            enriched.append(dataclasses.replace(
                job, description=normalize_description(raw, _content_is_html(cfg))))
        except Exception as e:
            # Swallowed on purpose, and only here: this is the fail-open
            # boundary. Logged so a systematically dead selector is still
            # visible in the run log rather than being invisible.
            print(f"[detail miss] {slug}/{job.id}: {e}", file=sys.stderr)
            enriched.append(job)

    return enriched


def _fetch_playwright_descriptions(jobs: list[Job], cfg: dict, slug: str) -> list[Job]:
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
                    if index >= MAX_DETAIL_FETCHES_PER_RUN:
                        print(f"[detail cap] {slug}: stopping after "
                              f"{MAX_DETAIL_FETCHES_PER_RUN} detail fetches; "
                              f"the remaining {len(jobs) - index} jobs stay "
                              "undetermined (still sent, flagged)",
                              file=sys.stderr)
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


def enrich(jobs: list[Job], profile) -> list[Job]:
    """jobs -> the same jobs, with `description` filled in where possible.

    Returns a list of the same length and order in every case, so a caller
    can never lose a job to the detail layer."""
    if not jobs:
        return jobs

    cfg = profile.detail_fetch
    if not cfg:
        return jobs          # v2 profile, or method "none" - all undetermined

    method = cfg.get("method")
    if method == "inline":
        # Nothing to do: the listing response already carried the description
        # and the fetcher populated it. Zero extra requests, which is why
        # inline is checked for before any per-job request is ever built.
        return jobs
    if method == "html":
        return _fetch_html_descriptions(jobs, cfg, profile.slug)
    if method == "playwright":
        return _fetch_playwright_descriptions(jobs, cfg, profile.slug)

    return jobs
