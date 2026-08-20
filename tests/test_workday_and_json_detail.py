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
# The per-tenant bulletFields layout - added 2026-08-20, after a silent
# duplicate-id defect on neogames (the aristocrat tenant).
#
# Captured live 2026-08-20 from aristocrat/wd3/AristocratExternalCareersSite
# with the profile's Israel facet applied, trimmed to the fields Workday
# actually returns.


_ARISTOCRAT = {
    "total": 2,
    "jobPostings": [
        {"title": "Games Product Manager",
         "externalPath": "/job/Israel---Tel-Aviv-Yafo/Games-Product-Manager_R0021963",
         "locationsText": "Israel - Tel Aviv-Yafo",
         "postedOn": "Posted 3 Days Ago",
         "remoteType": "Hybrid",
         "bulletFields": ["Regular", "Tel Aviv District", "Israel",
                          "R0021963", "Aristocrat"]},
        {"title": "Bookkeeper",
         "externalPath": "/job/Israel---Tel-Aviv-Yafo/Bookkeeper_R0020424",
         "locationsText": "Israel - Tel Aviv-Yafo",
         "postedOn": "Posted 30+ Days Ago",
         "remoteType": "Hybrid",
         "bulletFields": ["Regular", "Tel Aviv District", "Israel",
                          "R0020424", "Aristocrat"]},
    ],
}

# The same board, unfiltered: a location with no state/district gets FOUR
# bullets, not five. Also live 2026-08-20 - 6 of the 40 postings look like this.
# locationsText is forced Israeli here so the posting survives the relevance
# check and reaches the id assignment, which is what is under test.
_ARISTOCRAT_NO_DISTRICT = {
    "title": "Senior Database Administrator",
    "externalPath": "/job/London-United-Kingdom/Senior-Database-Administrator_R0020949-1",
    "locationsText": "Israel - Tel Aviv-Yafo",
    "postedOn": "Posted Today",
    "bulletFields": ["Regular", "United Kingdom", "R0020949", "Aristocrat"],
}

_ARISTOCRAT_ENDPOINT = ("https://aristocrat.wd3.myworkdayjobs.com/wday/cxs/"
                        "aristocrat/AristocratExternalCareersSite/jobs")


def _aristocrat_profile(id_path):
    return _profile(endpoint=_ARISTOCRAT_ENDPOINT,
                    fields={"id": id_path, "title": "title",
                            "location": "locationsText", "url": "externalPath"})


def test_the_platform_default_collides_every_posting_on_this_tenant(monkeypatch):
    """*** The defect this whole block exists for. ***

    bulletFields is a per-tenant configuration of what a search card shows, not
    a fixed schema. On aristocrat it is
    [employment type, district, country, requisition id, brand], so the
    platform default of bulletFields.0 reads the literal string "Regular" on
    every posting.

    That is not a cosmetic wrong id. state.process_company diffs on Job.id, so a
    whole board under one id is ONE entry in the state jobs map and no posting
    on it can ever read as new again - while the count stays healthy, so the
    collapse gate sees nothing wrong. neogames was silently un-alertable this
    way from 2026-08-18 to 2026-08-20, with two on-family postings behind it.
    """
    monkeypatch.setattr(api.requests, "post",
                        lambda *a, **k: _Response(_ARISTOCRAT))
    jobs = api.fetch_workday(_aristocrat_profile("bulletFields.0"))
    assert len(jobs) == 2
    assert len({j.id for j in jobs}) == 2, (
        "two postings collapsed onto one id - a board in this state is "
        "permanently un-alertable and nothing upstream reports it")


def test_the_id_path_comes_from_the_profile_so_one_tenant_can_be_overridden(monkeypatch):
    """The fix is a per-company api.fields.id, NOT a change to the platform
    profile: bulletFields.0 is right on the other 10 Workday tenants here,
    verified live 2026-08-20 - all 11 return unique ids."""
    monkeypatch.setattr(api.requests, "post",
                        lambda *a, **k: _Response(_ARISTOCRAT))
    ids = [j.id for j in api.fetch_workday(_aristocrat_profile("bulletFields.-2"))]
    assert ids == ["R0021963", "R0020424"]


