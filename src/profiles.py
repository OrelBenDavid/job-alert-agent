# -*- coding: utf-8 -*-
"""
Loading and validation of profiles/<slug>.json - the profile is the only
registration a company has in this project. There is no separate
companies.json. See the career-site-profiler skill,
references/profile_schema.md, for the full schema these files must satisfy.
"""

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import json
from pathlib import Path
from dataclasses import dataclass, field

# v3 adds the optional `detail_fetch` block (how to reach a posting's own
# description text). v2 is still accepted and simply reads as having no
# detail_fetch - every existing profile stays valid, untouched, and its jobs
# resolve to "undetermined", which passes.
CURRENT_SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = (2, 3)
PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"

# The four ways a description can be reached, cheapest first - the same
# cheap-to-expensive rule the fetch_type dispatch follows.
DETAIL_METHODS = ("inline", "html", "playwright", "none")


class ProfileError(Exception):
    """Validation error in a profile - raised with the filename and the
    specific problem, never swallowed silently. An invalid profile must
    not lead to a silent 0-jobs run."""


@dataclass
class Profile:
    slug: str
    name: str
    enabled: bool
    careers_url: str
    fetch_type: str          # api | html | playwright
    israel_filter: dict
    health: dict
    raw: dict = field(repr=False)   # the full document - fetchers read
                                     # fetch_type-specific fields (api/html/
                                     # playwright) straight from this

    @property
    def expected_min_jobs(self) -> int:
        return int(self.health.get("expected_min_jobs", 0))

    @property
    def zero_is_plausible(self) -> bool:
        return bool(self.health.get("zero_is_plausible", False))

    @property
    def detail_fetch(self) -> dict | None:
        """How to reach a posting's description text, or None if this company
        has no way to (a v2 profile, or method "none"). None is a perfectly
        normal answer - it means every job from this company reads as
        "undetermined", which passes and gets flagged."""
        block = self.raw.get("detail_fetch")
        if not block or block.get("method") == "none":
            return None
        return block


def _validate_detail_fetch(block: dict, where: str) -> None:
    """Validates the optional v3 `detail_fetch` block.

    Held to the same standard as the rest of the schema - a half-specified
    block fails at load time rather than turning into a silent stream of
    per-job fetch errors on every run. Note this is the only place where a
    detail_fetch problem is ever allowed to be fatal: once a run is under way,
    a broken selector must degrade to "undetermined", never to a company
    failure (that distinction is the whole point of the detail layer)."""
    if not isinstance(block, dict):
        raise ProfileError(f"{where}detail_fetch must be an object")

    method = block.get("method")
    if method not in DETAIL_METHODS:
        raise ProfileError(
            f"{where}detail_fetch.method invalid: {method!r}. "
            f"Expected one of {DETAIL_METHODS}.")

    if method == "none":
        return   # an explicit "this company has no reachable description"

    if method == "inline":
        if not block.get("inline_field"):
            raise ProfileError(
                f"{where}detail_fetch.method='inline' but inline_field is "
                "missing - there's no way to know which listing field holds "
                "the description.")
        return   # inline costs no requests, so the url_* fields don't apply

    # html / playwright - both issue one request per new posting
    if not block.get("content_selector"):
        raise ProfileError(
            f"{where}detail_fetch.method={method!r} requires content_selector.")

    url_source = block.get("url_source", "job_url")
    if url_source not in ("job_url", "template"):
        raise ProfileError(
            f"{where}detail_fetch.url_source invalid: {url_source!r}. "
            "Expected 'job_url' or 'template'.")
    if url_source == "template" and not block.get("url_template"):
        raise ProfileError(
            f"{where}detail_fetch.url_source='template' but url_template is "
            "missing.")


