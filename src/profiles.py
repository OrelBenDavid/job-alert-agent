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

# *** Platform profiles and thin company records ***
#
# Three kinds of file live under profiles/, and the difference is only about
# how much of a document is written down, never about how it behaves:
#
#   profiles/*.json              a full standalone profile. What every company
#                                was before this existed, and still the right
#                                answer for a company that doesn't fit a
#                                platform's shape (wix).
#   profiles/_platforms/*.json   a PARTIAL document holding everything that is
#                                identical for every customer of one ATS.
#   profiles/companies/*.json    a thin record naming a platform plus the few
#                                fields that genuinely differ per company.
#
# A company record is resolved by merging it OVER its platform profile, and the
# result is then validated exactly like a standalone profile - same schema,
# same errors, same required fields. Nothing downstream can tell the two apart,
# which is the point: this is config resolution, not a behaviour registry.
#
# In particular the `platform` key selects a FILE and nothing else. The fetch
# dispatch still reads `fetch_type` and `api.platform` off the resolved
# document (see fetchers/__init__.py and fetchers/api.py) - there is
# deliberately no platform-name -> function map anywhere in the resolution
# path, so a company can override any resolved field, including api.platform,
# and the dispatcher follows it.
PLATFORMS_DIR = PROFILES_DIR / "_platforms"
COMPANIES_DIR = PROFILES_DIR / "companies"

# The four ways a description can be reached, cheapest first - the same
# cheap-to-expensive rule the fetch_type dispatch follows.
DETAIL_METHODS = ("inline", "html", "embedded_json", "json", "playwright",
                  "none")

# The ATS platforms that actually have a handler in fetchers/api.py. Kept here
# as a literal rather than imported from there, because importing the fetchers
# package pulls in playwright, which a profile-validation pass has no reason
# to require. tests/test_profiles_schema.py asserts the two stay in sync.
#
# Validating this at LOAD time rather than at fetch time is the point: the
# skill's platform table lists more platforms (recruitee among them) than this
# project implements, so a profile naming one of them used to validate cleanly
# and then fail once per run, per company, as a fetch error - which needs two
# consecutive failures before it says anything.
IMPLEMENTED_API_PLATFORMS = ("lever", "greenhouse", "comeet", "smartrecruiters",
                             "ashby", "hibob", "workday", "workable")

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

    if method == "json":
        # json_path is this method's content_selector: without it the walk
        # ends at the whole document, which is never a description. A JSON
        # response has no markup, so content_selector must NOT be required
        # here - which is why this is a branch of the same chain rather than a
        # check before it.
        if not block.get("json_path"):
            raise ProfileError(
                f"{where}detail_fetch.method='json' requires json_path - the "
                "dotted path to the description inside the JSON response "
                "(e.g. 'jobPostingInfo.jobDescription').")

    elif method == "embedded_json":
        # The JSON path is this method's equivalent of content_selector:
        # without it the extractor walks to None on every posting and the
        # company reads as undetermined while the profile claims otherwise.
        if not block.get("embedded_json_path"):
            raise ProfileError(
                f"{where}detail_fetch.method='embedded_json' requires "
                "embedded_json_path - the dotted path to the description "
                "inside the embedded object.")
    # html / playwright - both issue one request per new posting
    elif not block.get("content_selector"):
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


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ProfileError(f"{path.name}: invalid JSON - {e}") from e
    except OSError as e:
        raise ProfileError(f"{path.name}: could not be read - {e}") from e


def _deep_merge(base: dict, override: dict) -> dict:
    """`override` merged over `base`, recursing into nested objects.

    The recursion is the whole reason this isn't dict.update(). A company needs
    to set `api.endpoint` without restating `api.fields` - a flat merge would
    replace the entire `api` block and drop the field mapping, which is the one
    thing the platform profile exists to supply. That failure would not be
    loud, either: `fields` is required by _validate, so it would surface as a
    schema error on all 142 companies at once, and the temptation would be to
    paste the field map back into every record.

    Lists and scalars REPLACE rather than merge. A half-overridden list (say,
    two of a platform's three checked_fields) is never what an author means,
    and merging them would make it impossible to shorten one.
    """
    merged = dict(base)
    for key, value in override.items():
        if (key in merged and isinstance(merged[key], dict)
                and isinstance(value, dict)):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _validate_platform(data: dict, path: Path) -> None:
    """Checks a platform profile's own contribution.

    Deliberately its own pass rather than leaving it to the merged validation.
    A typo in a platform file is inherited by every company on that platform,
    so it would otherwise surface as the same confusing error a hundred times
    over with a hundred company filenames attached and no mention of the file
    that actually caused it."""
    where = f"_platforms/{path.name}: "
    if data.get("fetch_type") is None:
        raise ProfileError(f"{where}missing 'fetch_type'")
    if data["fetch_type"] not in ("api", "html", "playwright"):
        raise ProfileError(f"{where}unknown fetch_type: {data['fetch_type']!r}")
    # A platform profile must NOT carry per-company identity. Catching it here
    # rather than letting it merge through is what stops one stray `slug` in a
    # platform file from silently renaming every company that inherits it.
    for forbidden in ("slug", "name", "careers_url"):
        if forbidden in data:
            raise ProfileError(
                f"{where}must not define {forbidden!r} - that is a per-company "
                "field, and a value here would be inherited by every company "
                "on this platform.")


