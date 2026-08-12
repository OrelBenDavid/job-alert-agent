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

# The ATS platforms that actually have a handler in fetchers/api.py. Kept here
# as a literal rather than imported from there, because importing the fetchers
# package pulls in playwright, which a profile-validation pass has no reason
# to require. tests/test_profiles_schema.py asserts the two stay in sync.
#
# Validating this at LOAD time rather than at fetch time is the point: the
# skill's platform table lists four more platforms (ashby, workable,
# recruitee, workday) than this project implements, so a profile naming one of
# them used to validate cleanly and then fail once per run, per company, as a
# fetch error - which needs two consecutive failures before it says anything.
IMPLEMENTED_API_PLATFORMS = ("lever", "greenhouse", "comeet", "smartrecruiters")

# The step verbs fetchers/browser.py's _apply_relevance_filter_actions can
# replay. An unknown verb raises there, mid-fetch; catching it here means a
# typo fails at load with the filename attached.
UI_ACTIONS = ("click", "fill", "press", "wait")


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

    # Mandatory for every method except "none", per the skill's schema: a
    # block without the posting URL it was confirmed against is a guess. That
    # matters more here than it looks - a guessed block doesn't fail loudly at
    # runtime, it quietly reads nothing, and every posting from the company
    # comes back "undetermined" while the profile claims to be filtering.
    # Omitting detail_fetch entirely is the correct output when unverified.
    if not block.get("verified_on_job_url"):
        raise ProfileError(
            f"{where}detail_fetch.method={method!r} requires "
            "verified_on_job_url - the real posting URL the field or selector "
            "was confirmed against. If it wasn't verified, omit the whole "
            "detail_fetch block instead (the filter is fail-open).")

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


def _validate_israel_filter(data: dict, where: str) -> None:
    """Validates the parts of `israel_filter` that the fetchers actually read.

    *** Why this is worth failing a load over ***

    Both checks here cover the same class of bug, and it is the nastiest one
    this project has: a profile that looks complete, runs without raising, and
    quietly returns the wrong set of jobs.

    `ui_actions_structured` is the machine-readable step list
    browser._apply_relevance_filter_actions replays. It is easy to author a
    `ui_interaction` profile with only the prose `ui_actions` field, in which
    case the loop finds nothing to replay and applies NO location filter at
    all. Nothing raises: the post_fetch relevance check still keeps the
    results correct, but pagination is now walking the company's ENTIRE
    unfiltered listing, and `max_pages` truncates it - so Israeli postings
    past the cap are never seen, the count stays healthy, and the health gate
    has nothing to notice. Silent loss, which is the one thing this project
    exists to prevent.

    `flat_multi_location` has a blunter version of the same problem: the
    strategy needs two selectors that live in the `playwright` block, and
    without them it raises KeyError mid-fetch (recorded in wix.json's notes as
    an actually-observed failure) rather than at load.
    """
    israel_filter = data.get("israel_filter")
    if not isinstance(israel_filter, dict):
        raise ProfileError(f"{where}israel_filter must be an object")

    method = israel_filter.get("method")
    if method not in ("url_param", "ui_interaction", "post_fetch"):
        raise ProfileError(
            f"{where}israel_filter.method invalid: {method!r}. Expected "
            "'url_param', 'ui_interaction' or 'post_fetch'.")

    if method == "url_param" and not israel_filter.get("param"):
        raise ProfileError(
            f"{where}israel_filter.method='url_param' but param is missing - "
            "there is no way to know which query parameter carries the filter.")

    if method == "ui_interaction":
        steps = israel_filter.get("ui_actions_structured")
        if not steps:
            raise ProfileError(
                f"{where}israel_filter.method='ui_interaction' requires a "
                "non-empty ui_actions_structured list. The prose `ui_actions` "
                "field is documentation; ui_actions_structured is what gets "
                "replayed. Without it no filter is applied at runtime and the "
                "unfiltered listing is paginated instead - silently, and with "
                "max_pages truncating the results.")
        if not isinstance(steps, list):
            raise ProfileError(
                f"{where}israel_filter.ui_actions_structured must be a list")
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ProfileError(
                    f"{where}ui_actions_structured[{index}] must be an object")
            action = step.get("action")
            if action not in UI_ACTIONS:
                raise ProfileError(
                    f"{where}ui_actions_structured[{index}].action invalid: "
                    f"{action!r}. Expected one of {UI_ACTIONS}.")
            if action == "click" and not step.get("selector"):
                raise ProfileError(
                    f"{where}ui_actions_structured[{index}] action='click' "
                    "needs a selector")
            if action == "fill" and not (step.get("selector") and
                                         step.get("value") is not None):
                raise ProfileError(
                    f"{where}ui_actions_structured[{index}] action='fill' "
                    "needs both selector and value")
            if action in ("press", "wait") and step.get("value") is None:
                raise ProfileError(
                    f"{where}ui_actions_structured[{index}] action={action!r} "
                    "needs a value")

    if (israel_filter.get("structure") == "flat_multi_location"
            and data.get("fetch_type") == "playwright"):
        block = data.get("playwright") or {}
        for field_name in ("location_filter_selector", "location_option_selector"):
            if not block.get(field_name):
                raise ProfileError(
                    f"{where}israel_filter.structure='flat_multi_location' "
                    f"requires playwright.{field_name} - the multi-location "
                    "walk opens the picker and reads its options, and without "
                    "this it raises mid-fetch instead of at load.")
        if not israel_filter.get("param"):
            raise ProfileError(
                f"{where}israel_filter.structure='flat_multi_location' "
                "requires israel_filter.param - the per-city query parameter.")


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

    _validate_israel_filter(data, where)

    block = data[fetch_type]
    if fetch_type == "api":
        platform = block.get("platform")
        if not platform:
            raise ProfileError(f"{where}api.platform missing")
        if platform not in IMPLEMENTED_API_PLATFORMS:
            raise ProfileError(
                f"{where}api.platform={platform!r} has no handler in "
                f"fetchers/api.py. Implemented: {IMPLEMENTED_API_PLATFORMS}. "
                "Adding one is a dispatcher change (and, per the skill, needs "
                "a live verification first) - not something a profile can "
                "declare on its own.")
        if not block.get("endpoint"):
            raise ProfileError(f"{where}api.endpoint missing")
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
