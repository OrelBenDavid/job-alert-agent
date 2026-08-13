# -*- coding: utf-8 -*-
"""
Tests for platform profiles and thin company records.

The mechanism under test is config resolution: a company record is merged OVER
its platform profile and the result is validated as one document. The risk it
introduces is specific and worth naming - a company can now be WRONG because of
a file it doesn't mention, and a resolution bug is silent by nature. It doesn't
raise; it produces a profile that loads cleanly and fetches the wrong thing.
So these tests pin the resolved values, not just the fact that resolution ran.
"""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from profiles import (COMPANIES_DIR, PLATFORMS_DIR, PROFILES_DIR, ProfileError,
                      _deep_merge, find_profile_path, load_all, load_platform,
                      load_profile, profile_paths)

PLATFORMS = ("comeet", "greenhouse", "lever", "ashby", "hibob")


# ---------------------------------------------------------------------------
# The shipped platform profiles
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", PLATFORMS)
def test_every_shipped_platform_profile_loads(name):
    assert load_platform(name)["fetch_type"] == "api"


@pytest.mark.parametrize("name", PLATFORMS)
def test_a_platform_profile_declares_an_implemented_platform(name):
    """A platform profile naming a platform with no handler would fail at load
    for every company that inherits it, all at once."""
    from profiles import IMPLEMENTED_API_PLATFORMS
    assert load_platform(name)["api"]["platform"] in IMPLEMENTED_API_PLATFORMS


@pytest.mark.parametrize("name", PLATFORMS)
def test_a_platform_profile_carries_no_per_company_identity(name):
    """slug/name/careers_url in a platform file would be inherited by every
    company on it - so one stray value silently renames a hundred companies."""
    data = load_platform(name)
    assert not ({"slug", "name", "careers_url"} & set(data))


@pytest.mark.parametrize("name", PLATFORMS)
def test_a_platform_profile_supplies_the_field_map(name):
    """The field map is the main thing a platform profile exists to supply.
    If it ever moved into the company records, the bulk import would be
    restating it 142 times and the platform file would be decorative."""
    fields = load_platform(name)["api"]["fields"]
    for required in ("id", "title", "location", "url"):
        assert required in fields


def test_an_unknown_platform_name_names_the_real_ones():
    with pytest.raises(ProfileError, match="unknown platform"):
        load_platform("workday")


def test_a_platform_profile_defining_a_slug_is_rejected(tmp_path, monkeypatch):
    import profiles
    monkeypatch.setattr(profiles, "PLATFORMS_DIR", tmp_path)
    (tmp_path / "bogus.json").write_text(
        json.dumps({"fetch_type": "api", "slug": "oops"}), encoding="utf-8")
    with pytest.raises(ProfileError, match="per-company field"):
        profiles.load_platform("bogus")


# ---------------------------------------------------------------------------
# Merge semantics
# ---------------------------------------------------------------------------

def test_merge_recurses_into_nested_objects():
    """The reason this isn't dict.update(): a company overriding api.endpoint
    must keep the platform's api.fields. A flat merge drops the field map."""
    base = {"api": {"platform": "lever", "fields": {"id": "id"},
                    "endpoint": "PLATFORM"}}
    merged = _deep_merge(base, {"api": {"endpoint": "COMPANY"}})
    assert merged["api"]["endpoint"] == "COMPANY"
    assert merged["api"]["fields"] == {"id": "id"}      # survived
    assert merged["api"]["platform"] == "lever"          # survived


def test_merge_replaces_lists_rather_than_concatenating():
    """A half-overridden list is never what an author means, and merging would
    make it impossible to SHORTEN one."""
    merged = _deep_merge({"f": {"checked": ["a", "b", "c"]}},
                         {"f": {"checked": ["a"]}})
    assert merged["f"]["checked"] == ["a"]


def test_merge_does_not_mutate_either_input():
    base = {"api": {"fields": {"id": "id"}}}
    override = {"api": {"endpoint": "x"}}
    _deep_merge(base, override)
    assert base == {"api": {"fields": {"id": "id"}}}
    assert override == {"api": {"endpoint": "x"}}


def test_a_company_can_override_a_scalar_inherited_from_its_platform(tmp_path):
    """Including api.platform itself. The `platform` key selects a file; it
    does not lock behaviour, and nothing downstream dispatches on it."""
    import profiles
    data = {"slug": "acme", "name": "Acme", "platform": "lever",
            "careers_url": "https://jobs.lever.co/acme",
            "api": {"endpoint": "https://api.lever.co/v0/postings/acme?mode=json"},
            "health": {"expected_min_jobs": 3}, "verified_on": "2026-08-13"}
    resolved = profiles.resolve_profile(data, tmp_path / "acme.json")
    assert resolved["fetch_type"] == "api"              # from the platform
    assert resolved["api"]["fields"]["title"] == "text"  # from the platform
    assert resolved["health"]["expected_min_jobs"] == 3  # from the company
    # zero_is_plausible came from the platform, untouched by the company
    assert resolved["health"]["zero_is_plausible"] is False


