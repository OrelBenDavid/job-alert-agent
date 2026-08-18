# -*- coding: utf-8 -*-
"""
Fetching jobs via Playwright - the most expensive path, and therefore the
one where every bug costs the most (run time, site blocks, GitHub Actions
timeouts).

This is the generic, profile-driven expression of the four pagination
strategies plus the flat_multi_location template, paralleling
assets/function_templates.py in the skill. The bug fixes documented in
references/investigation_playbook.md (v2) are implemented here once,
instead of being duplicated per company.
"""

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import os
import time
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from models import Job
from relevance import is_relevant_location, is_israel_location
from urls import canonicalize_url

_USER_AGENT = "Mozilla/5.0 (compatible; job-alert-bot/1.0)"
_DEFAULT_MAX_PAGES = 50
_DEFAULT_MAX_SCROLLS = 60

# Two DIFFERENT selector timeouts, because the same expiry means two opposite
# things depending on which page it happens on.
#
# On the FIRST page it means "the listing never rendered" - a slow site, a
# cold CDN, a runner under load. On any LATER page it means "there is no page
# N" - the ordinary, expected end of pagination.
#
# The first-page budget is deliberately the more generous of the two: it is
# paid once per company and only on the failure path, whereas shortening it
# buys nothing but a faster wrong answer. careers.wix.com, the profile this
# was measured against, renders its cards 4-9s after `load` fires on a warm
# connection and materially slower on a cold one.
_DEFAULT_FIRST_PAGE_TIMEOUT_MS = 30000
_DEFAULT_NEXT_PAGE_TIMEOUT_MS = 10000


# A wall-clock ceiling on ONE company's paginated walk.
#
# Every individual Playwright call here is already bounded, but the number of
# calls is not: url_param with max_pages=20 is up to 20 goto+wait pairs, so a
# single slow company can legitimately occupy ten minutes. That matters
# because fetch_all's run deadline can only stop companies that have not
# STARTED - a thread holding a Chromium cannot be interrupted - so without a
# per-company ceiling one slow site still drags the run past the workflow
# timeout, and a timed-out run commits no state at all.
#
# Exceeding it RAISES rather than returning what was collected so far.
# Returning a partial page walk would be a silent truncation, which is the
# single failure this project refuses to allow: it looks exactly like a
# healthy company with fewer jobs.
_DEFAULT_COMPANY_BUDGET_SECONDS = 240


class FetchBudgetExceeded(Exception):
    """One company's paginated walk ran past its wall-clock budget.

    Deliberately an error, not a truncation - see the note above. Treated by
    run.py exactly like any other fetch failure: state untouched, retried next
    run, escalated only after the repeated-failure threshold."""


class ListingNeverRendered(Exception):
    """The first page of a listing did not produce a single job card.

    A distinct exception type because this is the one browser failure that
    must NOT be confused with "this company has no open jobs". It propagates
    out of fetch() so run.py's per-company handler treats it as a fetch
    error - state untouched, retried next run - instead of it decaying into
    an empty list that the health gate then reports as a broken selector.
    """


def _selector_timeouts(cfg: dict) -> tuple[int, int]:
    """(first-page, later-page) selector budgets in ms, profile-overridable.

    Per-profile because "slow" is a property of the site, not of the code: a
    heavy tracker-laden careers page needs a longer first-page budget than a
    static one, and hard-coding the worst case would slow every company's
    failure path down to the slowest company's."""
    pag = cfg.get("pagination") or {}
    return (int(pag.get("first_page_timeout_ms", _DEFAULT_FIRST_PAGE_TIMEOUT_MS)),
            int(pag.get("next_page_timeout_ms", _DEFAULT_NEXT_PAGE_TIMEOUT_MS)))


