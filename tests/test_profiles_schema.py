# -*- coding: utf-8 -*-
"""
Tests for the v3 schema bump: detail_fetch is optional, v2 profiles keep
loading untouched, and a half-specified block fails loudly at load time
rather than turning into a silent stream of per-job errors on every run.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from profiles import (CURRENT_SCHEMA_VERSION, ProfileError,
                      find_profile_path, load_profile, profile_paths)

_BASE = {
    "schema_version": 3,
    "slug": "acme",
    "name": "Acme",
    "enabled": True,
    "careers_url": "https://jobs.lever.co/acme",
    "fetch_type": "api",
    "israel_filter": {"method": "post_fetch"},
    "api": {"platform": "lever",
            "endpoint": "https://api.lever.co/v0/postings/acme?mode=json",
            "fields": {"id": "id", "title": "text",
                       "location": "categories.location", "url": "hostedUrl"}},
    "health": {"expected_min_jobs": 5},
    "verified_on": "2026-08-12",
}


def _write(tmp_path, **overrides):
    data = dict(_BASE)
    data.update(overrides)
    path = tmp_path / f"{data['slug']}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Backwards compatibility - the existing profiles must not need touching
# ---------------------------------------------------------------------------

def test_every_shipped_profile_still_loads():
    """Covers standalone profiles AND platform-backed company records.
    Globbing the root alone stopped being sufficient once companies could
    live under companies/ - it would have passed while every migrated
    company went unchecked."""
    paths = profile_paths()
    assert len(paths) >= 3, paths      # guards the glob itself silently
                                        # matching nothing
    for path in paths:
        load_profile(path)          # raises ProfileError if it doesn't


def test_a_v2_profile_reads_as_having_no_detail_fetch(tmp_path):
    profile = load_profile(_write(tmp_path, schema_version=2))
    assert profile.detail_fetch is None


def test_a_v3_profile_without_the_block_is_valid(tmp_path):
    assert load_profile(_write(tmp_path)).detail_fetch is None


def test_an_unknown_schema_version_is_still_rejected(tmp_path):
    with pytest.raises(ProfileError, match="schema_version"):
        load_profile(_write(tmp_path, schema_version=4))


def test_a_v2_profile_carrying_a_v3_block_fails_loudly(tmp_path):
    """Ignoring it silently would leave the author convinced the filter is
    working for this company when every job reads as undetermined."""
    with pytest.raises(ProfileError, match="schema_version"):
        load_profile(_write(tmp_path, schema_version=2,
                            detail_fetch={"method": "inline",
                                          "inline_field": "content"}))


# ---------------------------------------------------------------------------
# detail_fetch validation
# ---------------------------------------------------------------------------

def test_method_none_reads_as_no_detail_fetch(tmp_path):
    profile = load_profile(_write(tmp_path, detail_fetch={"method": "none"}))
    assert profile.detail_fetch is None


def test_inline_block_is_accepted_and_exposed(tmp_path):
    profile = load_profile(_write(tmp_path, detail_fetch={
        "method": "inline", "inline_field": "content", "content_is_html": True,
        "verified_on_job_url": "https://boards.greenhouse.io/acme/jobs/1"}))
    assert profile.detail_fetch["inline_field"] == "content"


def test_html_block_is_accepted(tmp_path):
    profile = load_profile(_write(tmp_path, detail_fetch={
        "method": "html", "url_source": "job_url",
        "content_selector": "#job-description",
        "verified_on_job_url": "https://jobs.lever.co/acme/abc"}))
    assert profile.detail_fetch["method"] == "html"


_VERIFIED = {"verified_on_job_url": "https://jobs.lever.co/acme/abc"}


@pytest.mark.parametrize("block,message", [
    ({"method": "carrier-pigeon"}, "method invalid"),
    (dict(_VERIFIED, method="inline"), "inline_field"),
    (dict(_VERIFIED, method="html"), "content_selector"),
    (dict(_VERIFIED, method="playwright"), "content_selector"),
    (dict(_VERIFIED, method="html", content_selector="#d", url_source="magic"),
     "url_source invalid"),
    (dict(_VERIFIED, method="html", content_selector="#d", url_source="template"),
     "url_template"),
])
def test_malformed_blocks_fail_at_load_time(tmp_path, block, message):
    with pytest.raises(ProfileError, match=message):
        load_profile(_write(tmp_path, detail_fetch=block))


@pytest.mark.parametrize("block", [
    {"method": "inline", "inline_field": "content"},
    {"method": "html", "content_selector": "#job-description"},
    {"method": "playwright", "content_selector": "#job-description"},
])
def test_an_unverified_block_is_rejected(tmp_path, block):
    """A detail_fetch block without the posting URL it was confirmed against
    is a guess. It fails LOUDLY at load time rather than quietly reading
    nothing at runtime and tagging every posting undetermined while the
    profile claims to be filtering. Omitting the block is the correct output
    when it couldn't be verified - the filter is fail-open."""
    with pytest.raises(ProfileError, match="verified_on_job_url"):
        load_profile(_write(tmp_path, detail_fetch=block))


