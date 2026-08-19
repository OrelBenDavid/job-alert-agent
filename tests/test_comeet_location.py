# -*- coding: utf-8 -*-
"""Comeet resolves a posting's location from THREE fields, not one.

`location.name` is free text the company types itself. Audited across all 108
Comeet boards on 2026-08-19 it was a website, a page name, an office nickname,
a street address, a region and a misspelt city - on postings whose
`location.country` was "IL" every time. Reading it alone dropped 47
Israel-relevant postings at 9 companies, four of which had therefore never
delivered a single alert while their health gate reported a steady, plausible
zero.

Every fixture below is a real response shape taken from that live audit.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from profiles import find_profile_path, load_profile


def _fake_response(json_data):
    r = MagicMock()
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


def _position(uid, title, name, city, country, **extra):
    """One Comeet position, in the shape the live API returns."""
    position = {
        "uid": uid, "name": title,
        "location": {"name": name, "city": city, "country": country,
                     "state": "", "is_remote": False},
        "url_comeet_hosted_page": "https://www.comeet.co/jobs/x/" + uid,
    }
    position.update(extra)
    return position


def _fetch(positions, slug="panaya"):
    from fetchers import api as api_mod
    profile = load_profile(find_profile_path(slug))
    assert profile.raw["api"]["platform"] == "comeet"
    with patch.object(api_mod.requests, "get",
                      return_value=_fake_response(positions)):
        return api_mod.fetch(profile)


# --------------------------------------------------------------------------
# The bug: a label that is not a place
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label,city", [
    ("Herzeliya", "Herzliya"),                      # bird_aerosystems - misspelt
    ("www.final.co.il", "Ramat Hasharon"),          # final - a website
    ("careers", "Giv'atayim"),                      # imagen - a page name
    ("GK8 by Galaxy", "Ramat Gan"),                 # gk8 - an office nickname
    ("ActiveFence HQ", "Binyamina-Giv'at Ada"),     # activefence
    ("Tozeret Haaretz 3", "Tel Aviv"),              # drivenets - a street
    ("EMEA", "Yokneam"),                            # lumenis - a region
    ("Idan Hanegev", "Rahat"),                      # sodastream - an estate
])
def test_israeli_posting_is_kept_when_the_label_is_not_a_place(label, city):
    """Each of these was silently dropped before 2026-08-19."""
    jobs = _fetch([_position("41.AAA", "Backend Engineer", label, city, "IL")])
    assert [j.id for j in jobs] == ["41.AAA"]


def test_country_code_alone_is_enough():
    """The city may be one the keyword list has never heard of. The country
    code is a picker value, so it decides on its own."""
    jobs = _fetch([_position("41.BBB", "QA Engineer",
                             "Some Office", "Kiryat Nowhere", "IL")])
    assert len(jobs) == 1


def test_a_blank_country_still_falls_back_to_the_text_check():
    """Live Comeet postings do carry an empty country on genuinely Israeli
    roles. A missing code must mean nothing, never a rejection."""
    jobs = _fetch([_position("41.CCC", "Data Engineer",
                             "Tel Aviv, Israel", "Tel Aviv", "")])
    assert len(jobs) == 1


def test_a_foreign_country_code_never_rejects_on_its_own():
    """is_israel_country_code is additive only. A req attached to a foreign
    office whose location text still reads as Israel must survive - dropping
    it would be exactly the silent loss the gate exists to prevent."""
    jobs = _fetch([_position("41.DDD", "Platform Engineer",
                             "Tel Aviv", "Tel Aviv", "US")])
    assert len(jobs) == 1


# --------------------------------------------------------------------------
# The other direction: the city is what exposes a foreign remote role
# --------------------------------------------------------------------------

def test_remote_label_with_a_foreign_city_is_now_rejected():
    """activefence publishes 9 of these: label "Remote", city "New York". The
    label alone reads as open-to-anywhere and was kept; joined with the city,
    the existing qualified-remote rule sees the foreign metro."""
    jobs = _fetch([_position("41.EEE", "Account Executive",
                             "Remote", "New York", "US")])
    assert jobs == []


def test_remote_label_with_no_qualifying_city_is_still_kept():
    """"Remote" with nothing foreign attached stays open - EMEA includes
    Israel, and this half of the rule is unchanged."""
    jobs = _fetch([_position("41.FFF", "Backend Engineer",
                             "Remote", "Remote", "")])
    assert len(jobs) == 1


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------

def test_display_prefers_the_city_when_the_label_is_not_a_place():
    jobs = _fetch([_position("41.GGG", "Deep Learning Engineer",
                             "careers", "Giv'atayim", "IL")])
    assert jobs[0].location == "Giv'atayim"


@pytest.mark.parametrize("label", [
    "Jerusalem Office / Hybrid (In Israel)",   # cross_river_technologies
    "Ramat Gan, Israel (Hybrid)",              # viber
    "Be'er Sheva Hybrid",                      # mdclone
])
def test_display_keeps_the_label_when_it_carries_the_arrangement(label):
    """The label is preferred whenever it says something the bare city cannot.
    Collapsing these to the city would drop the hybrid/remote arrangement,
    which is the one thing the user needs from the location line."""
    jobs = _fetch([_position("41.HHH", "Support Engineer", label,
                             "Jerusalem", "IL")])
    assert jobs[0].location == label


def test_display_falls_back_to_the_label_when_there_is_no_city():
    jobs = _fetch([_position("41.III", "Backend Engineer",
                             "Tel Aviv, Israel", "", "IL")])
    assert jobs[0].location == "Tel Aviv, Israel"


# --------------------------------------------------------------------------
# The mechanism is profile-driven, and degrades to the old behaviour
# --------------------------------------------------------------------------

def test_the_platform_profile_maps_all_three_location_fields():
    profile = load_profile(find_profile_path("panaya"))
    fields = profile.raw["api"]["fields"]
    assert fields["location"] == "location.name"
    assert fields["location_city"] == "location.city"
    assert fields["location_country"] == "location.country"


def test_a_profile_without_the_new_fields_behaves_exactly_as_before():
    """The two field-map entries are optional, so an api profile that maps
    neither is unaffected - which is what keeps every other platform, and any
    hand-written Comeet record, resolving as it always did."""
    from fetchers import api as api_mod

    profile = load_profile(find_profile_path("panaya"))
    profile.raw["api"]["fields"] = {
        k: v for k, v in profile.raw["api"]["fields"].items()
        if not k.startswith("location_")}

    positions = [_position("41.JJJ", "Backend Engineer",
                           "careers", "Giv'atayim", "IL")]
    with patch.object(api_mod.requests, "get",
                      return_value=_fake_response(positions)):
        assert api_mod.fetch(profile) == []      # the pre-fix outcome
