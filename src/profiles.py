# -*- coding: utf-8 -*-
"""
Loading and validation of profiles/<slug>.json - the profile is the only
registration a company has in this project. There is no separate
companies.json. See the career-site-profiler skill,
references/profile_schema.md, for the full schema these files must satisfy.
"""

import json
from pathlib import Path
from dataclasses import dataclass, field

SUPPORTED_SCHEMA_VERSION = 2
PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"


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


def _validate(data: dict, path: Path) -> None:
    """Checks the fields required by schema v2. Doesn't silently patch
    values - a profile with a missing field fails loudly, at load time,
    not silently in the middle of a fetch."""
    where = f"{path.name}: "

    version = data.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise ProfileError(
            f"{where}schema_version={version!r}, expected {SUPPORTED_SCHEMA_VERSION}. "
            "Profile is on an old/unknown schema - fix it or re-run the skill.")

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