def test_method_none_needs_no_verification_url(tmp_path):
    """"Investigated, nothing reachable" is a finding, not a guess."""
    profile = load_profile(_write(tmp_path, detail_fetch={"method": "none"}))
    assert profile.detail_fetch is None


def test_both_shipped_detail_fetch_blocks_are_verified():
    """Guards the two profiles actually in production."""
    for slug in ("mobileye", "wiz"):
        cfg = load_profile(find_profile_path(slug)).detail_fetch
        assert cfg["verified_on_job_url"].startswith("https://")


def test_current_schema_version_is_three():
    assert CURRENT_SCHEMA_VERSION == 3


# ---------------------------------------------------------------------------
# The fields the fetchers actually read
#
# Everything below guards the same class of bug, and it is the nastiest one
# this project has: a profile that looks complete, loads, runs without raising,
# and quietly returns the wrong set of jobs. Catching these at load time is the
# difference between a loud "fix your profile" and a company that appears
# healthy while silently under-reporting.
# ---------------------------------------------------------------------------

def test_the_implemented_platform_list_matches_the_dispatcher():
    """profiles.py keeps its own copy so validation doesn't have to import
    playwright. This is what stops the two drifting."""
    from fetchers.api import _PLATFORM_DISPATCH
    from profiles import IMPLEMENTED_API_PLATFORMS
    assert set(IMPLEMENTED_API_PLATFORMS) == set(_PLATFORM_DISPATCH)


def test_a_platform_with_no_handler_is_rejected_at_load(tmp_path):
    """A platform in the skill's table with no handler here used to load
    cleanly and then fail once per run as an ordinary fetch error - which says
    nothing until it has failed twice.

    The example was `ashby` until 2026-08-13, when Ashby was live-verified and
    given a real handler. Only the example changed; what is being asserted did
    not. Workable is the replacement for the same reason Ashby was the
    original: it is in the skill's platform table and has no handler."""
    api = dict(_BASE["api"], platform="workable")
    with pytest.raises(ProfileError, match="no handler"):
        load_profile(_write(tmp_path, api=api))


def test_an_api_profile_without_an_endpoint_is_rejected(tmp_path):
    api = {k: v for k, v in _BASE["api"].items() if k != "endpoint"}
    with pytest.raises(ProfileError, match="endpoint"):
        load_profile(_write(tmp_path, api=api))


def test_ui_interaction_without_structured_actions_is_rejected(tmp_path):
    """The skill documents `ui_actions` (prose) and the fetcher replays
    `ui_actions_structured`. A profile carrying only the first applies NO
    location filter at runtime - nothing raises, the post_fetch check keeps
    the results correct, but pagination now walks the company's ENTIRE
    unfiltered listing and max_pages truncates it. Israeli postings past the
    cap are never seen, and the count stays healthy enough that the health
    gate notices nothing."""
    with pytest.raises(ProfileError, match="ui_actions_structured"):
        load_profile(_write(tmp_path, israel_filter={
            "method": "ui_interaction",
            "ui_actions": "click the location box, type Israel, press Enter",
        }))


def test_a_valid_structured_action_list_is_accepted(tmp_path):
    profile = load_profile(_write(tmp_path, israel_filter={
        "method": "ui_interaction",
        "ui_actions_structured": [
            {"action": "click", "selector": "#loc"},
            {"action": "fill", "selector": "#loc-input", "value": "Israel"},
            {"action": "press", "value": "Enter"},
            {"action": "wait", "value": 1500},
        ],
    }))
    assert profile.israel_filter["method"] == "ui_interaction"


@pytest.mark.parametrize("step", [
    {"action": "scroll", "selector": "#x"},        # not a verb the fetcher has
    {"action": "click"},                            # click with no selector
    {"action": "fill", "selector": "#x"},           # fill with no value
    {"action": "press"},                            # press with no key
])
def test_a_malformed_action_step_is_rejected(tmp_path, step):
    """These used to raise from inside the fetch, per company, per run."""
    with pytest.raises(ProfileError, match="ui_actions_structured"):
        load_profile(_write(tmp_path, israel_filter={
            "method": "ui_interaction", "ui_actions_structured": [step]}))


def test_url_param_without_a_param_is_rejected(tmp_path):
    with pytest.raises(ProfileError, match="param"):
        load_profile(_write(tmp_path, israel_filter={"method": "url_param"}))


def test_flat_multi_location_requires_the_selectors_it_walks(tmp_path):
    """wix.json's notes record hitting exactly this as a live KeyError
    mid-fetch. It belongs at load time, with the filename attached."""
    with pytest.raises(ProfileError, match="location_filter_selector"):
        load_profile(_write(
            tmp_path,
            fetch_type="playwright",
            israel_filter={"method": "post_fetch",
                           "structure": "flat_multi_location",
                           "param": "location"},
            playwright={"job_selector": ".j", "title_selector": ".t",
                        "location_selector": ".l", "link_selector": "a",
                        "pagination": {"method": "url_param"}}))


def test_an_unknown_israel_filter_method_is_rejected(tmp_path):
    with pytest.raises(ProfileError, match="israel_filter.method"):
        load_profile(_write(tmp_path, israel_filter={"method": "magic"}))