def test_a_document_without_a_platform_key_passes_through_untouched(tmp_path):
    """What keeps every standalone profile - wix included - resolving to
    exactly the bytes on disk."""
    import profiles
    data = {"slug": "solo", "fetch_type": "playwright", "anything": 1}
    assert profiles.resolve_profile(data, tmp_path / "solo.json") == data


# ---------------------------------------------------------------------------
# The migrated companies - the values the fetchers actually read
# ---------------------------------------------------------------------------

def test_mobileye_resolves_to_the_eu_host():
    """The single most breakable fact in the migration: api.lever.co (non-EU)
    404s for this slug, and mobileye is the only EU-hosted Lever account of
    the eight. If the company override were ever dropped, this company would
    fetch nothing and look like an ordinary outage."""
    profile = load_profile(find_profile_path("mobileye"))
    assert profile.raw["api"]["endpoint"] == \
        "https://api.eu.lever.co/v0/postings/mobileye?mode=json"


def test_mobileye_inherits_the_lever_field_map_and_detail_fetch():
    profile = load_profile(find_profile_path("mobileye"))
    assert profile.raw["api"]["fields"] == {
        "id": "id", "title": "text", "location": "categories.location",
        "url": "hostedUrl"}
    assert profile.detail_fetch["inline_field"] == "lists"
    assert profile.detail_fetch["inline_section_heading"] == "text"


def test_mobileye_keeps_its_own_health_numbers():
    profile = load_profile(find_profile_path("mobileye"))
    assert profile.expected_min_jobs == 20
    assert profile.zero_is_plausible is False


def test_wiz_keeps_zero_is_plausible_true():
    """wiz genuinely has no Israeli openings. If the platform default (False)
    ever won this merge, wiz would fire a false maintenance alert every run."""
    profile = load_profile(find_profile_path("wiz"))
    assert profile.zero_is_plausible is True
    assert profile.expected_min_jobs == 0


def test_wiz_inherits_greenhouse_content_true_endpoint_and_offices_check():
    profile = load_profile(find_profile_path("wiz"))
    assert profile.raw["api"]["endpoint"].endswith("?content=true")
    assert "offices" in profile.israel_filter["checked_fields"]


# ---------------------------------------------------------------------------
# wix must not be touched
# ---------------------------------------------------------------------------

def test_wix_is_still_a_standalone_playwright_profile():
    """Explicitly out of scope for the platform migration."""
    path = PROFILES_DIR / "wix.json"
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "platform" not in raw          # not platform-backed
    assert raw["fetch_type"] == "playwright"
    assert load_profile(path).fetch_type == "playwright"


# ---------------------------------------------------------------------------
# Discovery and duplicate detection
# ---------------------------------------------------------------------------

def test_platform_files_are_not_loaded_as_companies():
    """profiles/_platforms/*.json are partial documents. If they were picked
    up as companies they'd fail validation, once per platform, every run.

    Asserted on the PATH rather than the filename: a company may legitimately
    share its name with its platform (hibob is both, since the company runs
    its own hiring product), so a filename check would report a collision that
    isn't one - and would have to be weakened to pass, losing the real
    guarantee. What actually matters is that nothing under _platforms/ is ever
    returned as a company."""
    for path in profile_paths():
        assert path.parent != PLATFORMS_DIR, path
    assert not any(p.parent.name == "_platforms" for p in profile_paths())


def test_load_all_finds_both_standalone_and_platform_backed_companies():
    profiles_, errors = load_all()
    assert errors == [], errors
    slugs = {p.slug for p in profiles_}
    assert {"wix", "mobileye", "wiz"} <= slugs


def test_a_duplicate_slug_across_directories_is_reported_not_silently_merged(tmp_path):
    """Two files claiming one slug would share a single state/seen/<slug>.json
    and overwrite each other's ids every run - a permanent silent alert loss
    for both companies, and invisible in the logs."""
    (tmp_path / "companies").mkdir()
    doc = {"schema_version": 3, "slug": "dup", "name": "Dup", "enabled": True,
           "careers_url": "https://jobs.lever.co/dup", "fetch_type": "api",
           "israel_filter": {"method": "post_fetch"},
           "api": {"platform": "lever",
                   "endpoint": "https://api.lever.co/v0/postings/dup?mode=json",
                   "fields": {"id": "id", "title": "text",
                              "location": "categories.location",
                              "url": "hostedUrl"}},
           "health": {"expected_min_jobs": 1}, "verified_on": "2026-08-13"}
    (tmp_path / "dup.json").write_text(json.dumps(doc), encoding="utf-8")
    (tmp_path / "companies" / "dup.json").write_text(json.dumps(doc),
                                                     encoding="utf-8")

    profiles_, errors = load_all(tmp_path)
    assert len(profiles_) == 1                      # loaded once, not twice
    assert any("duplicate slug" in e for e in errors), errors


