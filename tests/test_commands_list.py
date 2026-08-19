# -*- coding: utf-8 -*-
"""/list must agree with what the bot actually fetches.

It used to read `enabled` straight off each profile FILE, which is correct only
for a standalone profile. A platform-backed company record carries only the
fields that differ per company; `enabled` is inherited from
profiles/_platforms/<platform>.json. So the flag read as None for every one of
them and /list reported 255 of 256 companies as paused - while all 256 were
being fetched on every run.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import commands as commands_mod
import profiles as profiles_mod
from notifier import TELEGRAM_MAX_CHARS


PLATFORM = {
    "schema_version": 3, "enabled": True, "fetch_type": "api",
    "israel_filter": {"method": "post_fetch", "structure": "none",
                      "param": None},
    "api": {"platform": "lever", "endpoint": "https://example.test/x",
            "fields": {"id": "id", "title": "text",
                       "location": "categories.location", "url": "hostedUrl"}},
    "health": {"expected_min_jobs": 0},
    "verified_on": "2026-08-19",
}


def _build_profiles_dir(tmp_path, companies):
    (tmp_path / "_platforms").mkdir()
    (tmp_path / "_platforms" / "lever.json").write_text(
        json.dumps(PLATFORM), encoding="utf-8")
    (tmp_path / "companies").mkdir()
    for slug, name, extra in companies:
        record = {"slug": slug, "name": name, "platform": "lever",
                  "careers_url": "https://example.test/" + slug,
                  "verified_on": "2026-08-19"}
        record.update(extra)
        (tmp_path / "companies" / (slug + ".json")).write_text(
            json.dumps(record), encoding="utf-8")
    return tmp_path


@pytest.fixture
def profiles_dir(tmp_path, monkeypatch):
    """Points /list at a throwaway profiles tree.

    load_all() binds PROFILES_DIR as a DEFAULT ARGUMENT, so patching the module
    constant does nothing - the directory has to be passed in. PLATFORMS_DIR is
    read at call time by load_platform and so is patched normally."""
    def _make(companies):
        directory = _build_profiles_dir(tmp_path, companies)
        monkeypatch.setattr(profiles_mod, "PLATFORMS_DIR", directory / "_platforms")
        monkeypatch.setattr(commands_mod, "load_all",
                            lambda: profiles_mod.load_all(directory))
        return directory
    return _make


def test_a_platform_backed_company_reads_as_active(profiles_dir):
    """The regression this whole fix is about: the company record carries no
    `enabled` of its own, and inherits True from its platform."""
    profiles_dir([("acme", "Acme", {})])
    reply = commands_mod._handle_list()
    assert "✅ Acme" in reply
    assert "⏸" not in reply


def test_a_company_paused_by_remove_still_reads_as_paused(profiles_dir):
    """/remove writes enabled=false onto the company record, which overrides
    the platform. The two halves have to keep agreeing."""
    profiles_dir([("acme", "Acme", {}), ("beta", "Beta", {"enabled": False})])
    reply = commands_mod._handle_list()
    assert "✅ Acme" in reply
    assert "⏸️ Beta" in reply


def test_a_broken_profile_is_reported_instead_of_taking_the_reply_down(
        profiles_dir, tmp_path):
    """A malformed file used to raise out of json.loads here, so the user got
    no reply at all and the command looked dead."""
    directory = profiles_dir([("acme", "Acme", {})])
    (directory / "companies" / "broken.json").write_text("{not json",
                                                         encoding="utf-8")
    reply = commands_mod._handle_list()
    assert "✅ Acme" in reply
    assert "⚠" in reply          # the failure is surfaced, not swallowed


def test_the_reply_never_exceeds_the_telegram_limit(profiles_dir):
    """Telegram rejects anything over 4096 characters with a 400, which fails
    the whole reply. At 256 companies the real board renders ~3,000 chars, so
    this is close enough that the next batch of additions would have silently
    turned /list off."""
    many = [("c%03d" % i, "Company Number %03d Ltd" % i, {}) for i in range(400)]
    profiles_dir(many)
    reply = commands_mod._handle_list()
    assert len(reply) <= TELEGRAM_MAX_CHARS
    assert "400" in reply             # the honest total is still reported


def test_an_empty_board_says_so(profiles_dir):
    profiles_dir([])
    assert "אין חברות" in commands_mod._handle_list()
