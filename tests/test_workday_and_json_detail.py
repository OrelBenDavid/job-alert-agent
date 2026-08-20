# -*- coding: utf-8 -*-
"""The Workday fetcher and the `json` detail method, both added 2026-08-19.

Workday is the first platform here that is a POST rather than a GET, the first
that filters server-side, and the first whose description lives behind a JSON
endpoint rather than in the listing or in a page. Each of those is a place the
existing shapes did not fit, so each gets a test.

Offline, like the rest of the suite - every response below is a real one,
trimmed, captured live from crowdstrike/wd5/crowdstrikecareers and
fairtility on 2026-08-19.
"""

import json
from types import SimpleNamespace

import pytest

import detail
from fetchers import api
from models import Job


# A real jobPostings page, trimmed to the fields Workday actually returns.
_LISTING = {
    "total": 3,
    "jobPostings": [
        {"title": "DevOps Engineer III",
         "externalPath": "/job/Israel---Tel-Aviv/DevOps-Engineer-III_R29093",
         "locationsText": "Israel - Tel Aviv",
         "postedOn": "Posted 12 Days Ago",
         "bulletFields": ["R29093"]},
        {"title": "Sr. Software Engineer - Browser Security",
         "externalPath": "/job/Israel---Tel-Aviv/Sr-Software-Engineer_R29094",
         "locationsText": "Israel - Tel Aviv",
         "postedOn": "Posted Yesterday",
         "bulletFields": ["R29094"]},
        {"title": "Regional Sales Director",
         "externalPath": "/job/USA-Chicago/Regional-Sales-Director_R11111",
         "locationsText": "USA, IL, Chicago",
         "postedOn": "Posted Today",
         "bulletFields": ["R11111"]},
    ],
}

_ENDPOINT = ("https://crowdstrike.wd5.myworkdayjobs.com/wday/cxs/"
             "crowdstrike/crowdstrikecareers/jobs")


def _profile(**api_overrides):
    block = {
        "platform": "workday",
        "endpoint": _ENDPOINT,
        "israel_facet": "084562884af243748dad7c84c304d89a",
        "fields": {"id": "bulletFields.0", "title": "title",
                   "location": "locationsText", "url": "externalPath"},
    }
    block.update(api_overrides)
    return SimpleNamespace(slug="crowdstrike", detail_fetch=None,
                           raw={"api": block})


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# The fetcher


def test_it_posts_rather_than_gets(monkeypatch):
    """Every other handler here is a GET. Workday's search endpoint only
    answers a POST, so getting this wrong fails for the whole platform."""
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"], seen["body"] = url, json
        return _Response(_LISTING)

    monkeypatch.setattr(api.requests, "post", fake_post)
    api.fetch_workday(_profile())
    assert seen["url"] == _ENDPOINT
    assert set(seen["body"]) == {"appliedFacets", "limit", "offset", "searchText"}


def test_the_israel_facet_is_applied_when_the_profile_carries_one(monkeypatch):
    """This is what keeps Workday affordable: without it a 449-posting board
    costs 23 requests per company per run."""
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen.update(json["appliedFacets"])
        return _Response(_LISTING)

    monkeypatch.setattr(api.requests, "post", fake_post)
    api.fetch_workday(_profile())
    assert seen == {"locationCountry": ["084562884af243748dad7c84c304d89a"]}


def test_a_missing_facet_degrades_to_unfiltered_rather_than_failing(monkeypatch):
    """A rotated facet id must not take the company down - the post-fetch
    location check still keeps the RESULT correct, only the cost goes up."""
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen.update(json)
        return _Response(_LISTING)

    monkeypatch.setattr(api.requests, "post", fake_post)
    jobs = api.fetch_workday(_profile(israel_facet=None))
    assert seen["appliedFacets"] == {}
    assert len(jobs) == 2          # still correct, just more expensive


def test_non_israeli_postings_are_dropped_even_with_the_facet(monkeypatch):
    """'USA, IL, Chicago' is Illinois. The facet should never return it, but
    the post-fetch check is the defence if the facet ever stops matching."""
    monkeypatch.setattr(api.requests, "post",
                        lambda *a, **k: _Response(_LISTING))
    titles = [j.title for j in api.fetch_workday(_profile())]
    assert "Regional Sales Director" not in titles
    assert len(titles) == 2