def _company_deadline(cfg: dict) -> float:
    """The absolute monotonic time this company's walk must finish by.

    Profile-overridable via `pagination.max_seconds`, and env-overridable via
    JOB_ALERT_COMPANY_BUDGET_SECONDS, for the same reason the worker counts
    are: how long is too long depends on the runner and on the site, neither
    of which is a property of this code."""
    pag = cfg.get("pagination") or {}
    try:
        default = int(os.environ.get("JOB_ALERT_COMPANY_BUDGET_SECONDS",
                                     _DEFAULT_COMPANY_BUDGET_SECONDS))
    except (TypeError, ValueError):
        default = _DEFAULT_COMPANY_BUDGET_SECONDS
    return time.monotonic() + max(1, int(pag.get("max_seconds", default)))


def _check_budget(deadline: float, profile, what: str) -> None:
    """Raises once the company's walk has run out of time."""
    if time.monotonic() > deadline:
        raise FetchBudgetExceeded(
            f"{profile.slug}: still {what} after the per-company time budget "
            "expired. Treated as a fetch failure, NOT as a short listing - "
            "returning a partial walk would be indistinguishable from a "
            "company with fewer jobs.")


def _await_first_page(page, profile, selector: str, timeout_ms: int,
                      reload_url: str | None = None) -> None:
    """Waits for the first page's cards, with ONE retry, then raises.

    The retry is not defensive padding - it is the documented behaviour of
    the sites being scraped. wix.json's own notes record a `goto` timing out
    once and succeeding immediately afterwards with no code change, and the
    same transient shows up under concurrency, when several browsers compete
    for a 2-core runner. One retry converts the overwhelmingly common
    transient into a normal run; anything that survives it is a real failure
    and is raised as one."""
    try:
        page.wait_for_selector(selector, timeout=timeout_ms)
        return
    except Exception:
        pass

    try:
        if reload_url:
            page.goto(reload_url, timeout=30000)
            _apply_relevance_filter_actions(page, profile.israel_filter)
        else:
            page.reload(timeout=30000)
            _apply_relevance_filter_actions(page, profile.israel_filter)
        page.wait_for_selector(selector, timeout=timeout_ms)
    except Exception as e:
        raise ListingNeverRendered(
            f"{profile.slug}: no job card matched {selector!r} within "
            f"{timeout_ms}ms, on two consecutive attempts. Treated as a fetch "
            "failure, NOT as zero open jobs.") from e


def _read_job_card(card, company: str, link_base: str, cfg: dict,
                   fallback_location: str = ""):
    """Reads a single job card from the DOM. Centralized here instead of
    duplicated in every strategy."""
    title_el = card.query_selector(cfg["title_selector"])
    location_el = card.query_selector(cfg["location_selector"])
    link_el = card.query_selector(cfg["link_selector"])
    title = title_el.inner_text().strip() if title_el else ""
    if not title:
        return None   # a card with no title isn't a real job (separator/skeleton)
    location = location_el.inner_text().strip() if location_el else fallback_location
    href = link_el.get_attribute("href") if link_el else ""
    canonical = canonicalize_url(href, link_base)
    return Job(id=canonical or f"{company}|{title}|{location}",
              title=title, location=location, url=canonical, company=company)


def _apply_relevance_filter_actions(page, israel_filter: dict) -> None:
    """Runs the UI actions recorded in the profile, if method ==
    ui_interaction. If method == url_param - nothing to do, the filter is
    already baked into the URL. If method == post_fetch - also nothing to
    do, the check happens in the scraping loop itself.

    ui_actions_structured is stored in the profile as a list of steps:
    each step is a dict with "action" (click/fill/press) and "selector" or
    "value". This is what turns the sequence recorded in Step 3a into code
    that actually runs, without hand-writing Python per company."""
    if israel_filter.get("method") != "ui_interaction":
        return
    for step in israel_filter.get("ui_actions_structured", []):
        action = step["action"]
        if action == "click":
            page.click(step["selector"])
        elif action == "fill":
            page.fill(step["selector"], step["value"])
        elif action == "press":
            page.keyboard.press(step["value"])
        elif action == "wait":
            page.wait_for_timeout(int(step["value"]))
        else:
            raise ValueError(f"unknown ui_actions action: {action!r}")


