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


# ---------------------------------------------------------------------------
# ...and why the override counts from the END - measured 2026-08-20
#
# The index above was bulletFields.3 when the collision was first fixed, which
# is correct for every Israeli posting on this board. It is not correct for the
# board: the district element is ABSENT wherever Workday's location hierarchy
# has no state/province level, so the array is 4 long there and index 3 lands
# on the brand.
#
# Across the 168 distinct postings on this tenant, 27 are that shape, and index
# 3 reads a brand on all 27 - 'Aristocrat', 'Aristocrat Gaming', 'Aristocrat
# Interactive', 'Product Madness'. That leaves it requisition-shaped on 141 of
# 168 and yielding 145 distinct ids: the same silent collision the override
# exists to prevent, one posting shape away. Index -2 is requisition-shaped on
# 168 of 168 and fully distinct, because the brand is always last.
# ---------------------------------------------------------------------------

# A real posting from the same board whose location carries no district.
_ARISTOCRAT_NO_DISTRICT = {
    "total": 1,
    "jobPostings": [
        {"title": "Senior Database Administrator",
         "externalPath": "/job/London-United-Kingdom/Senior-DBA_R0020949-1",
         # forced Israeli so the posting survives the relevance check and
         # reaches the id mapping, which is what is under test here
         "locationsText": "Israel - Tel Aviv-Yafo",
         "bulletFields": ["Regular", "United Kingdom", "R0020949",
                          "Aristocrat"]},
    ],
}


def _id_at(monkeypatch, payload, index):
    _stub_post(monkeypatch, payload)
    return [j.id for j in api.fetch_workday(_profile(
        fields={"id": "bulletFields.%s" % index, "title": "title",
                "location": "locationsText", "url": "externalPath"}))]


def test_a_fixed_index_reads_the_brand_when_the_district_is_absent(monkeypatch):
    """Index 3 is one posting shape away from re-introducing the collision:
    on a 4-bullet posting it returns the brand, which is shared by every
    posting under that brand."""
    assert _id_at(monkeypatch, _ARISTOCRAT_NO_DISTRICT, 3) == ["Aristocrat"]


def test_counting_from_the_end_survives_both_shapes(monkeypatch):
    """-2 is the requisition id whether or not the district element is there,
    because the brand is always last. Both shapes, one index."""
    assert _id_at(monkeypatch, _ARISTOCRAT_NO_DISTRICT, -2) == ["R0020949"]
    assert _id_at(monkeypatch, _ARISTOCRAT, -2) == ["R0021963", "R0020424"]


def test_a_negative_segment_indexes_from_the_end():
    """The override depends on this, so it is pinned rather than assumed."""
    assert api._get_by_path({"a": ["w", "x", "y", "z"]}, "a.-2") == "y"


def test_an_out_of_range_negative_index_is_None_not_an_exception():
    assert api._get_by_path({"a": ["x"]}, "a.-5") is None


# ---------------------------------------------------------------------------
# israel_facet_auto - the tenants with no country facet, added 2026-08-23
# ---------------------------------------------------------------------------
#
# Three of the eleven Workday boards (palo_alto_networks 1435 postings,
# johnson_johnson 1731, merck 888) expose no `locationCountry` facet at all,
# so they ran unfiltered and the 25-page walk saw their first 500 postings and
# stopped. Palo Alto Networks has 151 Israel-relevant postings; the bot could
# see about 25 of them, on every run, from the day it was imported. Nothing
# could notice: a truncation that is there from the first run never collapses
# anything for the health gate to compare against.
#
# What those tenants DO expose is `locations`, nested one level under
# `locationMainGroup`, whose values are individual offices. Resolved live
# rather than baked in - see fetchers.api._workday_israel_facets for why the
# two facet mechanisms differ on purpose.

_FACETS = {
    "total": 1731,
    "jobPostings": [],
    "facets": [
        {"facetParameter": "timeType",
         "values": [{"descriptor": "Full time", "id": "ft", "count": 900}]},
        {"facetParameter": "locationMainGroup",
         "values": [{"facetParameter": "locations",
                     "descriptor": "Locations",
                     "values": [
                         {"descriptor": "Aachen, North Rhine-Westphalia, "
                                        "Germany", "id": "de1", "count": 10},
                         {"descriptor": "Yokneam, Haifa District, Israel",
                          "id": "il1", "count": 7},
                         {"descriptor": "Office - Israel - Tel Aviv",
                          "id": "il2", "count": 93},
                         {"descriptor": "Alajuela, Costa Rica",
                          "id": "cr1", "count": 4},
                     ]}]},
    ],
}