# ---------------------------------------------------------------------------
# The imported corpus
#
# 139 of these files are generated, so a defect in the generator is a defect in
# all of them at once. These check properties of the whole set rather than of
# any one company.
# ---------------------------------------------------------------------------

def test_the_whole_corpus_loads_with_no_errors():
    """The single most valuable assertion here. A profile that fails
    validation is skipped at load with a logged error - so a broken generator
    would silently shrink the monitored set rather than fail anything."""
    profiles_, errors = load_all()
    assert errors == [], errors
    assert len(profiles_) >= 140, len(profiles_)


def test_every_imported_company_names_a_platform_that_exists():
    for profile in load_all()[0]:
        platform = profile.raw.get("platform")
        if platform:
            assert (PLATFORMS_DIR / f"{platform}.json").exists(), platform


def test_no_comeet_endpoint_uses_the_uid_as_its_token():
    """The exact defect the handed-over shortlist shipped with. It fails
    CLOSED - HTTP 400, which reads as a dead company - so a regression here
    would quietly drop 104 companies rather than break loudly."""
    for profile in load_all()[0]:
        if profile.raw.get("platform") != "comeet":
            continue
        endpoint = profile.raw["api"]["endpoint"]
        uid = endpoint.split("/company/")[1].split("/")[0]
        token = endpoint.split("token=")[1]
        assert token != uid, f"{profile.slug}: token == uid"
        assert len(token) >= 12, f"{profile.slug}: implausible token {token!r}"


def test_every_company_with_a_zero_floor_explains_itself():
    """zero_is_plausible=true switches OFF the total-zero health gate, which
    is this project's main defence. It must never be set by default - only
    where a live check found no Israeli postings."""
    for profile in load_all()[0]:
        if profile.zero_is_plausible:
            assert profile.health.get("_note"), profile.slug


def test_health_floors_stay_below_what_was_observed():
    """A floor at or above the observed count fires a maintenance alert on the
    very first run - the company looks broken from the moment it is added.

    The zero case is the other half of the same rule and is why this isn't a
    plain `<`: a company observed with no Israeli postings must carry floor 0
    AND zero_is_plausible, or the total-zero gate trips on a board that is
    working exactly as observed."""
    for profile in load_all()[0]:
        note = profile.health.get("_note", "")
        match = re.search(r"(\d+) Israel-relevant postings", note)
        if not match:
            continue
        observed = int(match.group(1))
        if observed == 0:
            assert profile.expected_min_jobs == 0, profile.slug
            assert profile.zero_is_plausible is True, profile.slug
        else:
            assert profile.expected_min_jobs < observed, profile.slug


def test_biocatch_is_the_comeet_board_not_the_abandoned_lever_one():
    """BioCatch had two live boards. The Lever one had not seen a new posting
    in nearly six months; importing it would have merged dead postings into
    the company permanently."""
    profile = load_profile(find_profile_path("biocatch"))
    assert profile.raw["platform"] == "comeet"
    assert "comeet.co" in profile.raw["api"]["endpoint"]


def test_no_unresolved_dead_row_from_phase_one_was_imported():
    """Of the ten identifiers that 404'd in Phase 1, three were later
    re-resolved to a working board on a different platform and re-added
    (hibob, viz_ai, insightec - see their `resolved_from`). The other seven
    moved to platforms with no handler here, or self-host. Importing one of
    those would mean a company that fails every run forever."""
    slugs = {p.slug for p in load_all()[0]}
    for dead in ("digital_turbine", "massivit_3d_printing_technologies",
                 "cyberbit", "cyberproof", "deep_instinct", "neogames",
                 "ree_automotive"):
        assert dead not in slugs, dead


@pytest.mark.parametrize("slug,platform", [
    ("hibob", "hibob"), ("viz_ai", "ashby"), ("insightec", "comeet")])
def test_the_re_resolved_companies_landed_on_the_right_platform(slug, platform):
    """All three were on a DIFFERENT platform in the shortlist than they
    actually use - the identifiers weren't just stale, the platform was wrong.
    viz_ai and insightec were both listed as Greenhouse."""
    profile = load_profile(find_profile_path(slug))
    assert profile.raw["platform"] == platform
    assert "Re-resolved on 2026-08-13" in profile.raw["resolved_from"]


def test_find_profile_path_locates_a_company_in_either_directory():
    assert find_profile_path("wix").parent == PROFILES_DIR
    assert find_profile_path("mobileye").parent == COMPANIES_DIR
    assert find_profile_path("no-such-company") is None