def _next_is_unavailable(btn, disabled_marker: str) -> bool:
    """Whether the "Next" button is actually disabled.

    Playwright's is_disabled() only checks the disabled property of form
    elements, and returns False for <a class="disabled"> and
    aria-disabled="true" - which are the common forms on careers sites.
    Relying on is_disabled() alone means an infinite click loop on the
    last page (see investigation_playbook.md)."""
    if btn is None:
        return True
    if btn.get_attribute("aria-disabled") == "true":
        return True
    if "disabled" in (btn.get_attribute("class") or "").lower():
        return True
    if disabled_marker and disabled_marker in (btn.get_attribute("class") or ""):
        return True
    try:
        if btn.is_disabled() or not btn.is_visible():
            return True
    except Exception:
        return True   # element vanished from the DOM between checks - treat as "no more"
    return False


def _fetch_single_page(page, profile, cfg: dict) -> list[Job]:
    """Reads the cards already on the page. The caller is responsible for
    having waited for them - every strategy now does that through
    _await_first_page, so the wait carries the retry and the raise-on-failure
    behaviour uniformly instead of each strategy inventing its own."""
    jobs = []
    for card in page.query_selector_all(cfg["job_selector"]):
        job = _read_job_card(card, profile.slug, cfg.get("link_base", profile.careers_url), cfg)
        if job and is_relevant_location(job.location, job.title):
            jobs.append(job)
    return jobs


def _fetch_none(page, profile, cfg: dict) -> list[Job]:
    """pagination.method == none: a single page, no more."""
    first_timeout, _ = _selector_timeouts(cfg)
    page.goto(profile.careers_url, timeout=30000)
    _apply_relevance_filter_actions(page, profile.israel_filter)
    _await_first_page(page, profile, cfg["job_selector"], first_timeout)
    return _fetch_single_page(page, profile, cfg)


def _fetch_url_pages(page, profile, cfg: dict) -> list[Job]:
    """pagination.method == url_param.

    Two critical fixes documented here:
    1) the filter is re-applied after EVERY navigation - page.goto resets
       any JS-only state, and without re-applying it every page past the
       first would come back unfiltered.
    2) a hard page cap plus a per-page content fingerprint - double
       protection against a site that clamps ?page=999 back to page 1
       instead of returning an empty list."""
    pag = cfg["pagination"]
    base_url = profile.careers_url
    start = int(pag.get("start_value", 1))
    max_pages = int(pag.get("max_pages", _DEFAULT_MAX_PAGES))
    page_param = pag["param_name"]

    first_timeout, next_timeout = _selector_timeouts(cfg)

    deadline = _company_deadline(cfg)
    jobs, seen_ids, previous_fingerprint = [], set(), None
    for page_num in range(start, start + max_pages):
        _check_budget(deadline, profile, f"paginating (page {page_num})")
        sep = "&" if "?" in base_url else "?"
        page_url = f"{base_url}{sep}{page_param}={page_num}"
        page.goto(page_url, timeout=30000)
        _apply_relevance_filter_actions(page, profile.israel_filter)   # (1)

        # (3) The first page is held to a different standard than the rest.
        # Treating its timeout as "no more pages" - which is what this loop
        # used to do for every page alike - silently returned an EMPTY list
        # whenever the site was merely slow. That is the single worst outcome
        # available here: it is indistinguishable from a healthy company with
        # no open roles, and downstream it surfaces as a false "broken
        # selector" maintenance alert. Measured on wix, where the cards land
        # 4-9s after `load` and the old flat 10s budget was a coin flip under
        # any load at all.
        if page_num == start:
            _await_first_page(page, profile, cfg["job_selector"],
                              first_timeout, reload_url=page_url)
        else:
            try:
                page.wait_for_selector(cfg["job_selector"], timeout=next_timeout)
            except Exception:
                break   # no page N - the ordinary end of pagination

        cards = page.query_selector_all(cfg["job_selector"])
        if not cards:
            break

        page_jobs = [_read_job_card(c, profile.slug,
                                    cfg.get("link_base", base_url), cfg)
                    for c in cards]
        page_jobs = [j for j in page_jobs if j]

        fingerprint = tuple(j.id for j in page_jobs)   # (2)
        if fingerprint == previous_fingerprint:
            break
        previous_fingerprint = fingerprint

        for job in page_jobs:
            if job.id in seen_ids or not is_relevant_location(job.location, job.title):
                continue
            seen_ids.add(job.id)
            jobs.append(job)

    return jobs