def _auto_profile():
    return _profile(israel_facet=None, israel_facet_auto=True)


def _record_posts(monkeypatch, responses):
    """Serves `responses` in order and records every request body."""
    bodies = []

    def fake_post(url, json=None, timeout=None):
        bodies.append(json)
        return _Response(responses[min(len(bodies) - 1, len(responses) - 1)])

    monkeypatch.setattr(api.requests, "post", fake_post)
    return bodies


def test_the_israel_facet_is_resolved_from_the_live_facet_list(monkeypatch):
    bodies = _record_posts(monkeypatch, [_FACETS, _LISTING])
    api.fetch_workday(_auto_profile())

    # First request is the facet probe: no filter, one posting.
    assert bodies[0]["appliedFacets"] == {} and bodies[0]["limit"] == 1
    # Every subsequent one carries the resolved offices, keyed by the facet
    # parameter they were found under - which is `locations`, not
    # `locationCountry`, on these tenants.
    assert bodies[1]["appliedFacets"] == {"locations": ["il1", "il2"]}


def test_a_nested_facet_value_that_is_not_israel_is_not_applied(monkeypatch):
    """The filter is is_israel_location, not a substring test, so Germany and
    Costa Rica have to stay out of it however the descriptor is formatted."""
    bodies = _record_posts(monkeypatch, [_FACETS, _LISTING])
    api.fetch_workday(_auto_profile())

    applied = bodies[1]["appliedFacets"]["locations"]
    assert "de1" not in applied and "cr1" not in applied


def test_unreadable_facets_fall_back_to_the_unfiltered_walk(monkeypatch):
    """The safety property. A tenant that changes its facet shape must degrade
    to the old behaviour - never to an empty result, which would read as a
    company with no open jobs and would be believed."""
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            raise RuntimeError("502 from Workday")
        return _Response(_LISTING)

    monkeypatch.setattr(api.requests, "post", fake_post)
    jobs = api.fetch_workday(_auto_profile())

    assert calls[1]["appliedFacets"] == {}          # unfiltered, as before
    assert [j.title for j in jobs] == ["DevOps Engineer III",
                                       "Sr. Software Engineer - Browser Security"]


def test_a_board_with_no_israeli_office_falls_back_rather_than_returning_none(
        monkeypatch):
    facets = {"total": 40, "jobPostings": [], "facets": [
        {"facetParameter": "locations",
         "values": [{"descriptor": "Alajuela, Costa Rica", "id": "cr1"}]}]}
    bodies = _record_posts(monkeypatch, [facets, _LISTING])

    jobs = api.fetch_workday(_auto_profile())

    assert bodies[1]["appliedFacets"] == {}
    assert len(jobs) == 2


def test_a_baked_country_facet_still_wins_and_costs_no_probe(monkeypatch):
    """israel_facet_auto is for the tenants that have no country facet. A
    company that carries one must not pay an extra request for it."""
    bodies = _record_posts(monkeypatch, [_LISTING])
    api.fetch_workday(_profile(israel_facet_auto=True))

    assert bodies[0]["appliedFacets"] == {
        "locationCountry": ["084562884af243748dad7c84c304d89a"]}
    assert bodies[0]["limit"] == 20          # no limit=1 probe happened


def test_a_filtered_board_is_no_longer_capped_at_five_pages(monkeypatch):
    """Palo Alto Networks has 151 Israel-relevant postings. At the old
    facet-path cap of 5 pages of 20, auto-resolving the filter would have
    swapped one silent truncation for a tighter one."""
    page = {"total": 151,
            "jobPostings": [{"title": f"Engineer {i}",
                             "externalPath": f"/job/tlv/e{i}",
                             "locationsText": "Israel - Tel Aviv",
                             "bulletFields": [f"R{i}"]} for i in range(20)]}
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            return _Response(_FACETS)
        start = (len(calls) - 2) * 20
        if start >= 151:
            return _Response({"total": 151, "jobPostings": []})
        return _Response({"total": 151,
                          "jobPostings": [dict(p, bulletFields=[f"R{start + i}"])
                                          for i, p in enumerate(page["jobPostings"])
                                          if start + i < 151]})

    monkeypatch.setattr(api.requests, "post", fake_post)
    jobs = api.fetch_workday(_auto_profile())

    assert len(jobs) == 151