def _validate(data: dict, path: Path) -> None:
    """Checks the fields required by the schema. Doesn't silently patch
    values - a profile with a missing field fails loudly, at load time,
    not silently in the middle of a fetch."""
    where = f"{path.name}: "

    version = data.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ProfileError(
            f"{where}schema_version={version!r}, expected one of "
            f"{SUPPORTED_SCHEMA_VERSIONS}. Profile is on an old/unknown "
            "schema - fix it or re-run the skill.")

    if "detail_fetch" in data:
        if version < CURRENT_SCHEMA_VERSION:
            # Not ignored silently: a v2 profile carrying a v3 block is a
            # contradiction, and quietly dropping it would leave the author
            # convinced the experience filter is working for this company
            # when it is in fact reading every job as undetermined.
            raise ProfileError(
                f"{where}detail_fetch is a v{CURRENT_SCHEMA_VERSION} field but "
                f"schema_version={version}. Bump schema_version to "
                f"{CURRENT_SCHEMA_VERSION} or remove the block.")
        _validate_detail_fetch(data["detail_fetch"], where)

    for req in ("slug", "name", "enabled", "careers_url", "fetch_type",
                "israel_filter", "health", "verified_on"):
        if req not in data:
            raise ProfileError(f"{where}missing required field: {req!r}")

    if data["slug"] != path.stem:
        raise ProfileError(
            f"{where}slug={data['slug']!r} doesn't match the filename {path.stem!r}")

    fetch_type = data["fetch_type"]
    if fetch_type not in ("api", "html", "playwright"):
        raise ProfileError(f"{where}unknown fetch_type: {fetch_type!r}")
    if fetch_type not in data:
        raise ProfileError(
            f"{where}fetch_type={fetch_type!r} but no {fetch_type!r} block in the profile")

    block = data[fetch_type]
    if fetch_type == "api":
        if not block.get("platform"):
            raise ProfileError(f"{where}api.platform missing")
        fields = block.get("fields", {})
        for f in ("id", "title", "location", "url"):
            if f not in fields:
                raise ProfileError(f"{where}api.fields is missing a mapping for {f!r}")
    elif fetch_type in ("html", "playwright"):
        for f in ("job_selector", "title_selector", "location_selector",
                  "link_selector"):
            if not block.get(f):
                raise ProfileError(
                    f"{where}{fetch_type}.{f} missing. link_selector is "
                    "mandatory in v2 - without it there's no stable job id.")
        if fetch_type == "playwright":
            pag = block.get("pagination", {})
            if pag.get("method") not in ("none", "url_param", "click", "scroll"):
                raise ProfileError(
                    f"{where}playwright.pagination.method invalid: "
                    f"{pag.get('method')!r}")

    health = data["health"]
    if "expected_min_jobs" not in health:
        raise ProfileError(
            f"{where}health.expected_min_jobs missing - this is the field "
            "that prevents 'silent zero' failures, it can't be skipped.")


def load_profile(path: Path) -> Profile:
    """Loads and parses a single profile. Raises ProfileError with details
    if something's invalid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ProfileError(f"{path.name}: invalid JSON - {e}") from e

    _validate(data, path)

    return Profile(
        slug=data["slug"], name=data["name"], enabled=data["enabled"],
        careers_url=data["careers_url"], fetch_type=data["fetch_type"],
        israel_filter=data["israel_filter"], health=data["health"], raw=data,
    )


def load_all(profiles_dir: Path = PROFILES_DIR) -> list[Profile]:
    """Loads every profile in the directory. A profile that fails
    validation is collected as a single error message with its filename -
    it does not stop the loading of the other profiles, so one broken
    company doesn't take down the whole run."""
    profiles, errors = [], []
    for path in sorted(profiles_dir.glob("*.json")):
        try:
            profiles.append(load_profile(path))
        except ProfileError as e:
            errors.append(str(e))
    return profiles, errors


def load_enabled(profiles_dir: Path = PROFILES_DIR) -> tuple[list[Profile], list[str]]:
    """Like load_all, but filters out enabled=False (what /remove sets)."""
    profiles, errors = load_all(profiles_dir)
    return [p for p in profiles if p.enabled], errors