def _fetch_click_next(page, profile, cfg: dict) -> list[Job]:
    """pagination.method == click. The filter is applied once, before the
    loop - correct here, since there are no navigations that reset state
    (unlike url_pages)."""
    pag = cfg["pagination"]
    max_pages = int(pag.get("max_pages", _DEFAULT_MAX_PAGES))
    disabled_marker = pag.get("disabled_marker", "")

    first_timeout, _ = _selector_timeouts(cfg)
    page.goto(profile.careers_url, timeout=30000)
    _apply_relevance_filter_actions(page, profile.israel_filter)
    _await_first_page(page, profile, cfg["job_selector"], first_timeout)

    deadline = _company_deadline(cfg)
    jobs, seen_ids = [], set()
    for page_num in range(max_pages):
        _check_budget(deadline, profile, f"clicking Next (page {page_num + 1})")
        new_here = 0
        for card in page.query_selector_all(cfg["job_selector"]):
            job = _read_job_card(card, profile.slug,
                                 cfg.get("link_base", profile.careers_url), cfg)
            if not job or job.id in seen_ids:
                continue
            new_here += 1
            seen_ids.add(job.id)
            if is_relevant_location(job.location, job.title):
                jobs.append(job)

        next_btn = page.query_selector(pag["next_button_selector"])
        if _next_is_unavailable(next_btn, disabled_marker):
            break
        if new_here == 0:
            break   # clicked but nothing new appeared - the site is stuck, stop here

        next_btn.click()
        page.wait_for_timeout(1200)

    return jobs


def _fetch_scroll(page, profile, cfg: dict) -> list[Job]:
    """pagination.method == scroll.

    Two fixes: (1) only stop after 3 consecutive stable checks, not one -
    a single check stops too early on a slow network. (2) support for an
    inner scroll container via JS, since page.mouse.wheel does nothing if
    the list scrolls inside a div rather than the window."""
    pag = cfg["pagination"]
    max_scrolls = int(pag.get("max_scrolls", _DEFAULT_MAX_SCROLLS))
    container = cfg.get("scroll_container_selector", "")
    stable_rounds_required = 3

    first_timeout, _ = _selector_timeouts(cfg)
    page.goto(profile.careers_url, timeout=30000)
    _apply_relevance_filter_actions(page, profile.israel_filter)
    _await_first_page(page, profile, cfg["job_selector"], first_timeout)

    deadline = _company_deadline(cfg)
    previous_count, stable_rounds = 0, 0
    for scroll_num in range(max_scrolls):
        _check_budget(deadline, profile, f"scrolling (round {scroll_num + 1})")
        if container:
            page.eval_on_selector(container, "el => el.scrollTop = el.scrollHeight")
        else:
            page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1000)

        current_count = len(page.query_selector_all(cfg["job_selector"]))
        if current_count == previous_count:
            stable_rounds += 1
            if stable_rounds >= stable_rounds_required:
                break
        else:
            stable_rounds = 0
        previous_count = current_count

    jobs_by_id = {}
    for card in page.query_selector_all(cfg["job_selector"]):
        job = _read_job_card(card, profile.slug,
                             cfg.get("link_base", profile.careers_url), cfg)
        if job and is_relevant_location(job.location, job.title):
            jobs_by_id[job.id] = job   # dedupe - scrolling sometimes duplicates cards
    return list(jobs_by_id.values())


