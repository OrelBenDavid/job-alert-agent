# -*- coding: utf-8 -*-
"""The health gate must eventually give up on a total zero, too.

Until 2026-08-19 only the two PARTIAL collapses had an escape hatch. The
reasoning was that freezing state on a zero costs nothing, because there are no
jobs to miss - which is true about jobs and false about the alert.
should_alert_failure fires on every run once the counter is past its threshold,
so a company that genuinely closed its last Israeli role sent the identical
maintenance alert every three hours forever, with no path back to healthy that
did not involve a human editing a JSON file.

Observed on panaya: one Israel-relevant posting was filled, and the same
"got 0 jobs after the previous run returned 1" alert went out on six
consecutive runs. Its endpoint was verified live and is perfectly healthy - it
returns four postings, in Brazil, the USA and Germany. There was no breakage to
fix and no number a human could set that would stop the alert.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import state as state_mod
from models import Job


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path)
    yield tmp_path


def _job(i):
    return Job(id="id%d" % i, title="Backend Engineer", location="Tel Aviv",
               url="https://x.com/%d" % i, company="acme")


def _profile(zero_is_plausible=False, expected_min_jobs=0):
    return SimpleNamespace(zero_is_plausible=zero_is_plausible,
                           expected_min_jobs=expected_min_jobs)


def _run_zero(times, profile):
    """Feeds `times` consecutive empty fetches and returns every result."""
    return [state_mod.process_company("acme", [], profile) for _ in range(times)]


def test_a_zero_is_still_held_at_first():
    """Nothing about the escape hatch weakens the gate itself - the whole
    point of holding is that a dead selector looks exactly like this."""
    state_mod.seed_company("acme", [_job(1), _job(2)])
    results = _run_zero(state_mod.TOTAL_ZERO_ACCEPT_AFTER - 1, _profile())

    assert all(r.status == "empty_suspicious" for r in results)
    saved = state_mod.load_state("acme")
    assert len(saved["jobs"]) == 2          # state untouched throughout
    assert saved["last_count"] == 2


def test_a_zero_is_accepted_once_it_has_been_reported_enough_times():
    state_mod.seed_company("acme", [_job(1), _job(2)])
    results = _run_zero(state_mod.TOTAL_ZERO_ACCEPT_AFTER, _profile())

    final = results[-1]
    assert final.status == "ok"
    assert final.new_jobs == []
    assert "no Israel-relevant open roles" in final.message
    assert "last alert" in final.message

    saved = state_mod.load_state("acme")
    assert saved["jobs"] == {}
    assert saved["last_count"] == 0
    assert saved["consecutive_failures"] == 0


def test_after_accepting_a_zero_the_company_goes_quiet():
    """The point of the whole change: no further maintenance alerts, and no
    stuck failure counter, once the zero is the new normal."""
    state_mod.seed_company("acme", [_job(1)])
    _run_zero(state_mod.TOTAL_ZERO_ACCEPT_AFTER, _profile())

    later = state_mod.process_company("acme", [], _profile())
    assert later.status == "ok"
    assert later.message == ""              # said once, then never again
    assert state_mod.should_alert_failure("acme") is False


def test_a_reopened_role_is_detected_as_new_after_the_zero_was_accepted():
    """Accepting must not blind the company. Its `jobs` map is emptied, so the
    next posting it publishes is genuinely new and alerts normally."""
    state_mod.seed_company("acme", [_job(1)])
    _run_zero(state_mod.TOTAL_ZERO_ACCEPT_AFTER, _profile())

    result = state_mod.process_company("acme", [_job(7)], _profile())
    assert result.status == "ok"
    assert [j.id for j in result.new_jobs] == ["id7"]


def test_the_old_jobs_re_alert_if_the_zero_was_really_a_breakage():
    """The stated cost of accepting, pinned down: a fetch that was in fact
    broken has its postings un-seen, so they come back as new once it is
    fixed. That errs toward re-sending rather than toward silence, which is
    the same trade the partial-collapse path already makes."""
    state_mod.seed_company("acme", [_job(1), _job(2)])
    _run_zero(state_mod.TOTAL_ZERO_ACCEPT_AFTER, _profile())

    recovered = state_mod.process_company("acme", [_job(1), _job(2)], _profile())
    assert {j.id for j in recovered.new_jobs} == {"id1", "id2"}


def test_a_zero_is_held_longer_than_a_partial_collapse():
    """Not the same number, deliberately: holding a zero loses no jobs, so it
    is the cheaper mistake and is allowed to run longer before giving up."""
    assert state_mod.TOTAL_ZERO_ACCEPT_AFTER > state_mod.PARTIAL_COLLAPSE_ACCEPT_AFTER


def test_a_partial_collapse_still_accepts_on_its_own_shorter_threshold():
    """The two thresholds are now chosen per branch, so this guards against
    the zero's longer hold being applied to a partial drop - which would keep
    real new postings blocked for twice as long."""
    state_mod.seed_company("acme", [_job(i) for i in range(20)])
    profile = _profile(expected_min_jobs=10)

    results = [state_mod.process_company("acme", [_job(0)], profile)
               for _ in range(state_mod.PARTIAL_COLLAPSE_ACCEPT_AFTER)]
    assert results[-1].status == "ok"
    assert "accepting 1 jobs" in results[-1].message


def test_zero_is_plausible_still_short_circuits_the_whole_gate():
    state_mod.seed_company("acme", [_job(1)])
    result = state_mod.process_company("acme", [], _profile(zero_is_plausible=True))
    assert result.status == "ok"
    assert result.message == ""
    assert state_mod.load_state("acme")["last_count"] == 0
