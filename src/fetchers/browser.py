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

from urllib.parse import quote

from playwright.sync_api import sync_playwright

from models import Job
from relevance import is_relevant_location, is_israel_location
from urls import canonicalize_url

_USER_AGENT = "Mozilla/5.0 (compatible; job-alert-bot/1.0)"
_DEFAULT_MAX_PAGES = 50
_DEFAULT_MAX_SCROLLS = 60


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
    page.wait_for_selector(cfg["job_selector"], timeout=15000)
    jobs = []
    for card in page.query_selector_all(cfg["job_selector"]):
        job = _read_job_card(card, profile.slug, cfg.get("link_base", profile.careers_url), cfg)
        if job and is_relevant_location(job.location):
            jobs.append(job)
    return jobs


def _fetch_none(page, profile, cfg: dict) -> list[Job]:
    """pagination.method == none: a single page, no more."""
    page.goto(profile.careers_url, timeout=30000)
    _apply_relevance_filter_actions(page, profile.israel_filter)
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

    jobs, seen_ids, previous_fingerprint = [], set(), None
    for page_num in range(start, start + max_pages):
        sep = "&" if "?" in base_url else "?"
        page.goto(f"{base_url}{sep}{page_param}={page_num}", timeout=30000)
        _apply_relevance_filter_actions(page, profile.israel_filter)   # (1)
        try:
            page.wait_for_selector(cfg["job_selector"], timeout=10000)
        except Exception:
            break

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
            if job.id in seen_ids or not is_relevant_location(job.location):
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

    page.goto(profile.careers_url, timeout=30000)
    _apply_relevance_filter_actions(page, profile.israel_filter)
    page.wait_for_selector(cfg["job_selector"], timeout=15000)

    jobs, seen_ids = [], set()
    for _ in range(max_pages):
        new_here = 0
        for card in page.query_selector_all(cfg["job_selector"]):
            job = _read_job_card(card, profile.slug,
                                 cfg.get("link_base", profile.careers_url), cfg)
            if not job or job.id in seen_ids:
                continue
            new_here += 1
            seen_ids.add(job.id)
            if is_relevant_location(job.location):
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

    page.goto(profile.careers_url, timeout=30000)
    _apply_relevance_filter_actions(page, profile.israel_filter)
    page.wait_for_selector(cfg["job_selector"], timeout=15000)

    previous_count, stable_rounds = 0, 0
    for _ in range(max_scrolls):
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
        if job and is_relevant_location(job.location):
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

    jobs_by_id = {}
    for city in israel_options:
        for page_num in range(start, start + max_pages):
            sep = "&" if "?" in base_url else "?"
            city_url = f"{base_url}{sep}{location_param}={quote(city)}&{page_param}={page_num}"
            page.goto(city_url, timeout=30000)
            try:
                page.wait_for_selector(cfg["job_selector"], timeout=10000)
            except Exception:
                break

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
        page = browser.new_page(user_agent=_USER_AGENT)
        try:
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
