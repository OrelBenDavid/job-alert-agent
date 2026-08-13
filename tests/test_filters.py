# -*- coding: utf-8 -*-
"""
Tests for the filter chain: the title pre-check, the fail-open decision
table, the runtime toggle, and the cost guarantees that make the whole thing
safe to leave on.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import detail as detail_mod
import filters as filters_mod
from filters import (ExperienceFilter, build_chain, run_chain,
                     title_looks_senior)
from models import Job
from stats import RunStats


def _job(job_id="1", title="Backend Developer", description=None):
    return Job(id=job_id, title=title, location="Tel Aviv",
               url=f"https://example.com/{job_id}", company="acme",
               description=description)


# A profile with no way to reach descriptions - the shape every existing v2
# profile has. Tests that need a description set it on the Job directly.
_PROFILE = SimpleNamespace(slug="acme", detail_fetch=None)


# ---------------------------------------------------------------------------
# H. Title pre-check - reject-only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Senior Backend Engineer", "Sr. Data Analyst", "Sr Data Analyst",
    "Team Lead", "Staff Engineer", "Principal Scientist",
    "Software Architect", "Engineering Manager", "Director of Engineering",
    "Head of Product", "VP R&D",
])
def test_senior_titles_are_rejected(title):
    assert title_looks_senior(title) is True


@pytest.mark.parametrize("title", [
    "Backend Developer", "Data Analyst", "QA Engineer",
    "Salesforce Administrator",     # contains "sr" only as a substring
    "Sales Representative",         # ditto
    "Research Engineer",            # contains "arch" only as a substring
    "Leadership Program Associate",  # contains "lead" only as a substring
])
def test_ordinary_titles_pass_the_precheck(title):
    assert title_looks_senior(title) is False


@pytest.mark.parametrize("title", [
    "Junior Developer", "Intern - Software", "Student Position",
    "Graduate Software Engineer",
])
def test_junior_titles_do_not_auto_pass(title):
    """~35% of postings labelled entry-level still demand 3+ years, so a
    junior-sounding title must prove nothing and go on to the detail fetch
    like anything else."""
    assert title_looks_senior(title) is False
    verdict = ExperienceFilter().prescreen(_job(title=title))
    assert verdict is None          # no opinion - NOT an early pass


def test_senior_title_is_rejected_without_any_description():
    verdict = ExperienceFilter().prescreen(_job(title="Senior Engineer"))
    assert verdict.passed is False
    assert verdict.confidence == "certain"


# ---------------------------------------------------------------------------
# The decision table: fail-open, hard threshold
# ---------------------------------------------------------------------------

def _evaluate(description, **kwargs):
    return ExperienceFilter(**kwargs).evaluate(_job(description=description))


def test_undetermined_passes_and_is_flagged_unknown():
    job, verdict = _evaluate("<p>Join our team.</p>")
    assert verdict.passed is True
    assert verdict.confidence == "unknown"
    assert job.min_years_exp is None


def test_at_the_threshold_passes():
    job, verdict = _evaluate("<h3>Requirements</h3><ul>"
                             "<li>1 year of experience</li></ul>")
    assert verdict.passed is True
    assert verdict.confidence == "certain"
    assert job.min_years_exp == 1.0


def test_above_the_threshold_rejects():
    job, verdict = _evaluate("<h3>Requirements</h3><ul>"
                             "<li>5+ years of experience</li></ul>")
    assert verdict.passed is False
    assert verdict.confidence == "certain"
    assert job.min_years_exp == 5.0


def test_threshold_is_configurable():
    description = "<h3>Requirements</h3><ul><li>3+ years of experience</li></ul>"
    assert _evaluate(description)[1].passed is False
    assert _evaluate(description, max_years=5)[1].passed is True


def test_strict_mode_makes_undetermined_reject():
    _, verdict = _evaluate("<p>Join our team.</p>", strict=True)
    assert verdict.passed is False
    assert verdict.confidence == "unknown"


def test_strict_mode_still_passes_a_low_number():
    _, verdict = _evaluate("<h3>Requirements</h3><ul>"
                           "<li>1 year of experience</li></ul>", strict=True)
    assert verdict.passed is True


# ---------------------------------------------------------------------------
# G. The three tags
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("description,marker", [
    ("<h3>Requirements</h3><ul><li>1 year of experience</li></ul>", "✅"),
    ("<h3>Requirements</h3><ul><li>ללא ניסיון</li></ul>", "✅"),
    ("<p>Join our team.</p>", "⚠️"),
    ("<h3>Requirements</h3><ul><li>Proven experience at scale</li></ul>", "🔶"),
])
def test_three_distinct_tags(description, marker):
    job_filter = ExperienceFilter()
    job, verdict = job_filter.evaluate(_job(description=description))
    assert job_filter.tag_for(job, verdict).startswith(marker)


def test_all_three_tag_levels_still_send():
    """Fail-open is unchanged by the tagging - the flag only sorts the
    undetermined pile, it never suppresses anything."""
    jobs = [_job("1", description="<p>Nothing stated.</p>"),
            _job("2", description="<h3>Requirements</h3><ul>"
                                  "<li>Proven experience</li></ul>"),
            _job("3", description="<h3>Requirements</h3><ul>"
                                  "<li>1 year of experience</li></ul>")]
    survivors = run_chain(jobs, _PROFILE, [ExperienceFilter()])
    assert [job.id for job, _ in survivors] == ["1", "2", "3"]
    assert all(tag for _, tag in survivors)


# ---------------------------------------------------------------------------
# J. The runtime toggle, and the cost guarantee that depends on it
# ---------------------------------------------------------------------------

def test_filter_starts_on_by_default():
    from settings import DEFAULTS
    assert DEFAULTS["experience"]["enabled"] is True
    assert DEFAULTS["experience"]["max_years"] == 1.0
    assert DEFAULTS["experience"]["strict"] is False


def test_a_disabled_filter_is_never_constructed():
    chain = build_chain({"experience": {"enabled": False}})
    assert chain == []


def test_a_disabled_chain_sends_everything_untagged():
    jobs = [_job("1", title="Senior Engineer"),
            _job("2", description="<h3>Requirements</h3><ul>"
                                  "<li>10+ years of experience</li></ul>")]
    survivors = run_chain(jobs, _PROFILE, [])
    assert [job.id for job, _ in survivors] == ["1", "2"]
    assert all(tag is None for _, tag in survivors)


def test_a_disabled_chain_triggers_no_detail_fetch(monkeypatch):
    """With the filter off, run cost must return to exactly what it was
    before this patch - not "the same work, decided differently"."""
    def explode(*args, **kwargs):
        raise AssertionError("the detail layer must not run for an empty chain")

    monkeypatch.setattr(detail_mod, "enrich", explode)
    monkeypatch.setattr(filters_mod.detail, "enrich", explode)
    assert len(run_chain([_job("1")], _PROFILE, [])) == 1


def test_a_job_rejected_by_title_is_never_detail_fetched(monkeypatch):
    """The whole point of the pre-check: a senior title costs zero requests."""
    seen = []

    def spy(jobs, profile, budget=None):
        seen.extend(job.id for job in jobs)
        return jobs

    monkeypatch.setattr(filters_mod.detail, "enrich", spy)
    run_chain([_job("1", title="Senior Engineer"), _job("2")],
              _PROFILE, [ExperienceFilter()])
    assert seen == ["2"]        # the senior job never reached the fetch


# ---------------------------------------------------------------------------
# K. Stats
# ---------------------------------------------------------------------------

def test_every_outcome_lands_in_its_own_counter():
    jobs = [
        _job("1", title="Senior Engineer"),
        _job("2", description="<h3>Requirements</h3><ul>"
                              "<li>1 year of experience</li></ul>"),
        _job("3", description="<h3>Requirements</h3><ul>"
                              "<li>8+ years of experience</li></ul>"),
        _job("4", description="<p>Nothing stated at all.</p>"),
        _job("5", description="<h3>Requirements</h3><ul>"
                              "<li>Proven experience at scale</li></ul>"),
    ]
    stats = RunStats()
    run_chain(jobs, _PROFILE, [ExperienceFilter()], stats)
    assert stats.by_filter["experience"] == {
        "rejected_by_title": 1,
        "passed_with_number": 1,
        "rejected_with_number": 1,
        "undetermined": 1,
        "undetermined_signals": 1,
    }
