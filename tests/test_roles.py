# -*- coding: utf-8 -*-
"""The role filter, and the seniority-title changes that landed with it.

Every title below is a real posting from the 2026-08-18 corpus, with the
company named where it matters. The point of using real ones is that the
awkward cases are awkward in ways nobody invents: "Sales Engineer" vs
"Support Engineer", "Business Analyst - Channels" vs "Channel Manager".
"""

from types import SimpleNamespace

import pytest

import roles
from filters import (RoleFilter, ExperienceFilter, build_chain, run_chain,
                     title_looks_senior)
from models import Job
from stats import RunStats

_PROFILE = SimpleNamespace(slug="acme", detail_fetch=None)


def _job(job_id="1", title="Backend Developer", description=None):
    return Job(id=job_id, title=title, location="Tel Aviv",
               url=f"https://example.com/{job_id}", company="acme",
               description=description)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

TARGET = [
    "Backend Engineer", "DevOps Engineer", "MLOps Engineer", "Data Scientist",
    "VLSI Digital Design Graduate Engineer", "QA Automation Engineer",
    "Deep Learning Developer - REM Modeling", "Low Level Engineer",
    "System Administrator", "Help Desk", "IT Specialist", "Security Analyst",
    "Business Analyst - Channels",        # wix - must NOT hit "channel"
    "Business Data Analyst", "AI Product Data Analyst", "UI/UX Designer",
    "Drone Lab Technician", "Support Escalation Specialist",
    "Technical Support Specialist - Student Position",
    "מפתח תוכנה", "מהנדסת חשמל",
]

BLOCKED = [
    "Bookkeeper", "Payroll Accountant", "Payroll Student", "Corporate Controller",
    "General Counsel", "Legal Counsel", "FP&A Analyst", "Finance Operations Analyst",
    "Billing and Collection Specialist", "Credit Risk Analyst",
    "Account Executive", "Enterprise Account Executive", "Sales Engineer",
    "Solutions Engineer (Pre-Sales)", "Sales Development Rep - TLV",
    "Business Development Representative - French speaker",
    "Securities Sales Representative", "Marketing Operations Specialist",
    "AI Content writer- Base44", "Video Content Creator - Base44",
    "Customer Experience Specialist", "Talent Acquisition Specialist",
    "Global HRIS and HR Ops", "Administrative Assistant (50% Position)",
    "Warehouse Clerk", "Forklift Operator- Marlog", "Security Guard",
    "Taxi Line Representative (On site)", "נציג/ת מכירות",
]


@pytest.mark.parametrize("title", TARGET)
def test_target_families_pass(title):
    assert roles.classify(title)[0] == "target", title


@pytest.mark.parametrize("title", BLOCKED)
def test_off_target_families_are_blocked(title):
    assert roles.classify(title)[0] == "blocked", title


def test_a_domain_word_does_not_beat_an_unambiguous_tech_noun():
    """The override list is the whole reason both of these can be right."""
    assert roles.classify("Sales Engineer")[0] == "blocked"
    assert roles.classify("Support Engineer")[0] == "target"
    assert roles.classify("Sales Data Analyst")[0] == "target"   # data analyst wins
    assert roles.classify("Sales Administrator")[0] == "blocked"


def test_substring_matches_never_decide():
    """'sales' inside 'Salesforce' would block a genuine engineering role."""
    assert roles.classify("Salesforce Developer")[0] == "target"


UNKNOWN = ["DFIR", "CyOps Analyst is not a term", "Operator-Yiftah",
           "Material Planner", "Filmer & Editor", "2D Animator",
           "Experiment Field Operator", "Quality Control-Injection"]


@pytest.mark.parametrize("title", UNKNOWN)
def test_unrecognised_titles_are_unknown_not_blocked(title):
    """The bucket that makes a blocklist safe. These must never be rejected
    outright - state is written before filtering, so a drop is permanent."""
    assert roles.classify(title)[0] != "blocked", title


# ---------------------------------------------------------------------------
# The filter's behaviour in the chain
# ---------------------------------------------------------------------------

def test_unknown_is_sent_and_flagged_by_default():
    survivors = run_chain([_job("1", title="DFIR")], _PROFILE, [RoleFilter()])
    assert [j.id for j, _ in survivors] == ["1"]
    assert "לא מזוהה" in survivors[0][1]


def test_send_unknown_off_drops_it():
    assert run_chain([_job("1", title="DFIR")], _PROFILE,
                     [RoleFilter(send_unknown=False)]) == []


def test_a_target_role_is_sent_untagged_by_this_filter():
    survivors = run_chain([_job("1", title="Backend Engineer")], _PROFILE,
                          [RoleFilter()])
    assert survivors[0][1] is None