def load_platform(name: str) -> dict:
    """Loads one platform profile by name. Raises with the list of real ones
    if it doesn't exist - a typo'd platform name is otherwise a KeyError from
    somewhere unhelpful."""
    path = PLATFORMS_DIR / f"{name}.json"
    if not path.exists():
        available = sorted(p.stem for p in PLATFORMS_DIR.glob("*.json")) \
            if PLATFORMS_DIR.exists() else []
        raise ProfileError(
            f"unknown platform {name!r} - no profiles/_platforms/{name}.json. "
            f"Available: {available}")
    data = _read_json(path)
    _validate_platform(data, path)
    return data


def resolve_profile(data: dict, path: Path) -> dict:
    """A profile document as the rest of the project should see it.

    A document with no `platform` key is already complete and passes straight
    through untouched - which is what keeps every standalone profile, wix
    included, resolving to exactly the bytes on disk."""
    platform = data.get("platform")
    if not platform:
        return data
    resolved = _deep_merge(load_platform(platform), data)
    # `platform` has done its job selecting the file. It is left in the
    # resolved document on purpose - the importer and any future tooling want
    # to know which platform a company came from, and _validate ignores keys it
    # doesn't recognise.
    return resolved


def load_profile(path: Path) -> Profile:
    """Loads, resolves and parses a single profile. Raises ProfileError with
    details if something's invalid.

    Validation runs on the RESOLVED document, so a thin company record and a
    standalone profile are held to identical standards - a company that
    inherits a broken endpoint fails exactly as loudly as one that spells it
    out."""
    data = resolve_profile(_read_json(path), path)

    _validate(data, path)

    return Profile(
        slug=data["slug"], name=data["name"], enabled=data["enabled"],
        careers_url=data["careers_url"], fetch_type=data["fetch_type"],
        israel_filter=data["israel_filter"], health=data["health"], raw=data,
    )


def profile_paths(profiles_dir: Path = PROFILES_DIR) -> list[Path]:
    """Every file that is a company, in a stable order.

    Two directories, and the exclusion is the load-bearing part: `_platforms/`
    is NOT included. Platform files are partial documents with no slug and no
    careers_url, so loading one as a company would fail validation - which
    means the underscore prefix is a convention, not the mechanism. The
    mechanism is that this function globs two named directories rather than
    walking the tree, so a new subdirectory under profiles/ can never start
    being picked up as companies by accident.
    """
    paths = sorted(profiles_dir.glob("*.json"))
    companies = profiles_dir / "companies"
    if companies.is_dir():
        paths += sorted(companies.glob("*.json"))
    return paths


def find_profile_path(slug: str, profiles_dir: Path = PROFILES_DIR) -> Path | None:
    """The file defining one company, or None. Root first, then companies/.

    Every caller that used to build `PROFILES_DIR / f"{slug}.json"` by hand
    must go through this instead. Once a company can live in either directory,
    that hand-built path silently misses the ones under companies/ - and the
    Telegram commands failed exactly that way: /remove and /jobs answered
    'no such profile' for a company that was being monitored perfectly well,
    and /list showed only whatever was left at the root."""
    for candidate in (profiles_dir / f"{slug}.json",
                      profiles_dir / "companies" / f"{slug}.json"):
        if candidate.exists():
            return candidate
    return None


def load_all(profiles_dir: Path = PROFILES_DIR) -> tuple[list[Profile], list[str]]:
    """Loads every profile, standalone and platform-backed alike. A profile
    that fails validation is collected as a single error message with its
    filename - it does not stop the loading of the other profiles, so one
    broken company doesn't take down the whole run.

    A duplicate slug across the two directories is reported rather than
    silently resolved. At three companies that could not happen; at 142
    imported ones, a company added by hand at the root and again by the
    importer under companies/ would otherwise mean two profiles sharing a
    single state file, each overwriting the other's ids every run - a
    permanent, silent alert loss for both."""
    profiles, errors = [], []
    seen: dict[str, Path] = {}
    for path in profile_paths(profiles_dir):
        try:
            profile = load_profile(path)
        except ProfileError as e:
            errors.append(str(e))
            continue
        if profile.slug in seen:
            errors.append(
                f"{path.name}: duplicate slug {profile.slug!r}, already defined "
                f"by {seen[profile.slug].name}. Both would share "
                f"state/seen/{profile.slug}.json and overwrite each other every "
                "run. Skipped this one; delete or rename one of the two.")
            continue
        seen[profile.slug] = path
        profiles.append(profile)
    return profiles, errors


def load_enabled(profiles_dir: Path = PROFILES_DIR) -> tuple[list[Profile], list[str]]:
    """Like load_all, but filters out enabled=False (what /remove sets)."""
    profiles, errors = load_all(profiles_dir)
    return [p for p in profiles if p.enabled], errors