def test_the_index_counts_from_the_END_because_the_district_can_be_absent(monkeypatch):
    """Why -2 and not 3. A location with no state/district ships four bullets
    instead of five, so a fixed index 3 reads the BRAND there - "Aristocrat" on
    most postings, which is the identical silent collision in a new shape.
    Counting back from the brand was correct on all 40 postings on this board.
    """
    payload = {"total": 1, "jobPostings": [_ARISTOCRAT_NO_DISTRICT]}
    monkeypatch.setattr(api.requests, "post", lambda *a, **k: _Response(payload))

    at_three = api.fetch_workday(_aristocrat_profile("bulletFields.3"))
    assert at_three[0].id == "Aristocrat"        # the brand - a collision waiting

    at_minus_two = api.fetch_workday(_aristocrat_profile("bulletFields.-2"))
    assert at_minus_two[0].id == "R0020949"      # the requisition id


def test_a_field_path_that_misses_the_array_falls_back_to_externalPath(monkeypatch):
    """An out-of-range index must not raise mid-fetch, and must not yield an
    empty id - an empty id would collide exactly the way "Regular" did."""
    monkeypatch.setattr(api.requests, "post",
                        lambda *a, **k: _Response(_ARISTOCRAT))
    ids = [j.id for j in api.fetch_workday(_aristocrat_profile("bulletFields.99"))]
    assert ids == ["/job/Israel---Tel-Aviv-Yafo/Games-Product-Manager_R0021963",
                   "/job/Israel---Tel-Aviv-Yafo/Bookkeeper_R0020424"]


def test_get_by_path_indexes_lists_including_negative():
    """The field map can address arrays now. Before this, _get_by_path returned
    None for any list, so bulletFields.0 in the platform profile was decorative:
    the fetcher had index 0 hardcoded and no profile could override it."""
    item = {"bulletFields": ["Regular", "Israel", "R1", "Aristocrat"],
            "nested": {"a": [{"b": 7}]}}
    assert api._get_by_path(item, "bulletFields.0") == "Regular"
    assert api._get_by_path(item, "bulletFields.-2") == "R1"
    assert api._get_by_path(item, "nested.a.0.b") == 7
    assert api._get_by_path(item, "bulletFields.9") is None       # out of range
    assert api._get_by_path(item, "bulletFields.title") is None   # not an index
    assert api._get_by_path(item, "nested.0") is None             # index into a dict


# ---------------------------------------------------------------------------
# The collision guard - what keeps a repeat of this from being silent


def test_colliding_ids_fall_back_to_externalPath(monkeypatch):
    """A misconfigured field map must degrade to a DUPLICATE alert, never to
    silence. Two real Workday postings never share a requisition id, so a
    collision always means the map points at the wrong bullet. externalPath is
    unique per posting (all 40 on this board); its known flaw is that a reworded
    title re-alerts an unchanged posting, and re-sending is the mistake this
    project chooses every time - see restore_state and the accept-after paths
    in state.py."""
    monkeypatch.setattr(api.requests, "post",
                        lambda *a, **k: _Response(_ARISTOCRAT))
    jobs = api.fetch_workday(_aristocrat_profile("bulletFields.0"))
    assert [j.id for j in jobs] == [
        "/job/Israel---Tel-Aviv-Yafo/Games-Product-Manager_R0021963",
        "/job/Israel---Tel-Aviv-Yafo/Bookkeeper_R0020424"]
    assert [j.title for j in jobs] == ["Games Product Manager", "Bookkeeper"]


def test_a_unique_id_is_not_dragged_onto_externalPath_by_a_colliding_neighbour(monkeypatch):
    """Only the colliding ids are rewritten. One broken bullet must not move a
    whole board onto the fragile title-derived identifier."""
    payload = {"total": 3, "jobPostings": [
        dict(_ARISTOCRAT["jobPostings"][0], bulletFields=["X"]),
        dict(_ARISTOCRAT["jobPostings"][1], bulletFields=["X"]),
        {"title": "Data Engineer", "externalPath": "/job/Israel/Data-Engineer_R3",
         "locationsText": "Israel - Tel Aviv-Yafo", "bulletFields": ["R0033333"]},
    ]}
    monkeypatch.setattr(api.requests, "post", lambda *a, **k: _Response(payload))
    ids = [j.id for j in api.fetch_workday(_aristocrat_profile("bulletFields.0"))]
    assert ids[2] == "R0033333"                       # untouched
    assert ids[0] != ids[1]
    assert all(i.startswith("/job/") for i in ids[:2])


def test_the_other_workday_tenants_are_untouched(monkeypatch):
    """The platform default still reads the requisition id everywhere it was
    already right - this fix must not become a platform-wide change."""
    monkeypatch.setattr(api.requests, "post",
                        lambda *a, **k: _Response(_LISTING))
    assert [j.id for j in api.fetch_workday(_profile())] == ["R29093", "R29094"]
