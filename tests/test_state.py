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


def _job(i, title="X", loc="Tel Aviv"):
    return Job(id=f"id{i}", title=title, location=loc,
              url=f"https://x.com/{i}", company="acme")


def _profile(zero_is_plausible=False, expected_min_jobs=0):
    """The two health fields process_company reads off a Profile.

    expected_min_jobs defaults to 0, which disables the partial-collapse half
    of the gate - so every test that doesn't care about it keeps testing
    exactly what it did before."""
    return SimpleNamespace(zero_is_plausible=zero_is_plausible,
                           expected_min_jobs=expected_min_jobs)


def test_new_company_first_seed_then_diff():
    state_mod.seed_company("acme", [_job(1), _job(2)])
    profile = _profile()
    result = state_mod.process_company("acme", [_job(1), _job(2), _job(3)], profile)
    assert result.status == "ok"
    assert [j.id for j in result.new_jobs] == ["id3"]


def test_silent_zero_does_not_wipe_state():
    """The core of the health gate: 0 after a healthy count is a failure,
    not 'no jobs'."""
    state_mod.seed_company("acme", [_job(1), _job(2), _job(3)])
    profile = _profile()
    result = state_mod.process_company("acme", [], profile)
    assert result.status == "empty_suspicious"
    assert result.new_jobs == []
    # state wasn't overwritten - the 3 jobs from the previous run are still there
    saved = state_mod.load_state("acme")
    assert len(saved["jobs"]) == 3
    assert saved["consecutive_failures"] == 1


def test_zero_is_plausible_allows_real_zero():
    state_mod.seed_company("tiny_co", [_job(1)])
    profile = _profile(zero_is_plausible=True)
    result = state_mod.process_company("tiny_co", [], profile)
    assert result.status == "ok"
    saved = state_mod.load_state("tiny_co")
    assert saved["jobs"] == {}   # this time it IS overwritten, since 0 was declared plausible


def test_consecutive_failures_trigger_alert_threshold():
    state_mod.seed_company("acme", [_job(1)])
    profile = _profile()
    assert state_mod.should_alert_failure("acme") is False
    state_mod.process_company("acme", [], profile)   # failure 1
    assert state_mod.should_alert_failure("acme") is False
    state_mod.process_company("acme", [], profile)   # failure 2 - crosses the threshold
    assert state_mod.should_alert_failure("acme") is True


def test_disappeared_jobs_drop_silently_no_alert_needed():
    state_mod.seed_company("acme", [_job(1), _job(2)])
    profile = _profile()
    result = state_mod.process_company("acme", [_job(1)], profile)
    assert result.status == "ok"
    assert result.new_jobs == []
    saved = state_mod.load_state("acme")
    assert set(saved["jobs"].keys()) == {"id1"}