def test_non_job_cards_are_rejected():
    for title in ["We always look for great people!",
                  "Didn't find what you were looking for?",
                  "Explore New Opportunities", "General Application (Israel)"]:
        assert run_chain([_job("1", title=title)], _PROFILE, [RoleFilter()]) == [], title


def test_temporary_roles_are_tagged_never_dropped():
    survivors = run_chain(
        [_job("1", title="QA Engineer (Temp position) - Domains")],
        _PROFILE, [RoleFilter()])
    assert [j.id for j, _ in survivors] == ["1"]
    assert "זמנית" in survivors[0][1]


def test_an_off_target_role_never_reaches_the_detail_fetch(monkeypatch):
    """The cost guarantee: the role filter runs first and is title-only, so a
    blocked posting must not spend a detail request establishing an experience
    number nobody will read."""
    seen = []

    def spy(jobs, profile, budget=None):
        seen.extend(job.id for job in jobs)
        return jobs

    import filters as filters_mod
    monkeypatch.setattr(filters_mod.detail, "enrich", spy)
    run_chain([_job("1", title="Bookkeeper"), _job("2", title="Backend Engineer")],
              _PROFILE, [RoleFilter(), ExperienceFilter()])
    assert seen == ["2"]


def test_both_filters_tags_survive_together():
    """The old chain kept only the last non-empty label, so a job that was both
    unclassifiable AND undetermined lost one of the two."""
    survivors = run_chain(
        [_job("1", title="DFIR (Maternity Leave Cover)",
              description="<p>Nothing stated.</p>")],
        _PROFILE, [RoleFilter(), ExperienceFilter()])
    tag = survivors[0][1]
    assert "לא מזוהה" in tag and "זמנית" in tag and "לא צוינה" in tag


def test_role_counters_use_their_own_vocabulary():
    stats = RunStats()
    run_chain([_job("1", title="Backend Engineer"),
               _job("2", title="Bookkeeper"),
               _job("3", title="DFIR")],
              _PROFILE, [RoleFilter()], stats)
    assert stats.by_filter["role"] == {
        "target_role": 1, "off_target": 1,
        "unclassified_sent": 1, "unclassified_dropped": 0,
    }


def test_a_passing_prescreen_is_counted_once_not_twice():
    """RoleFilter decides entirely in prescreen and then repeats the verdict in
    evaluate; without the guard in run_chain every survivor tallied twice."""
    stats = RunStats()
    run_chain([_job("1", title="Backend Engineer")], _PROFILE, [RoleFilter()], stats)
    assert sum(stats.by_filter["role"].values()) == 1


def test_role_filter_is_on_by_default_and_first_in_the_chain():
    from settings import DEFAULTS
    assert DEFAULTS["role"]["enabled"] is True
    assert DEFAULTS["role"]["send_unknown"] is True
    chain = build_chain({"role": {"enabled": True},
                         "experience": {"enabled": True}})
    assert [f.name for f in chain] == ["role", "experience"]


def test_role_filter_can_be_turned_off():
    assert build_chain({"role": {"enabled": False},
                        "experience": {"enabled": False}}) == []


# ---------------------------------------------------------------------------
# Seniority title changes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Back-End Team Leader - Server Group",     # 33 of these were getting through
    "NOC Team Leader", "Drone Pilots Team Leader",
    "Experienced Algorithm Validation Engineer",
    "GenAI CBRNE Cyber Expert", "Tableau Expert", "AI Solution Expert",
    "CISO", "Chief Operating Officer",
])
def test_newly_caught_senior_titles(title):
    assert title_looks_senior(title) is True, title


@pytest.mark.parametrize("title", [
    "Junior Project Manager",        # mprest - the ONE false negative on record
    "Junior AP Bookkeeper", "Student Position - Data Analyst",
    "Graduate Software Engineer", "Intern - Backend",
    "VLSI Digital Design Graduate Engineer",
])
def test_a_junior_signal_overrides_a_seniority_word(title):
    assert title_looks_senior(title) is False, title


def test_cto_group_is_an_org_not_a_level():
    """Mobileye: a researcher in the CTO's group is not a C-level job."""
    assert title_looks_senior(
        "Algorithm Researcher – Autonomous Driving (CTO Group)") is False


@pytest.mark.parametrize("title", [
    "Salesforce Administrator",       # must not match "sr"
    "Backend Developer", "Data Scientist", "Technical Support Specialist",
])
def test_ordinary_titles_are_still_not_senior(title):
    assert title_looks_senior(title) is False, title