def _fetch_multi_location(page, profile, cfg: dict) -> list[Job]:
    """israel_filter.structure == flat_multi_location - only when there's
    no API alternative (the first thing checked before getting here is
    Step 1a/1b in the skill).

    Three fixes: (1) quote() on the city name - "Tel-Aviv, Israel"
    contains a space and a comma that can't go raw into a URL. (2)
    pagination WITHIN each city, not just the first page. (3) merge by
    id, not by title - the same title in two cities is usually two
    different jobs."""
    israel_filter = profile.israel_filter
    pag = cfg["pagination"]
    base_url = profile.careers_url
    start = int(pag.get("start_value", 1))
    max_pages = int(pag.get("max_pages", _DEFAULT_MAX_PAGES))
    page_param = pag.get("param_name", "page")
    location_param = israel_filter["param"]

    page.goto(base_url, timeout=30000)
    page.click(cfg["location_filter_selector"])
    options = [el.inner_text().strip()
              for el in page.query_selector_all(cfg["location_option_selector"])]
    israel_options = [o for o in options if is_israel_location(o)]
    page.keyboard.press("Escape")

    _, next_timeout = _selector_timeouts(cfg)

    # (4) Unlike the single-listing strategies, an individual city here may
    # legitimately render nothing - a real office with no current openings
    # (wix's Beer Sheva, at the time its profile was written). So the
    # "did anything render?" question can only be asked across the WHOLE
    # walk, not per city: every city coming back empty means either the site
    # never rendered or the company genuinely has nothing open anywhere, and
    # the safe reading of that ambiguity is a fetch failure, not zero jobs.
    deadline = _company_deadline(cfg)
    any_page_rendered = False
    jobs_by_id = {}
    for city in israel_options:
        for page_num in range(start, start + max_pages):
            # The budget matters most here: this is the one strategy whose
            # cost is cities x pages, not pages.
            _check_budget(deadline, profile, f"walking {city} (page {page_num})")
            sep = "&" if "?" in base_url else "?"
            city_url = f"{base_url}{sep}{location_param}={quote(city)}&{page_param}={page_num}"
            page.goto(city_url, timeout=30000)
            try:
                page.wait_for_selector(cfg["job_selector"], timeout=next_timeout)
            except Exception:
                break
            any_page_rendered = True

            cards = page.query_selector_all(cfg["job_selector"])
            if not cards:
                break

            added_here = 0
            for card in cards:
                job = _read_job_card(card, profile.slug,
                                     cfg.get("link_base", base_url), cfg,
                                     fallback_location=city)
                if not job:
                    continue
                if job.id not in jobs_by_id:
                    added_here += 1
                jobs_by_id[job.id] = job

            if added_here == 0:
                break

    if not any_page_rendered:
        raise ListingNeverRendered(
            f"{profile.slug}: none of the {len(israel_options)} Israel "
            f"location(s) rendered a single job card. Treated as a fetch "
            "failure, NOT as zero open jobs.")

    return list(jobs_by_id.values())


_PAGINATION_DISPATCH = {
    "none": _fetch_none,
    "url_param": _fetch_url_pages,
    "click": _fetch_click_next,
    "scroll": _fetch_scroll,
}


def fetch(profile) -> list[Job]:
    """The entry point the main dispatcher calls for fetch_type ==
    "playwright". Picks a strategy based on israel_filter.structure and
    pagination.method - two profile fields, not a manual registration of
    the company."""
    cfg = profile.raw["playwright"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            # new_page() inside the try, not before it: a launch that succeeds
            # and a new_page() that then fails used to skip browser.close()
            # entirely, leaking a Chromium per failure in the pool.
            page = browser.new_page(user_agent=_USER_AGENT)
            if profile.israel_filter.get("structure") == "flat_multi_location":
                jobs = _fetch_multi_location(page, profile, cfg)
            else:
                method = cfg["pagination"]["method"]
                handler = _PAGINATION_DISPATCH.get(method)
                if handler is None:
                    raise NotImplementedError(
                        f"{profile.slug}: pagination.method not implemented: {method!r}")
                jobs = handler(page, profile, cfg)
        finally:
            browser.close()
    return jobs