def test_the_id_is_the_requisition_not_the_path(monkeypatch):
    """externalPath is derived from the job TITLE, so a reworded title would
    produce a new id and a duplicate alert for an unchanged posting."""
    monkeypatch.setattr(api.requests, "post",
                        lambda *a, **k: _Response(_LISTING))
    ids = [j.id for j in api.fetch_workday(_profile())]
    assert ids == ["R29093", "R29094"]


def test_the_public_url_is_built_from_the_cxs_endpoint():
    """Derived rather than stored, so the two cannot drift apart. Verified
    live 2026-08-19 that this form returns HTTP 200."""
    url = api._build_workday_url(
        {"endpoint": _ENDPOINT}, "/job/Israel---Tel-Aviv/DevOps_R29093")
    assert url == ("https://crowdstrike.wd5.myworkdayjobs.com/en-US/"
                   "crowdstrikecareers/job/Israel---Tel-Aviv/DevOps_R29093")


def test_pagination_stops_at_total(monkeypatch):
    """A board that keeps answering must not loop forever."""
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json["offset"])
        return _Response({"total": 3, "jobPostings": _LISTING["jobPostings"]})

    monkeypatch.setattr(api.requests, "post", fake_post)
    api.fetch_workday(_profile())
    assert calls == [0]            # 3 <= 20, so one page is the whole board


# ---------------------------------------------------------------------------
# The json detail method


def test_json_detail_reads_a_dotted_path():
    raw = json.dumps({"jobPostingInfo": {"jobDescription": "<p>5 years</p>"}})
    assert detail._extract_from_json(
        raw, {"json_path": "jobPostingInfo.jobDescription"}) == "<p>5 years</p>"


def test_json_detail_renders_a_MAPPING_of_sections():
    """SmartRecruiters returns sections keyed rather than ordered. The heading
    has to survive: 'Qualifications' becoming a real heading is what promotes
    the bullets under it to mandatory in experience.classify_block."""
    raw = json.dumps({"jobAd": {"sections": {
        "jobDescription": {"title": "Job Description", "text": "<p>Build things</p>"},
        "qualifications": {"title": "Qualifications", "text": "<li>3+ years</li>"},
    }}})
    out = detail._extract_from_json(raw, {
        "json_path": "jobAd.sections",
        "inline_section_heading": "title",
        "inline_section_content": "text"})
    assert "Qualifications" in out
    assert "3+ years" in out


def test_json_detail_fails_open_on_junk():
    """A detail-layer miss must read as 'undetermined', never raise - the
    posting is still delivered, flagged."""
    assert detail._extract_from_json("not json at all", {"json_path": "a.b"}) is None
    assert detail._extract_from_json('{"a": 1}', {"json_path": "x.y"}) is None
    assert detail._extract_from_json('{"a": {"b": 7}}', {"json_path": "a.b"}) is None


def test_json_detail_needs_a_request():
    """It costs one GET per posting, so it must draw on the run-wide budget
    exactly as html and embedded_json do."""
    assert detail.wants_detail_fetch(SimpleNamespace(
        detail_fetch={"method": "json", "json_path": "a"})) is True


# ---------------------------------------------------------------------------
# url_rewrite


def test_url_rewrite_turns_a_public_page_into_its_json_endpoint():
    """Workday's description is at the CXS path, which differs from the public
    posting URL by one segment - and no placeholder carries a path."""
    job = Job(id="R29093", title="t", location="Israel - Tel Aviv",
              url="https://crowdstrike.wd5.myworkdayjobs.com/en-US/"
                  "crowdstrikecareers/job/Israel---Tel-Aviv/DevOps_R29093",
              company="crowdstrike")
    out = detail._detail_url(job, {"url_rewrite": ["/en-US/",
                                                   "/wday/cxs/crowdstrike/"]})
    assert out == ("https://crowdstrike.wd5.myworkdayjobs.com/wday/cxs/"
                   "crowdstrike/crowdstrikecareers/job/Israel---Tel-Aviv/"
                   "DevOps_R29093")


def test_url_rewrite_is_a_no_op_when_the_pattern_is_absent():
    """A posting whose URL does not carry the segment must fall through to the
    plain job URL rather than producing a mangled one."""
    job = Job(id="1", title="t", location="Tel Aviv",
              url="https://example.com/jobs/1", company="x")
    assert detail._detail_url(
        job, {"url_rewrite": ["/en-US/", "/other/"]}) == "https://example.com/jobs/1"


