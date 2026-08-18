# -*- coding: utf-8 -*-
"""Two scale-only defects found on 2026-08-18, both invisible at three
companies and steady noise at a hundred:

  - one opening published once per city arriving as several alerts
  - Greenhouse's offices[] treated as "where this job is" rather than as the
    fallback for an unresolvable location
"""

from types import SimpleNamespace

import pytest

from models import Job
from filters import collapse_duplicate_titles, run_chain, ExperienceFilter
from fetchers.api import fetch_greenhouse, _is_unresolved_location

_PROFILE = SimpleNamespace(slug="acme", detail_fetch=None)


def _job(job_id, title, company="acme", location="Tel Aviv"):
    return Job(id=job_id, title=title, location=location,
               url=f"https://example.com/{job_id}", company=company)


# ---------------------------------------------------------------------------
# Per-city duplicates
# ---------------------------------------------------------------------------

def test_comeet_style_city_duplicates_collapse():
    """The real shape: Comeet appends the location uid to the position uid,
    so one MLOps opening is two ids on two rows."""
    jobs = [_job("B6.D6A", "MLOps Engineer", location="Netanya"),
            _job("B6.D6A-9D.50A", "MLOps Engineer", location="Tel Aviv")]
    kept = collapse_duplicate_titles(jobs)
    assert [j.id for j in kept] == ["B6.D6A"]      # first wins, stably


def test_mobileye_style_distinct_uuids_collapse():
    """Greenhouse/Mobileye issue unrelated uuids per site - there is no id
    relationship to exploit, only the title."""
    jobs = [_job("bb661a53", "Embedded Linux OS Architect", location="Haifa"),
            _job("62f6ee02", "Embedded Linux OS Architect", location="Jerusalem"),
            _job("ac5edc03", "Embedded Linux OS Architect", location="Petah Tikva")]
    assert len(collapse_duplicate_titles(jobs)) == 1


def test_collapsing_is_case_and_whitespace_insensitive():
    jobs = [_job("1", "Data  Scientist"), _job("2", "data scientist "),
            _job("3", "DATA SCIENTIST")]
    assert len(collapse_duplicate_titles(jobs)) == 1


def test_different_companies_are_never_collapsed():
    """Two companies hiring the same role are two jobs, always."""
    jobs = [_job("1", "DevOps Engineer", company="wix"),
            _job("2", "DevOps Engineer", company="monday")]
    assert len(collapse_duplicate_titles(jobs)) == 2


def test_different_titles_are_untouched():
    jobs = [_job("1", "Backend Engineer"), _job("2", "Frontend Engineer")]
    assert len(collapse_duplicate_titles(jobs)) == 2


def test_duplicates_are_dropped_before_the_prescreen_costs_anything(monkeypatch):
    """A collapsed duplicate must not reach the detail layer either - that is
    the request the cap is spent on."""
    seen = []

    def spy(jobs, profile, budget=None):
        seen.extend(job.id for job in jobs)
        return jobs

    import filters as filters_mod
    monkeypatch.setattr(filters_mod.detail, "enrich", spy)
    run_chain([_job("1", "MLOps Engineer"), _job("2", "MLOps Engineer")],
              _PROFILE, [ExperienceFilter()])
    assert seen == ["1"]


def test_an_empty_chain_still_does_not_collapse():
    """No chain means no filtering of any kind, including this one - the
    documented "filters off costs exactly what it used to" guarantee."""
    jobs = [_job("1", "MLOps Engineer"), _job("2", "MLOps Engineer")]
    assert len(run_chain(jobs, _PROFILE, [])) == 2


# ---------------------------------------------------------------------------
# Greenhouse offices[]
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _greenhouse_profile():
    return SimpleNamespace(
        slug="datadog", detail_fetch=None,
        raw={"api": {"endpoint": "https://example.com/jobs",
                     "fields": {"id": "id", "title": "title",
                                "location": "location.name",
                                "url": "absolute_url"}}})


def _run_greenhouse(monkeypatch, jobs):
    import fetchers.api as api_mod
    monkeypatch.setattr(api_mod.requests, "get",
                        lambda *a, **k: _FakeResponse({"jobs": jobs}))
    return fetch_greenhouse(_greenhouse_profile())


# The real Datadog office list, verbatim.
_DATADOG_OFFICES = [{"name": n} for n in
                    ["Bordeaux", "Lisbon", "Lyon", "Madrid", "Montpellier",
                     "Nantes", "Paris", "Remote - France", "Sophia Antipolis",
                     "Tel Aviv"]]


def test_a_foreign_location_is_not_rescued_by_an_israeli_office(monkeypatch):
    jobs = _run_greenhouse(monkeypatch, [{
        "id": 1, "title": "Manager I, Engineering - Husky",
        "location": {"name": "Paris, France"},
        "offices": _DATADOG_OFFICES, "absolute_url": "https://x/1"}])
    assert jobs == []


def test_multiple_locations_still_resolves_through_offices(monkeypatch):
    """The case offices[] exists for must keep working - this is what the
    fix is not allowed to break."""
    jobs = _run_greenhouse(monkeypatch, [{
        "id": 2, "title": "Backend Engineer",
        "location": {"name": "Multiple Locations"},
        "offices": [{"name": "New York"}, {"name": "Tel Aviv"}],
        "absolute_url": "https://x/2"}])
    assert [j.id for j in jobs] == ["2"]


def test_an_israeli_location_is_kept_whatever_the_offices_say(monkeypatch):
    jobs = _run_greenhouse(monkeypatch, [{
        "id": 3, "title": "Data Engineer",
        "location": {"name": "Tel Aviv, Israel"},
        "offices": _DATADOG_OFFICES, "absolute_url": "https://x/3"}])
    assert [j.id for j in jobs] == ["3"]


def test_an_empty_location_falls_back_to_offices(monkeypatch):
    jobs = _run_greenhouse(monkeypatch, [{
        "id": 4, "title": "QA Engineer", "location": {"name": ""},
        "offices": [{"name": "Herzliya"}], "absolute_url": "https://x/4"}])
    assert [j.id for j in jobs] == ["4"]


@pytest.mark.parametrize("value,expected", [
    ("", True), ("Multiple Locations", True), ("multiple locations", True),
    ("  Various  ", True), ("Tel Aviv", False), ("Paris, France", False),
])
def test_unresolved_location_detection(value, expected):
    assert _is_unresolved_location(value) is expected
