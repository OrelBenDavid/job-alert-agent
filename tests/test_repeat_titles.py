# -*- coding: utf-8 -*-
"""The repeat-title tag.

Two of the 24 alerts delivered between 2026-08-13 and 2026-08-18 were a title
the user had already been sent for that company: Kaltura's "Help Desk" and
Wix's "Payroll Accountant". Neither was the per-city duplication that
collapse_duplicate_titles handles - each company had exactly ONE live posting
with that title. They were the same role re-posted under a new id (a new
Comeet uid; a new Wix URL slug, which is what Wix's id is derived from), so
the diff correctly saw a brand-new job.

The user's decision was to TAG rather than suppress: "Help Desk" and "QA
Engineer" are exactly the titles a company reuses for a genuine second req,
and state is written before filtering, so a drop would be permanent.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from filters import (RoleFilter, ExperienceFilter, run_chain, title_key,
                     recently_seen_titles, REPEAT_TITLE_TAG,
                     REPEAT_TITLE_WINDOW_DAYS)
from models import Job

_PROFILE = SimpleNamespace(slug="kaltura", detail_fetch=None)
_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _job(job_id, title, company="kaltura"):
    return Job(id=job_id, title=title, location="Israel",
               url=f"https://example.com/{job_id}", company=company)


def _state(*entries):
    """entries: (id, title, days_ago)"""
    return {"jobs": {jid: {"title": t,
                           "first_seen": (_NOW - timedelta(days=d)).isoformat()}
                     for jid, t, d in entries}}


# ---------------------------------------------------------------------------
# recently_seen_titles
# ---------------------------------------------------------------------------

def test_a_title_inside_the_window_is_remembered():
    keys = recently_seen_titles(_state(("E5.07B", "Help Desk", 3)), now=_NOW)
    assert keys == {"help desk"}


def test_a_title_outside_the_window_is_forgotten():
    old = REPEAT_TITLE_WINDOW_DAYS + 1
    assert recently_seen_titles(_state(("E5.07B", "Help Desk", old)), now=_NOW) == set()


def test_matching_ignores_case_and_whitespace():
    keys = recently_seen_titles(_state(("1", "  Help   Desk ", 1)), now=_NOW)
    assert title_key("help desk") in keys


def test_a_malformed_timestamp_does_not_break_the_run():
    state = {"jobs": {"1": {"title": "Help Desk", "first_seen": "not-a-date"},
                      "2": {"title": "NOC", "first_seen": None},
                      "3": {"title": "QA", "first_seen": _NOW.isoformat()}}}
    assert recently_seen_titles(state, now=_NOW) == {"qa"}


def test_a_naive_timestamp_is_treated_as_utc():
    state = {"jobs": {"1": {"title": "Help Desk",
                            "first_seen": _NOW.replace(tzinfo=None).isoformat()}}}
    assert recently_seen_titles(state, now=_NOW) == {"help desk"}


def test_empty_state_is_empty():
    assert recently_seen_titles({}, now=_NOW) == set()
    assert recently_seen_titles({"jobs": {}}, now=_NOW) == set()


# ---------------------------------------------------------------------------
# The tag in the chain
# ---------------------------------------------------------------------------

def test_the_real_kaltura_case_is_tagged_not_dropped():
    """New uid, same title, three days later."""
    seen = recently_seen_titles(_state(("E5.07B", "Help Desk", 3)), now=_NOW)
    survivors = run_chain([_job("F1.234", "Help Desk")], _PROFILE,
                          [RoleFilter()], seen_titles=seen)
    assert [j.id for j, _ in survivors] == ["F1.234"]   # still delivered
    assert REPEAT_TITLE_TAG in survivors[0][1]


def test_a_genuinely_new_title_is_not_tagged():
    seen = recently_seen_titles(_state(("E5.07B", "Help Desk", 3)), now=_NOW)
    survivors = run_chain([_job("F1.234", "NOC Engineer")], _PROFILE,
                          [RoleFilter()], seen_titles=seen)
    assert survivors[0][1] is None


def test_the_repeat_tag_reads_first_and_joins_the_others():
    seen = {"qa engineer temp position"}
    survivors = run_chain(
        [_job("1", "QA Engineer Temp Position")], _PROFILE,
        [RoleFilter(), ExperienceFilter()], seen_titles=seen)
    tag = survivors[0][1]
    assert tag.startswith(REPEAT_TITLE_TAG)
    assert "זמנית" in tag          # the temp label survives alongside it


def test_no_history_means_no_tag_anywhere():
    """The default path - a company seen for the first time, and every
    existing caller that passes nothing."""
    survivors = run_chain([_job("1", "Help Desk")], _PROFILE, [RoleFilter()])
    assert survivors[0][1] is None


def test_a_repeat_is_never_a_rejection():
    """The whole point of the user's choice. Even with every filter present,
    a repeat still comes through."""
    seen = {"help desk"}
    survivors = run_chain([_job("1", "Help Desk")], _PROFILE,
                          [RoleFilter(), ExperienceFilter()], seen_titles=seen)
    assert len(survivors) == 1


def test_same_batch_duplicates_are_still_collapsed_not_tagged():
    """collapse_duplicate_titles runs first, so two rows of one opening remain
    ONE alert - the repeat tag is only for the cross-run case."""
    survivors = run_chain([_job("1", "Help Desk"), _job("2", "Help Desk")],
                          _PROFILE, [RoleFilter()])
    assert len(survivors) == 1
    assert survivors[0][1] is None