# ---------------------------------------------------------------------------
# The id mapping, and the collision it hid - found 2026-08-20
#
# fetch_workday used to hardcode bulletFields[0], which made api.fields.id
# decorative: the platform profile documented a mapping the code never read.
#
# bulletFields is a POSITIONAL array with no schema. Verified live across all
# 11 Workday companies: 10 carry the requisition id at index 0, and Aristocrat
# (neogames) orders it ['Regular', 'Tel Aviv District', 'Israel', 'R0021963',
# 'Aristocrat'] - index 0 is the EMPLOYMENT TYPE, and every posting on that
# board is 'Regular'.
#
# The diff runs on Job.id, so both of that board's Israeli postings collapsed
# onto one key. The survivor was a Bookkeeper, which roles.py blocks, and the
# on-family posting was permanently un-alertable. last_count stayed at 2 -
# stable and above zero - so the health gate never fired and never would.
# ---------------------------------------------------------------------------

def _stub_post(monkeypatch, payload):
    """Serve one fixed listing page to fetch_workday's POST."""
    monkeypatch.setattr(api.requests, "post",
                        lambda url, json=None, timeout=None: _Response(payload))


_ARISTOCRAT = {
    "total": 2,
    "jobPostings": [
        {"title": "Games Product Manager",
         "externalPath": "/job/Israel---Tel-Aviv-Yafo/Games-Product-Manager_R0021963",
         "locationsText": "Israel - Tel Aviv-Yafo",
         "bulletFields": ["Regular", "Tel Aviv District", "Israel",
                          "R0021963", "Aristocrat"]},
        {"title": "Bookkeeper",
         "externalPath": "/job/Israel---Tel-Aviv-Yafo/Bookkeeper_R0020424",
         "locationsText": "Israel - Tel Aviv-Yafo",
         "bulletFields": ["Regular", "Tel Aviv District", "Israel",
                          "R0020424", "Aristocrat"]},
    ],
}


def test_index_0_collides_every_posting_on_the_aristocrat_ordering(monkeypatch):
    """The defect itself, pinned. With the platform default, this real board
    produces two postings that state cannot tell apart."""
    _stub_post(monkeypatch, _ARISTOCRAT)
    jobs = api.fetch_workday(_profile())
    assert len(jobs) == 2
    assert len({j.id for j in jobs}) == 1        # <-- the collision


def test_a_company_can_override_the_index_and_separate_them(monkeypatch):
    """The fix: api.fields.id is READ, so a per-company record can point at
    the index where that tenant actually puts the requisition id."""
    _stub_post(monkeypatch, _ARISTOCRAT)
    profile = _profile(fields={"id": "bulletFields.3", "title": "title",
                               "location": "locationsText",
                               "url": "externalPath"})
    jobs = api.fetch_workday(profile)
    assert sorted(j.id for j in jobs) == ["R0020424", "R0021963"]


def test_an_out_of_range_index_falls_back_rather_than_raising(monkeypatch):
    """A mapping that does not resolve must not take the run down. externalPath
    is a poor id - it is derived from the title - but it is better than
    dropping the posting or raising mid-fetch."""
    _stub_post(monkeypatch, _ARISTOCRAT)
    profile = _profile(fields={"id": "bulletFields.99", "title": "title",
                               "location": "locationsText",
                               "url": "externalPath"})
    jobs = api.fetch_workday(profile)
    assert len(jobs) == 2
    assert all(j.id.startswith("/job/") for j in jobs)


# ---------------------------------------------------------------------------
# _get_by_path list indexing, which is what makes the override expressible
# ---------------------------------------------------------------------------

def test_a_numeric_segment_indexes_a_list():
    assert api._get_by_path({"a": ["x", "y", "z"]}, "a.1") == "y"


def test_a_numeric_segment_reads_a_dict_KEY_first():
    """A platform whose JSON genuinely has "0" as a field name must keep
    working - dict lookup wins before the list branch is considered."""
    assert api._get_by_path({"a": {"0": "by-key"}}, "a.0") == "by-key"


def test_an_out_of_range_index_is_None_not_an_exception():
    assert api._get_by_path({"a": ["x"]}, "a.5") is None


def test_a_non_numeric_segment_against_a_list_is_None():
    assert api._get_by_path({"a": ["x"]}, "a.name") is None
