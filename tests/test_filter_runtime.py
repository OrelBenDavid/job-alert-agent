# -*- coding: utf-8 -*-
"""
Integration-level tests for the two things that are easy to get right in
isolation and catastrophic to get wrong together: the ORDER of the state
write relative to filtering, and the runtime toggle the bot writes for
itself.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import commands as commands_mod
import settings as settings_mod
import state as state_mod
import stats as stats_mod
from filters import ExperienceFilter, run_chain
from models import Job
from stats import RunStats, save_stats


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Every file this patch writes gets redirected into tmp_path."""
    monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path / "seen")
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "filters.json")
    monkeypatch.setattr(stats_mod, "STATS_PATH", tmp_path / "filter_stats.json")
    yield tmp_path


def _job(job_id, title="Backend Developer", description=None):
    return Job(id=job_id, title=title, location="Tel Aviv",
               url=f"https://example.com/{job_id}", company="acme",
               description=description)


_PROFILE = SimpleNamespace(slug="acme", detail_fetch=None, zero_is_plausible=False)

_SENIOR = "<h3>Requirements</h3><ul><li>8+ years of experience</li></ul>"
_JUNIOR = "<h3>Requirements</h3><ul><li>1 year of experience</li></ul>"


# ---------------------------------------------------------------------------
# The ordering rule - the one that breaks the bot quietly if reversed
# ---------------------------------------------------------------------------

def test_a_filtered_out_job_is_still_written_to_state():
    """State means "everything ever seen", and filtering is presentation
    only. If a rejected job were held out of state it would look new on
    every subsequent run, and its detail page would be re-fetched forever."""
    state_mod.seed_company("acme", [])

    fetched = [_job("keep", description=_JUNIOR),
               _job("drop", description=_SENIOR)]
    result = state_mod.process_company("acme", fetched, _PROFILE)
    assert {job.id for job in result.new_jobs} == {"keep", "drop"}

    survivors = run_chain(result.new_jobs, _PROFILE, [ExperienceFilter()])
    assert [job.id for job, _ in survivors] == ["keep"]

    # The rejected job is nevertheless permanently recorded.
    saved = state_mod.load_state("acme")
    assert set(saved["jobs"].keys()) == {"keep", "drop"}


def test_a_rejected_job_is_not_re_detected_on_the_next_run():
    """The direct consequence of the ordering: no repeat work, ever."""
    state_mod.seed_company("acme", [])
    fetched = [_job("drop", description=_SENIOR)]

    state_mod.process_company("acme", fetched, _PROFILE)
    second_run = state_mod.process_company("acme", fetched, _PROFILE)

    assert second_run.new_jobs == []


def test_turning_the_filter_off_does_not_replay_suppressed_jobs():
    """An accepted consequence, asserted so nobody "fixes" it into a replay
    mechanism later."""
    state_mod.seed_company("acme", [])
    fetched = [_job("drop", description=_SENIOR)]
    state_mod.process_company("acme", fetched, _PROFILE)     # filter was on

    # Filter now off - but the job is already "seen", so it is not new again.
    later = state_mod.process_company("acme", fetched, _PROFILE)
    assert run_chain(later.new_jobs, _PROFILE, []) == []


# ---------------------------------------------------------------------------
# L. The seed run
# ---------------------------------------------------------------------------

def test_seeding_writes_state_without_alerts_or_detail_work():
    jobs = [_job(str(i), description=_SENIOR) for i in range(200)]
    state_mod.seed_company("acme", jobs)
    saved = state_mod.load_state("acme")
    assert len(saved["jobs"]) == 200
    assert saved["last_count"] == 200


# ---------------------------------------------------------------------------
# J. The runtime toggle
# ---------------------------------------------------------------------------

def test_defaults_apply_when_no_settings_file_exists():
    cfg = settings_mod.load_settings()["experience"]
    assert cfg == {"enabled": True, "max_years": 1.0, "strict": False}


def test_a_toggle_persists_to_disk():
    settings_mod.update_filter("experience", enabled=False)
    assert settings_mod.load_settings()["experience"]["enabled"] is False
    stored = json.loads(settings_mod.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert stored["filters"]["experience"]["enabled"] is False


def test_a_partial_stored_file_still_gets_the_other_defaults():
    """Adding a knob later must not require migrating the on-disk file."""
    settings_mod.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings_mod.SETTINGS_PATH.write_text(
        json.dumps({"filters": {"experience": {"max_years": 3}}}),
        encoding="utf-8")
    cfg = settings_mod.load_settings()["experience"]
    assert cfg["max_years"] == 3
    assert cfg["enabled"] is True and cfg["strict"] is False


def test_a_corrupt_settings_file_falls_back_to_defaults_instead_of_crashing():
    settings_mod.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings_mod.SETTINGS_PATH.write_text("{not json", encoding="utf-8")
    assert settings_mod.load_settings()["experience"]["enabled"] is True


# ---------------------------------------------------------------------------
# The Telegram command handlers
# ---------------------------------------------------------------------------

def test_filter_command_reports_state():
    reply = commands_mod._handle_filter("")
    assert "experience" in reply and "פעיל" in reply


def test_filter_command_toggles():
    commands_mod._handle_filter("experience off")
    assert settings_mod.load_settings()["experience"]["enabled"] is False
    commands_mod._handle_filter("experience on")
    assert settings_mod.load_settings()["experience"]["enabled"] is True


def test_filter_command_rejects_an_unknown_filter_and_bad_syntax():
    assert "❌" in commands_mod._handle_filter("salary on")
    assert "❌" in commands_mod._handle_filter("experience maybe")


def test_minexp_sets_the_threshold():
    commands_mod._handle_minexp("3")
    assert settings_mod.load_settings()["experience"]["max_years"] == 3.0


def test_minexp_rejects_nonsense_without_changing_anything():
    assert "❌" in commands_mod._handle_minexp("soon")
    assert "❌" in commands_mod._handle_minexp("-2")
    assert settings_mod.load_settings()["experience"]["max_years"] == 1.0


def test_minexp_strict_toggles():
    commands_mod._handle_minexp("strict on")
    assert settings_mod.load_settings()["experience"]["strict"] is True
    commands_mod._handle_minexp("strict off")
    assert settings_mod.load_settings()["experience"]["strict"] is False


def test_stats_command_before_any_run():
    assert "אין עדיין" in commands_mod._handle_stats()


def test_stats_command_reports_the_counters():
    run = RunStats()
    run.record("experience", "rejected_by_title")
    run.record("experience", "undetermined")
    save_stats(run)

    reply = commands_mod._handle_stats()
    assert "experience" in reply
    assert "נחסמו לפי כותרת" in reply


def test_an_empty_run_does_not_erase_the_last_real_measurement():
    run = RunStats()
    run.record("experience", "rejected_with_number")
    save_stats(run)
    save_stats(RunStats())          # a run with no new jobs at all

    stored = stats_mod.load_stats()
    assert stored["last_run"]["experience"]["rejected_with_number"] == 1


def test_totals_accumulate_across_runs():
    for _ in range(3):
        run = RunStats()
        run.record("experience", "undetermined")
        save_stats(run)

    stored = stats_mod.load_stats()
    assert stored["totals"]["experience"]["undetermined"] == 3
    assert stored["last_run"]["experience"]["undetermined"] == 1
