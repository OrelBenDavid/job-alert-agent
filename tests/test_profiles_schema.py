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

from profiles import (CURRENT_SCHEMA_VERSION, PROFILES_DIR, ProfileError,
                      load_profile)

_BASE = {
    "schema_version": 3,
    "slug": "acme",
    "name": "Acme",
    "enabled": True,
    "careers_url": "https://jobs.lever.co/acme",
    "fetch_type": "api",
    "israel_filter": {"method": "post_fetch"},
    "api": {"platform": "lever",
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
    for path in sorted(PROFILES_DIR.glob("*.json")):
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
        "method": "inline", "inline_field": "content", "content_is_html": True}))
    assert profile.detail_fetch["inline_field"] == "content"


def test_html_block_is_accepted(tmp_path):
    profile = load_profile(_write(tmp_path, detail_fetch={
        "method": "html", "url_source": "job_url",
        "content_selector": "#job-description",
        "verified_on_job_url": "https://jobs.lever.co/acme/abc"}))
    assert profile.detail_fetch["method"] == "html"


@pytest.mark.parametrize("block,message", [
    ({"method": "carrier-pigeon"}, "method invalid"),
    ({"method": "inline"}, "inline_field"),
    ({"method": "html"}, "content_selector"),
    ({"method": "playwright"}, "content_selector"),
    ({"method": "html", "content_selector": "#d", "url_source": "magic"},
     "url_source invalid"),
    ({"method": "html", "content_selector": "#d", "url_source": "template"},
     "url_template"),
])
def test_malformed_blocks_fail_at_load_time(tmp_path, block, message):
    with pytest.raises(ProfileError, match=message):
        load_profile(_write(tmp_path, detail_fetch=block))


def test_current_schema_version_is_three():
    assert CURRENT_SCHEMA_VERSION == 3
