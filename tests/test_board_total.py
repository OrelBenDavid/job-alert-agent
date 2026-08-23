# -*- coding: utf-8 -*-
"""
The denominator: how many postings the WHOLE board held, before relevance.

*** Why a post-filter count was not enough ***

Everything this project counts is Israel-relevant, so two situations that need
opposite responses produce the same observation:

    a real zero       the board returns 41 postings and none is in Israel
    the wrong board   the board returns 2 postings, because the endpoint is
                      not the company's careers board at all

Both are `last_count: 0`, both are stable across runs, and both satisfy every
health check in the project - the gates all compare a company against itself,
and a company pointed at the wrong endpoint is perfectly consistent with
itself forever.

Found on 2026-08-23 by fetching every board twice, once with relevance forced
true: `wiz` had been on Greenhouse board_token 'wizprivate' since 2026-08-12,
a real board with two postings on it, while the company hires on 'wizinc' -
124 postings, 22 Israel-relevant. Four companies had migrated ATS and left a
valid, authenticated, empty endpoint behind.

Offline, like the rest of the suite.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import health_report
import state as state_mod
from fetchers import api
from models import Job, JobList, board_total_of


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path)
    yield tmp_path


def _profile(**health):
    return SimpleNamespace(zero_is_plausible=health.get("zero_is_plausible",
                                                        False),
                           expected_min_jobs=health.get("expected_min_jobs", 0))


def _job(i):
    return Job(id="id%d" % i, title="Backend Engineer", location="Tel Aviv",
               url="https://x/%d" % i, company="acme")


# ---------------------------------------------------------------------------
# The carrier
# ---------------------------------------------------------------------------

def test_a_joblist_is_a_list_everywhere_it_matters():
    """The reason it is a list subclass and not a new return type: nothing
    downstream of a fetcher had to learn about it."""
    jobs = JobList([_job(1), _job(2)], board_total=41)

    assert jobs == [_job(1), _job(2)]
    assert len(jobs) == 2 and list(jobs)[0].id == "id1"
    assert board_total_of(jobs) == 41


def test_a_plain_list_reads_as_unknown_not_as_zero():
    """The distinction the whole feature rests on. A fetcher that does not
    report a board size must not be indistinguishable from one reporting an
    empty board."""
    assert board_total_of([_job(1)]) is None
    assert board_total_of(JobList([], board_total=0)) == 0


# ---------------------------------------------------------------------------
# The fetchers report it
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


_GREENHOUSE = {"jobs": [
    {"id": 1, "title": "Backend Engineer", "absolute_url": "https://g/1",
     "location": {"name": "Tel Aviv, Israel"}, "offices": []},
    {"id": 2, "title": "Account Executive", "absolute_url": "https://g/2",
     "location": {"name": "New York, USA"}, "offices": []},
    {"id": 3, "title": "Recruiter", "absolute_url": "https://g/3",
     "location": {"name": "Berlin, Germany"}, "offices": []},
]}


def _greenhouse_profile():
    return SimpleNamespace(
        slug="acme", detail_fetch=None,
        raw={"api": {"platform": "greenhouse",
                     "endpoint": "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                     "fields": {"id": "id", "title": "title",
                                "location": "location.name",
                                "url": "absolute_url"}}})


def test_a_fetcher_reports_the_board_it_saw_not_the_jobs_it_kept(monkeypatch):
    monkeypatch.setattr(api.requests, "get",
                        lambda url, timeout=None: _Response(_GREENHOUSE))

    jobs = api.fetch_greenhouse(_greenhouse_profile())

    assert len(jobs) == 1                    # one Israeli posting
    assert board_total_of(jobs) == 3         # three on the board


def test_an_empty_board_reports_zero_rather_than_nothing(monkeypatch):
    """The `wiz` shape, in miniature: this is the value that would have made
    the mis-resolution visible."""
    monkeypatch.setattr(api.requests, "get",
                        lambda url, timeout=None: _Response({"jobs": []}))

    jobs = api.fetch_greenhouse(_greenhouse_profile())

    assert len(jobs) == 0
    assert board_total_of(jobs) == 0


# ---------------------------------------------------------------------------
# State records it
# ---------------------------------------------------------------------------

def test_state_records_the_board_total_on_a_healthy_run():
    state_mod.process_company("acme", JobList([_job(1)], board_total=41),
                              _profile())
    assert state_mod.load_state("acme")["last_board_total"] == 41


def test_a_fetcher_that_reports_nothing_leaves_the_field_alone():
    """A profile whose fetcher cannot report a board size must not overwrite a
    real number with a misleading 0 - nor invent one."""
    state_mod.process_company("acme", JobList([_job(1)], board_total=41),
                              _profile())
    state_mod.process_company("acme", [_job(1), _job(2)], _profile())

    saved = state_mod.load_state("acme")
    assert saved["last_count"] == 2
    assert saved["last_board_total"] == 41   # not clobbered, not zeroed


def test_a_frozen_run_does_not_write_a_board_total():
    """State is deliberately not overwritten while the health gate holds, and
    that has to include this field - a board size written beside a `last_count`
    from a different run would be a number describing two runs at once."""
    state_mod.seed_company("acme", [_job(i) for i in range(20)])
    result = state_mod.process_company("acme", JobList([], board_total=41),
                                       _profile())

    assert result.status == "empty_suspicious"
    assert "last_board_total" not in state_mod.load_state("acme")


# ---------------------------------------------------------------------------
# ...and it turns the alert into something actionable
# ---------------------------------------------------------------------------

def test_a_zero_with_a_live_board_says_so_in_the_alert():
    state_mod.seed_company("acme", [_job(i) for i in range(20)])
    result = state_mod.process_company("acme", JobList([], board_total=41),
                                       _profile())

    assert "still returns 41 postings" in result.message
    assert "not about a broken selector" in result.message


def test_a_zero_with_an_empty_board_points_at_the_endpoint():
    state_mod.seed_company("acme", [_job(i) for i in range(20)])
    result = state_mod.process_company("acme", JobList([], board_total=0),
                                       _profile())

    assert "returned 0 postings of any kind" in result.message
    assert "the endpoint is the thing to check" in result.message


def test_an_unknown_board_size_adds_nothing_to_the_alert():
    """Saying nothing beats implying zero."""
    state_mod.seed_company("acme", [_job(i) for i in range(20)])
    result = state_mod.process_company("acme", [], _profile())

    assert "the board itself" not in result.message.lower()


def test_a_partial_collapse_is_not_annotated():
    """The board context answers "is the endpoint reaching the right place",
    which is a question about a zero. On a drop from 20 to 4 the endpoint
    plainly works, and the sentence would be noise."""
    state_mod.seed_company("acme", [_job(i) for i in range(20)])
    result = state_mod.process_company("acme",
                                       JobList([_job(i) for i in range(4)],
                                               board_total=41),
                                       _profile())

    assert result.status == "empty_suspicious"
    assert "the board itself" not in result.message.lower()


# ---------------------------------------------------------------------------
# ...and the report surfaces it
# ---------------------------------------------------------------------------

def _health(**kwargs):
    base = dict(slug="acme", name="Acme", platform="greenhouse", enabled=True,
                seeded=True)
    base.update(kwargs)
    return health_report.CompanyHealth(**base)


def test_a_two_posting_board_is_flagged():
    assert _health(last_board_total=2).implausible_board is True


def test_a_real_board_is_not_flagged():
    assert _health(last_board_total=124).implausible_board is False


def test_an_unmeasured_company_is_never_flagged():
    """Absent evidence is not evidence. A company whose fetcher reports no
    board size must not be accused on the strength of a missing field."""
    assert _health(last_board_total=None).implausible_board is False


def test_board_yield_is_none_rather_than_a_division_by_zero():
    assert _health(last_board_total=0, last_count=0).board_yield is None
    assert _health(last_board_total=100, last_count=22).board_yield == 0.22
