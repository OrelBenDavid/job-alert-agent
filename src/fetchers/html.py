# -*- coding: utf-8 -*-
"""
Fetching jobs from static HTML - two possible sources, both reading their
selectors/structure from the profile: css (plain CSS selectors) and
json-ld (JobPosting blocks).
"""

import json as json_lib

import requests
from bs4 import BeautifulSoup

from models import Job
from relevance import is_relevant_location
from urls import canonicalize_url

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-alert-bot/1.0)"}


def _fetch_css(profile) -> list[Job]:
    cfg = profile.raw["html"]
    r = requests.get(profile.careers_url, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    jobs = []
    for card in soup.select(cfg["job_selector"]):
        title_el = card.select_one(cfg["title_selector"])
        location_el = card.select_one(cfg["location_selector"])
        link_el = card.select_one(cfg["link_selector"])
        title = title_el.get_text(strip=True) if title_el else ""
        location = location_el.get_text(strip=True) if location_el else ""
        href = link_el.get("href", "") if link_el else ""
        if not title or not is_relevant_location(location):
            continue
        canonical = canonicalize_url(href, cfg.get("link_base", profile.careers_url))
        jobs.append(Job(
            id=canonical or f"{profile.slug}|{title}|{location}",
            title=title, location=location, url=canonical, company=profile.slug,
        ))
    return jobs


def _fetch_jsonld(profile) -> list[Job]:
    cfg = profile.raw["html"]
    r = requests.get(profile.careers_url, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    jobs = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json_lib.loads(script.string or "{}")
        except json_lib.JSONDecodeError:
            continue   # malformed block - skip it, don't fail the whole run
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if entry.get("@type") != "JobPosting":
                continue
            addr = ((entry.get("jobLocation") or {}).get("address") or {})
            location = ", ".join(x for x in [addr.get("addressLocality"),
                                             addr.get("addressCountry")] if x)
            if not is_relevant_location(location):
                continue
            canonical = canonicalize_url(entry.get("url", ""),
                                         cfg.get("link_base", profile.careers_url))
            jobs.append(Job(
                id=str(entry.get("identifier") or canonical),
                title=(entry.get("title") or "").strip(),
                location=location, url=canonical, company=profile.slug,
            ))
    return jobs


def fetch(profile) -> list[Job]:
    """The entry point the main dispatcher calls for fetch_type == "html"."""
    source = profile.raw["html"].get("source", "css")
    if source == "json-ld":
        return _fetch_jsonld(profile)
    return _fetch_css(profile)
